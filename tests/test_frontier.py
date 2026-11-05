import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.crawler import frontier
from apps.crawler.frontier import Link
from apps.crawler.models import FrontierURL

from .conftest import make_source


@pytest.mark.django_db
def test_enqueue_deduplicates_by_canonical_url(source):
    frontier.enqueue(
        source,
        [
            Link("HTTPS://Shop.test:443/a?b=2&a=1#reviews"),
            Link("https://shop.test/a?a=1&b=2"),
        ],
    )
    frontier.enqueue(source, [Link("https://shop.test/a?a=1&b=2")])  # second batch: ON CONFLICT DO NOTHING

    assert list(FrontierURL.objects.values_list("url", flat=True)) == ["https://shop.test/a?a=1&b=2"]


@pytest.mark.django_db
def test_claim_orders_by_priority_and_marks_in_progress(source):
    frontier.enqueue(source, [Link("https://shop.test/low", priority=1), Link("https://shop.test/high", priority=9)])

    claimed = frontier.claim(source, "worker-1", limit=1)

    assert [c.url for c in claimed] == ["https://shop.test/high"]
    row = FrontierURL.objects.get(pk=claimed[0].id)
    assert (row.status, row.locked_by, row.attempts) == ("in_progress", "worker-1", 1)
    assert frontier.claim(source, "worker-2", limit=10)[0].url == "https://shop.test/low"
    assert frontier.claim(source, "worker-3", limit=10) == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_workers_never_claim_the_same_url():
    """Four threads, four DB connections, one frontier: SKIP LOCKED partitions the work."""
    source = make_source()
    frontier.enqueue(source, [Link(f"https://shop.test/p/{i}") for i in range(300)])
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(4)

    def worker(name: str) -> None:
        mine: list[int] = []
        barrier.wait()  # maximise contention
        try:
            while batch := frontier.claim(source, name, limit=7):
                mine.extend(c.id for c in batch)
        finally:
            claimed[name] = mine
            connection.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_ids = [i for ids in claimed.values() for i in ids]
    assert len(all_ids) == 300
    assert len(set(all_ids)) == 300, "a URL was claimed twice"
    assert sum(1 for ids in claimed.values() if ids) > 1, "work should be spread over workers"


@pytest.mark.django_db
def test_failures_back_off_then_give_up(source):
    frontier.enqueue(source, [Link("https://shop.test/flaky")])
    for attempt in range(1, frontier.MAX_ATTEMPTS + 1):
        FrontierURL.objects.update(next_attempt_at=timezone.now())  # skip the backoff delay
        (entry,) = frontier.claim(source, "w", 1)
        assert entry.attempts == attempt
        will_retry = frontier.fail(entry, error="boom", status_code=503)
        assert will_retry is (attempt < frontier.MAX_ATTEMPTS)

    row = FrontierURL.objects.get()
    assert row.status == "failed"
    assert row.next_attempt_at > timezone.now()


@pytest.mark.django_db
def test_stale_claims_are_released(source):
    frontier.enqueue(source, [Link("https://shop.test/x")])
    frontier.claim(source, "dead-worker", 1)
    FrontierURL.objects.update(locked_at=timezone.now() - timedelta(minutes=30))

    assert frontier.release_stale(timedelta(minutes=5)) == 1
    assert FrontierURL.objects.get().status == "pending"


@pytest.mark.django_db
def test_reset_keeps_etags_for_conditional_requests(source):
    frontier.enqueue(source, [Link("https://shop.test/x")])
    (entry,) = frontier.claim(source, "w", 1)
    frontier.complete(entry, status_code=200, etag='"abc"', last_modified="Wed, 08 Feb 2023 21:02:32 GMT")

    frontier.reset_for_new_run(source)

    (again,) = frontier.claim(source, "w", 1)
    assert again.etag == '"abc"'
    assert again.last_modified.startswith("Wed")
