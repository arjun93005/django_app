"""Crawl run lifecycle and worker loops (shared by the CLI and Celery tasks)."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field, fields

import httpx
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.core.cache import bump_namespace

from . import frontier
from .http import CrawlerHttpClient, RobotsDisallowed
from .models import CrawlRun, Source
from .pipeline import FlushResult, IngestionPipeline
from .sources.base import FrontierSource, StreamSource, get_source_class

logger = logging.getLogger(__name__)

MAX_RETRY_WAIT_SECONDS = 60

MERGE_HTTP_STATUS_SQL = """
UPDATE crawler_crawlrun
   SET http_status = (
        SELECT coalesce(jsonb_object_agg(code, total), '{}'::jsonb)
        FROM (
            SELECT code, sum(n::int) AS total
            FROM (
                SELECT key AS code, value AS n FROM jsonb_each_text(http_status)
                UNION ALL
                SELECT key, value FROM jsonb_each_text(%s::jsonb)
            ) AS merged
            GROUP BY code
        ) AS totals
   )
 WHERE id = %s
"""


class RunAlreadyActive(Exception):
    pass


def worker_identity(suffix: str | int = "") -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}{f':{suffix}' if suffix != '' else ''}"[:64]


@dataclass
class RunCounters:
    """Locally accumulated counters, pushed to the CrawlRun row with F() increments."""

    pages_fetched: int = 0
    pages_not_modified: int = 0
    items_scraped: int = 0
    items_invalid: int = 0
    products_created: int = 0
    products_updated: int = 0
    price_changes: int = 0
    errors: int = 0
    retries: int = 0
    http_status: dict[str, int] = field(default_factory=dict)

    def add_flush(self, result: FlushResult | None) -> None:
        if result:
            self.products_created += result.created
            self.products_updated += result.updated
            self.price_changes += result.price_changes

    def push(self, run_id: int, http: CrawlerHttpClient | None = None) -> None:
        if http is not None:
            status, retries = http.stats.snapshot_and_reset()
            self.retries += retries
            for code, n in status.items():
                self.http_status[code] = self.http_status.get(code, 0) + n

        increments = {
            f.name: F(f.name) + getattr(self, f.name)
            for f in fields(self)
            if f.name != "http_status" and getattr(self, f.name)
        }
        if increments:
            # UPDATE ... SET pages_fetched = pages_fetched + 12, ... : atomic
            # across any number of concurrent workers.
            CrawlRun.objects.filter(pk=run_id).update(**increments)
        if self.http_status:
            with connection.cursor() as cursor:
                cursor.execute(MERGE_HTTP_STATUS_SQL, [json.dumps(self.http_status), run_id])

        for f in fields(self):
            setattr(self, f.name, {} if f.name == "http_status" else 0)


class PageBudget:
    """Optional global cap on pages per run, shared by worker threads."""

    def __init__(self, limit: int | None):
        self.remaining = limit
        self._lock = threading.Lock()

    def take(self) -> bool:
        if self.remaining is None:
            return True
        with self._lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------
def start_run(source: Source, *, trigger: str = "manual") -> CrawlRun:
    try:
        with transaction.atomic():
            run = CrawlRun.objects.create(source=source, trigger=trigger)
    except IntegrityError as exc:
        # The partial unique index one_running_crawl_per_source fired.
        raise RunAlreadyActive(f"{source.slug} already has a running crawl") from exc

    Source.objects.filter(pk=source.pk).update(last_run_started_at=run.started_at)
    impl = get_source_class(source)(source)
    if isinstance(impl, FrontierSource):
        frontier.reset_for_new_run(source)
        frontier.enqueue(source, impl.seeds())
    logger.info("started crawl run %s for %s", run.pk, source.slug)
    return run


def finalize_run(run_id: int) -> CrawlRun:
    run = CrawlRun.objects.get(pk=run_id)
    if run.status != CrawlRun.Status.RUNNING:
        return run
    if run.errors == 0:
        status = CrawlRun.Status.SUCCEEDED
    elif run.items_scraped > 0:
        status = CrawlRun.Status.PARTIAL
    else:
        status = CrawlRun.Status.FAILED
    CrawlRun.objects.filter(pk=run_id, status=CrawlRun.Status.RUNNING).update(status=status, finished_at=timezone.now())
    # New data: invalidate every cached catalog API response in O(1).
    bump_namespace("catalog")
    run.refresh_from_db()
    logger.info(
        "crawl run %s finished: %s pages=%s items=%s created=%s price_changes=%s errors=%s",
        run.pk,
        run.status,
        run.pages_fetched,
        run.items_scraped,
        run.products_created,
        run.price_changes,
        run.errors,
    )
    return run


def fail_run(run_id: int, message: str) -> None:
    CrawlRun.objects.filter(pk=run_id, status=CrawlRun.Status.RUNNING).update(
        status=CrawlRun.Status.FAILED, finished_at=timezone.now(), error_message=message[:5000]
    )


def fail_stuck_runs() -> int:
    """Runs whose workers died without finalizing would block the source forever."""
    cutoff = timezone.now() - settings.CRAWLER["MAX_RUN_DURATION"]
    return CrawlRun.objects.filter(status=CrawlRun.Status.RUNNING, started_at__lt=cutoff).update(
        status=CrawlRun.Status.FAILED,
        finished_at=timezone.now(),
        error_message="exceeded MAX_RUN_DURATION; presumed dead",
    )


# --------------------------------------------------------------------------
# Frontier workers
# --------------------------------------------------------------------------
class FrontierWorker:
    def __init__(
        self,
        run: CrawlRun,
        *,
        http: CrawlerHttpClient,
        pipeline: IngestionPipeline,
        worker_id: str,
        budget: PageBudget | None = None,
        claim_batch: int | None = None,
        sleep=time.sleep,
    ):
        self.run = run
        self.source = run.source
        self.impl = get_source_class(self.source)(self.source)
        assert isinstance(self.impl, FrontierSource)
        self.http = http
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.budget = budget or PageBudget(None)
        self.claim_batch = claim_batch or settings.CRAWLER["CLAIM_BATCH"]
        self.counters = RunCounters()
        self._sleep = sleep

    def run_until_exhausted(self) -> None:
        try:
            while True:
                batch = frontier.claim(self.source, self.worker_id, self.claim_batch)
                if not batch:
                    if not self._wait_for_more_work():
                        break
                    continue
                for index, entry in enumerate(batch):
                    if not self.budget.take():
                        frontier.release([e.id for e in batch[index:]])
                        return
                    self._process(entry)
                self.counters.push(self.run.pk, self.http)
        finally:
            self.counters.push(self.run.pk, self.http)

    def _wait_for_more_work(self) -> bool:
        """No claimable rows right now. Decide whether to wait or stop."""
        if frontier.in_flight(self.source) > 0:
            # Other workers are mid-batch and may still discover new links.
            self._sleep(0.5)
            return True
        due_in = frontier.seconds_until_next_due(self.source)
        if due_in is None or due_in > MAX_RETRY_WAIT_SECONDS:
            return False  # nothing left (or only far-future retries: next run)
        self._sleep(min(due_in, 5.0) + 0.05)
        return True

    def _process(self, entry: frontier.ClaimedURL) -> None:
        c = self.counters
        try:
            result = self.impl.process(entry, self.http)
        except RobotsDisallowed:
            frontier.fail(entry, error="disallowed by robots.txt", retryable=False)
            return
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            frontier.fail(entry, error=str(exc), status_code=code, retryable=code >= 500 or code == 429)
            c.errors += 1
            return
        except httpx.HTTPError as exc:
            frontier.fail(entry, error=repr(exc))
            c.errors += 1
            return
        except Exception as exc:  # parser bug: don't retry forever, keep the run alive
            logger.exception("failed processing %s", entry.url)
            frontier.fail(entry, error=repr(exc), retryable=False)
            c.errors += 1
            return

        if result.status_code == 304:
            c.pages_not_modified += 1
        else:
            c.pages_fetched += 1
        c.items_scraped += len(result.items)
        c.items_invalid += result.invalid_items
        if result.links:
            frontier.enqueue(self.source, result.links, depth=entry.depth + 1)
        if result.items:
            c.add_flush(self.pipeline.add(result.items))
        frontier.complete(entry, status_code=result.status_code, etag=result.etag, last_modified=result.last_modified)


def flush_pipeline(pipeline: IngestionPipeline, run_id: int) -> None:
    counters = RunCounters()
    counters.add_flush(pipeline.flush())
    counters.push(run_id)


# --------------------------------------------------------------------------
# Stream (API) sources
# --------------------------------------------------------------------------
def run_stream(run: CrawlRun, *, http: CrawlerHttpClient, checkpoint: dict | None = None) -> None:
    impl = get_source_class(run.source)(run.source)
    assert isinstance(impl, StreamSource)
    pipeline = IngestionPipeline(run.source, run)
    counters = RunCounters()
    for batch in impl.stream(run, http, checkpoint or {}):
        counters.pages_fetched += 1
        counters.items_scraped += len(batch.items)
        counters.items_invalid += batch.invalid_items
        pipeline.add(batch.items)
        # Flush before persisting the checkpoint: a resumed run may replay a
        # page (upserts are idempotent) but can never skip one.
        counters.add_flush(pipeline.flush())
        CrawlRun.objects.filter(pk=run.pk).update(checkpoint=batch.checkpoint)
        counters.push(run.pk, http)
    counters.push(run.pk, http)
