import hashlib
import uuid
from datetime import timedelta

from django.db import models
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Now
from django.utils import timezone


class SourceQuerySet(models.QuerySet):
    def due(self):
        """Enabled sources whose interval elapsed and that have no active run."""
        active_run = CrawlRun.objects.filter(source=OuterRef("pk"), status__in=CrawlRun.ACTIVE_STATUSES)
        return (
            self.filter(enabled=True)
            .filter(Q(last_run_started_at__isnull=True) | Q(last_run_started_at__lte=Now() - F("crawl_interval")))
            .exclude(Exists(active_run))
        )


class Source(models.Model):
    """A website we collect products from. ``implementation`` selects the crawler class."""

    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    base_url = models.URLField()
    implementation = models.CharField(max_length=60, help_text="Key in apps.crawler.sources.REGISTRY")
    enabled = models.BooleanField(default=True)
    crawl_interval = models.DurationField(default=timedelta(hours=6))
    concurrency = models.PositiveSmallIntegerField(default=4, help_text="Parallel frontier workers")
    rate_limit_per_host = models.FloatField(default=2.0, help_text="Requests per second")
    config = models.JSONField(default=dict, blank=True)
    last_run_started_at = models.DateTimeField(null=True, blank=True)

    objects = SourceQuerySet.as_manager()

    class Meta:
        ordering = ["slug"]

    def __str__(self) -> str:
        return self.slug


class CrawlRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        PARTIAL = "partial", "Finished with errors"
        FAILED = "failed"

    ACTIVE_STATUSES = (Status.RUNNING,)

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=32, default="manual")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    # Counters are incremented concurrently by many workers with F() expressions
    # (UPDATE ... SET pages_fetched = pages_fetched + n) - no lost updates and no
    # explicit row locking needed.
    pages_fetched = models.PositiveIntegerField(default=0)
    pages_not_modified = models.PositiveIntegerField(default=0)
    items_scraped = models.PositiveIntegerField(default=0)
    items_invalid = models.PositiveIntegerField(default=0)
    products_created = models.PositiveIntegerField(default=0)
    products_updated = models.PositiveIntegerField(default=0)
    price_changes = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    retries = models.PositiveIntegerField(default=0)

    http_status = models.JSONField(default=dict, blank=True, help_text="status code histogram")
    checkpoint = models.JSONField(default=dict, blank=True, help_text="resume state for stream sources")
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "-started_at"], name="crawlrun_source_recent")]
        constraints = [
            # At most one active run per source, enforced by the database
            # (a partial unique index) instead of racy application checks.
            models.UniqueConstraint(
                fields=["source"],
                condition=Q(status="running"),
                name="one_running_crawl_per_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source.slug}#{self.pk} ({self.status})"

    @property
    def duration(self) -> timedelta | None:
        return (self.finished_at - self.started_at) if self.finished_at else None


class FrontierURL(models.Model):
    """The crawl frontier: a work queue that lives in PostgreSQL.

    Workers claim batches with ``SELECT ... FOR UPDATE SKIP LOCKED`` (see
    ``apps.crawler.frontier``) so any number of threads / Celery workers can
    consume it concurrently without double-processing a URL and without a
    separate queueing system. Rows are kept between runs so ETag /
    Last-Modified enable conditional re-crawls.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        IN_PROGRESS = "in_progress"
        DONE = "done"
        FAILED = "failed"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="+", db_index=False)
    url = models.TextField()
    # Long URLs can exceed the btree tuple limit (~2.7kB); index a fixed 16-byte
    # hash of the canonical URL instead of the URL itself.
    url_hash = models.UUIDField()
    kind = models.CharField(max_length=20, default="page")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    priority = models.SmallIntegerField(default=0)
    depth = models.SmallIntegerField(default=0)
    attempts = models.SmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_by = models.CharField(max_length=64, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    etag = models.CharField(max_length=255, blank=True)
    last_modified = models.CharField(max_length=64, blank=True)
    last_status_code = models.SmallIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_crawled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "url_hash"], name="frontier_unique_url"),
        ]
        indexes = [
            # Partial index: only *pending* rows are ever searched by the claim
            # query, so the index stays small however many URLs are done.
            models.Index(
                fields=["source", "-priority", "id"],
                condition=Q(status="pending"),
                name="frontier_claim_idx",
            ),
            models.Index(
                fields=["locked_at"],
                condition=Q(status="in_progress"),
                name="frontier_inflight_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.url

    @staticmethod
    def hash_url(url: str) -> uuid.UUID:
        return uuid.UUID(bytes=hashlib.blake2b(url.encode(), digest_size=16).digest())
