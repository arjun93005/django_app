"""Celery orchestration.

    beat (every minute) -> dispatch_due_crawls
        -> start_crawl(source)                         [creates the CrawlRun]
            frontier source: chord( crawl_frontier_worker x N )( finalize_crawl )
            stream source:   chain( crawl_stream, finalize_crawl )

Horizontal scaling: frontier workers can run on any number of Celery hosts;
they coordinate purely through ``FOR UPDATE SKIP LOCKED`` on the frontier
table, so adding capacity is just ``docker compose up --scale crawler=4``.
"""

import logging

from celery import chain, chord, shared_task
from django.conf import settings
from django.db.models import F

from apps.core.db import advisory_xact_lock

from . import frontier
from .http import build_http_client
from .models import CrawlRun, Source
from .pipeline import IngestionPipeline
from .runner import (
    FrontierWorker,
    RunAlreadyActive,
    fail_run,
    fail_stuck_runs,
    finalize_run,
    flush_pipeline,
    run_stream,
    start_run,
    worker_identity,
)
from .sources.base import FrontierSource, get_source_class

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def dispatch_due_crawls() -> list[str]:
    # If beat is accidentally run twice (or during a rolling deploy) only one
    # scheduler tick does the work: the transaction-scoped advisory lock is
    # held until COMMIT, then released automatically.
    with advisory_xact_lock("crawler:dispatch") as acquired:
        if not acquired:
            logger.info("another dispatcher holds the lock; skipping tick")
            return []
        due = list(Source.objects.due().values_list("pk", "slug"))
    for pk, _ in due:
        start_crawl.delay(pk, trigger="schedule")
    return [slug for _, slug in due]


@shared_task(ignore_result=True)
def start_crawl(source_id: int, trigger: str = "manual", workers: int | None = None) -> int | None:
    source = Source.objects.get(pk=source_id)
    try:
        run = start_run(source, trigger=trigger)
    except RunAlreadyActive:
        logger.info("%s already running; not starting another run", source.slug)
        return None

    if issubclass(get_source_class(source), FrontierSource):
        fan_out = [crawl_frontier_worker.s(run.pk, i) for i in range(workers or source.concurrency)]
        chord(fan_out)(finalize_crawl.si(run.pk))
    else:
        chain(crawl_stream.si(run.pk), finalize_crawl.si(run.pk)).delay()
    return run.pk


@shared_task(bind=True)
def crawl_frontier_worker(self, run_id: int, index: int) -> int:
    run = CrawlRun.objects.select_related("source").get(pk=run_id)
    pipeline = IngestionPipeline(run.source, run, flush_every=settings.CRAWLER["FLUSH_EVERY"])
    try:
        with build_http_client(run.source) as http:
            worker = FrontierWorker(run, http=http, pipeline=pipeline, worker_id=worker_identity(self.request.id))
            worker.run_until_exhausted()
        flush_pipeline(pipeline, run_id)
    except Exception as exc:
        # Swallow + record instead of raising: a failed chord header would
        # never trigger finalize_crawl and the run would stay "running".
        logger.exception("frontier worker %s of run %s crashed", index, run_id)
        CrawlRun.objects.filter(pk=run_id).update(errors=F("errors") + 1, error_message=repr(exc)[:5000])
    return index


@shared_task
def crawl_stream(run_id: int) -> None:
    run = CrawlRun.objects.select_related("source").get(pk=run_id)
    try:
        with build_http_client(run.source) as http:
            run_stream(run, http=http)
    except Exception as exc:
        fail_run(run_id, repr(exc))
        raise


@shared_task(ignore_result=True)
def finalize_crawl(run_id: int) -> str:
    run = finalize_run(run_id)
    from apps.analytics.tasks import refresh_daily_category_stats

    refresh_daily_category_stats.delay()
    return run.status


@shared_task(ignore_result=True)
def housekeeping() -> dict[str, int]:
    """Recover from crashed workers: release their URLs, fail their runs."""
    return {
        "released_urls": frontier.release_stale(settings.CRAWLER["VISIBILITY_TIMEOUT"]),
        "failed_runs": fail_stuck_runs(),
    }
