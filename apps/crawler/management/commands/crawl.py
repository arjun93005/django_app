"""Run a crawl synchronously from the command line.

Frontier sources are processed by N threads that all pull from the same
Postgres frontier - the same SKIP LOCKED mechanism Celery workers use in
production, just inside one process.

    python manage.py crawl books_toscrape --workers 4 --max-pages 120
    python manage.py crawl demo_shop
"""

from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection

from apps.crawler.http import build_http_client
from apps.crawler.models import CrawlRun, Source
from apps.crawler.pipeline import IngestionPipeline
from apps.crawler.runner import (
    FrontierWorker,
    PageBudget,
    RunAlreadyActive,
    fail_run,
    finalize_run,
    flush_pipeline,
    run_stream,
    start_run,
    worker_identity,
)
from apps.crawler.sources.base import FrontierSource, get_source_class


class Command(BaseCommand):
    help = "Crawl a source synchronously (threads instead of Celery)."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Source slug, e.g. books_toscrape")
        parser.add_argument("--workers", type=int, help="Frontier worker threads (default: source.concurrency)")
        parser.add_argument("--max-pages", type=int, help="Stop after this many pages (demo / smoke runs)")
        parser.add_argument("--resume", action="store_true", help="Stream sources: resume the last failed run")
        parser.add_argument("--skip-analytics", action="store_true", help="Don't refresh the materialized view")

    def handle(self, *args, **opts):
        try:
            source = Source.objects.get(slug=opts["source"])
        except Source.DoesNotExist as exc:
            raise CommandError(f"unknown source {opts['source']!r}") from exc

        try:
            run = start_run(source, trigger="cli")
        except RunAlreadyActive as exc:
            raise CommandError(str(exc)) from exc

        http = build_http_client(source)
        try:
            if issubclass(get_source_class(source), FrontierSource):
                self._crawl_frontier(
                    run, http, workers=opts["workers"] or source.concurrency, max_pages=opts["max_pages"]
                )
            else:
                checkpoint = self._resume_checkpoint(source) if opts["resume"] else {}
                run_stream(run, http=http, checkpoint=checkpoint)
        except BaseException as exc:  # includes KeyboardInterrupt: never leave a run "running"
            fail_run(run.pk, repr(exc))
            raise
        finally:
            http.close()

        run = finalize_run(run.pk)
        if not opts["skip_analytics"]:
            from apps.analytics.services import refresh_daily_category_stats

            refresh_daily_category_stats()
        self._report(run)

    def _crawl_frontier(self, run: CrawlRun, http, *, workers: int, max_pages: int | None) -> None:
        pipeline = IngestionPipeline(run.source, run)
        budget = PageBudget(max_pages)

        def work(index: int) -> None:
            try:
                FrontierWorker(
                    run, http=http, pipeline=pipeline, worker_id=worker_identity(index), budget=budget
                ).run_until_exhausted()
            finally:
                # Django connections are per-thread; close ours explicitly.
                connection.close()

        self.stdout.write(f"crawling {run.source.slug} with {workers} worker thread(s) ...")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="frontier") as pool:
            for future in [pool.submit(work, i) for i in range(workers)]:
                future.result()
        close_old_connections()
        flush_pipeline(pipeline, run.pk)

    @staticmethod
    def _resume_checkpoint(source: Source) -> dict:
        last = (
            CrawlRun.objects.filter(source=source, status=CrawlRun.Status.FAILED)
            .exclude(checkpoint={})
            .order_by("-started_at")
            .first()
        )
        return last.checkpoint if last else {}

    def _report(self, run: CrawlRun) -> None:
        rows = [
            ("status", run.status),
            ("duration", f"{run.duration.total_seconds():.1f}s" if run.duration else "-"),
            ("pages fetched", run.pages_fetched),
            ("pages not modified (304)", run.pages_not_modified),
            ("items scraped", run.items_scraped),
            ("items invalid", run.items_invalid),
            ("products created", run.products_created),
            ("products updated", run.products_updated),
            ("price changes recorded", run.price_changes),
            ("errors", run.errors),
            ("http retries", run.retries),
            ("http status", run.http_status),
        ]
        width = max(len(k) for k, _ in rows)
        self.stdout.write(self.style.SUCCESS(f"\nrun #{run.pk} for {run.source.slug}"))
        for key, value in rows:
            self.stdout.write(f"  {key.ljust(width)}  {value}")
