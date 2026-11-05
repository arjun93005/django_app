"""PostgreSQL-backed crawl frontier (work queue).

The core trick is ``FOR UPDATE SKIP LOCKED``: concurrent workers each lock a
*different* set of pending rows instead of blocking on (or double-claiming)
the same ones. Claim + mark-in-progress happen in a single statement, so a
claim is atomic and needs no application-level locking. Crashed workers are
handled like an SQS visibility timeout: rows stuck ``in_progress`` longer than
the timeout are released back to ``pending`` by ``release_stale``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection
from django.db.models import F
from django.utils import timezone

from .models import FrontierURL, Source
from .urls_util import canonicalize_url

MAX_ATTEMPTS = 4

CLAIM_SQL = """
WITH next AS (
    SELECT id
    FROM crawler_frontierurl
    WHERE source_id = %(source_id)s
      AND status = 'pending'
      AND next_attempt_at <= now()
    ORDER BY priority DESC, id
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE crawler_frontierurl AS f
   SET status = 'in_progress',
       locked_by = %(worker)s,
       locked_at = now(),
       attempts = f.attempts + 1
  FROM next
 WHERE f.id = next.id
RETURNING f.id, f.url, f.kind, f.depth, f.attempts, f.etag, f.last_modified
"""


@dataclass(frozen=True)
class Link:
    url: str
    kind: str = "page"
    priority: int = 0


@dataclass(frozen=True)
class ClaimedURL:
    id: int
    url: str
    kind: str
    depth: int
    attempts: int
    etag: str
    last_modified: str


def enqueue(source: Source, links: Iterable[Link], *, depth: int = 0) -> int:
    """Insert new URLs; already-known URLs are ignored (ON CONFLICT DO NOTHING)."""
    rows: dict = {}
    for link in links:
        url = canonicalize_url(link.url)
        url_hash = FrontierURL.hash_url(url)
        rows[url_hash] = FrontierURL(
            source=source, url=url, url_hash=url_hash, kind=link.kind, priority=link.priority, depth=depth
        )
    if not rows:
        return 0
    FrontierURL.objects.bulk_create(rows.values(), ignore_conflicts=True, batch_size=1000)
    return len(rows)


def claim(source: Source, worker_id: str, limit: int) -> list[ClaimedURL]:
    with connection.cursor() as cursor:
        cursor.execute(CLAIM_SQL, {"source_id": source.pk, "limit": limit, "worker": worker_id})
        return [ClaimedURL(*row) for row in cursor.fetchall()]


def complete(entry: ClaimedURL, *, status_code: int, etag: str = "", last_modified: str = "") -> None:
    FrontierURL.objects.filter(pk=entry.id).update(
        status=FrontierURL.Status.DONE,
        locked_by="",
        locked_at=None,
        last_status_code=status_code,
        etag=etag or entry.etag,
        last_modified=last_modified or entry.last_modified,
        last_error="",
        last_crawled_at=timezone.now(),
    )


def fail(entry: ClaimedURL, *, error: str, status_code: int | None = None, retryable: bool = True) -> bool:
    """Record a failure. Returns True if the URL will be retried later."""
    will_retry = retryable and entry.attempts < MAX_ATTEMPTS
    backoff = timedelta(seconds=min(300, 5 * 2**entry.attempts))
    FrontierURL.objects.filter(pk=entry.id).update(
        status=FrontierURL.Status.PENDING if will_retry else FrontierURL.Status.FAILED,
        next_attempt_at=timezone.now() + backoff,
        locked_by="",
        locked_at=None,
        last_status_code=status_code,
        last_error=error[:2000],
    )
    return will_retry


def reset_for_new_run(source: Source) -> int:
    """Make every known URL eligible again (ETags are kept for conditional GETs)."""
    return FrontierURL.objects.filter(
        source=source, status__in=[FrontierURL.Status.DONE, FrontierURL.Status.FAILED]
    ).update(status=FrontierURL.Status.PENDING, attempts=0, next_attempt_at=timezone.now())


def release_stale(visibility_timeout: timedelta) -> int:
    """Return URLs claimed by workers that died back to the queue."""
    return FrontierURL.objects.filter(
        status=FrontierURL.Status.IN_PROGRESS, locked_at__lt=timezone.now() - visibility_timeout
    ).update(status=FrontierURL.Status.PENDING, locked_by="", locked_at=None)


def in_flight(source: Source) -> int:
    return FrontierURL.objects.filter(source=source, status=FrontierURL.Status.IN_PROGRESS).count()


def seconds_until_next_due(source: Source) -> float | None:
    """Seconds until the earliest delayed retry becomes claimable, if any."""
    row = (
        FrontierURL.objects.filter(source=source, status=FrontierURL.Status.PENDING)
        .order_by("next_attempt_at")
        .values_list("next_attempt_at", flat=True)
        .first()
    )
    if row is None:
        return None
    return max(0.0, (row - timezone.now()).total_seconds())


def release(ids: list[int]) -> int:
    """Hand claimed-but-unprocessed URLs back to the queue (e.g. page budget hit)."""
    return FrontierURL.objects.filter(pk__in=ids, status=FrontierURL.Status.IN_PROGRESS).update(
        status=FrontierURL.Status.PENDING, locked_by="", locked_at=None, attempts=F("attempts") - 1
    )
