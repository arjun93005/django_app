import threading
from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.catalog.models import PricePoint, Product
from apps.core.db import advisory_key
from apps.crawler import frontier
from apps.crawler.frontier import Link
from apps.crawler.models import CrawlRun, FrontierURL, Source
from apps.crawler.runner import RunAlreadyActive, start_run
from apps.crawler.tasks import dispatch_due_crawls, housekeeping, start_crawl

from .conftest import make_source


@pytest.mark.django_db
def test_due_respects_interval_enabled_flag_and_active_runs():
    never_run = make_source("never-run")
    recent = make_source("recent", last_run_started_at=timezone.now(), crawl_interval=timedelta(hours=1))
    overdue = make_source(
        "overdue", last_run_started_at=timezone.now() - timedelta(hours=2), crawl_interval=timedelta(hours=1)
    )
    make_source("disabled", enabled=False)
    busy = make_source("busy")
    CrawlRun.objects.create(source=busy)

    due = set(Source.objects.due().values_list("slug", flat=True))

    assert {never_run.slug, overdue.slug} <= due
    assert not due & {recent.slug, "disabled", busy.slug}


@pytest.mark.django_db
def test_database_enforces_one_running_crawl_per_source(source):
    start_run(source)
    with pytest.raises(RunAlreadyActive):
        start_run(source)
    CrawlRun.objects.update(status=CrawlRun.Status.SUCCEEDED)
    start_run(source)  # allowed again once the previous run finished


@pytest.mark.django_db
def test_celery_chord_crawls_and_finalizes(source):
    """start_crawl -> chord(frontier workers) -> finalize_crawl -> MV refresh (all eager)."""
    run_id = start_crawl.delay(source.pk, workers=3).get()

    run = CrawlRun.objects.get(pk=run_id)
    assert run.status == "succeeded"
    assert (run.pages_fetched, run.items_scraped, run.products_created) == (6, 5, 5)
    assert Product.objects.count() == 5
    assert PricePoint.objects.count() == 5
    assert set(FrontierURL.objects.values_list("status", flat=True)) == {"done"}


@pytest.mark.django_db(transaction=True)
def test_dispatcher_skips_tick_when_another_holds_the_lock():
    make_source()
    acquired, release = threading.Event(), threading.Event()

    def other_scheduler():
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [advisory_key("crawler:dispatch")])
            acquired.set()
            release.wait(5)
        connection.close()

    thread = threading.Thread(target=other_scheduler)
    thread.start()
    acquired.wait(5)
    try:
        assert dispatch_due_crawls() == []
    finally:
        release.set()
        thread.join()


@pytest.mark.django_db
def test_housekeeping_recovers_from_dead_workers(source, settings):
    frontier.enqueue(source, [Link("https://shop.test/x")])
    frontier.claim(source, "dead", 1)
    FrontierURL.objects.update(locked_at=timezone.now() - timedelta(hours=1))
    CrawlRun.objects.create(source=source, started_at=timezone.now() - timedelta(days=1))

    result = housekeeping()

    assert result == {"released_urls": 1, "failed_runs": 1}
    assert CrawlRun.objects.get().status == "failed"
