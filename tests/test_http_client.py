import httpx
import pytest

from apps.crawler.http import CrawlerHttpClient, RetryPolicy, RobotsDisallowed, TokenBucket


def client_for(handler, *, robots: bool = False, sleeps: list | None = None, **kwargs) -> CrawlerHttpClient:
    return CrawlerHttpClient(
        user_agent="TestBot/1.0",
        transport=httpx.MockTransport(handler),
        respect_robots=robots,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        rate_per_host=1000,
        **kwargs,
    )


def test_retries_5xx_and_honours_retry_after():
    responses = iter([httpx.Response(503, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")])
    sleeps: list[float] = []
    client = client_for(lambda request: next(responses), sleeps=sleeps)

    response = client.get("https://shop.test/a")

    assert response.status_code == 200
    assert 7 in sleeps
    assert client.stats.retries == 1
    assert dict(client.stats.status) == {"503": 1, "200": 1}


def test_gives_up_after_max_attempts_and_returns_last_response():
    client = client_for(lambda request: httpx.Response(502), retry=RetryPolicy(max_attempts=3))
    assert client.get("https://shop.test/a").status_code == 502
    assert client.stats.retries == 2


def test_transport_errors_are_retried_then_raised():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    client = client_for(handler, retry=RetryPolicy(max_attempts=3))
    with pytest.raises(httpx.ConnectError):
        client.get("https://shop.test/a")
    assert len(calls) == 3


def test_429_halves_the_host_rate():
    responses = iter([httpx.Response(429), httpx.Response(200)])
    client = client_for(lambda request: next(responses))
    client.get("https://shop.test/a")
    bucket = client.bucket_for("https://shop.test/x")
    assert bucket.rate < bucket.max_rate


def test_conditional_request_headers():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(304)

    client = client_for(handler)
    response = client.get("https://shop.test/a", etag='"v1"', last_modified="Wed, 08 Feb 2023 21:02:32 GMT")

    assert response.status_code == 304
    assert seen["if-none-match"] == '"v1"'
    assert seen["if-modified-since"].startswith("Wed")
    assert seen["user-agent"] == "TestBot/1.0"


def test_robots_txt_is_enforced_and_crawl_delay_caps_rate():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\nCrawl-delay: 2\n")
        return httpx.Response(200)

    client = client_for(handler, robots=True)
    assert client.get("https://shop.test/public").status_code == 200
    with pytest.raises(RobotsDisallowed):
        client.get("https://shop.test/private/page")
    assert client.bucket_for("https://shop.test/").max_rate == pytest.approx(0.5)


def test_robots_5xx_means_disallow_everything():
    client = client_for(lambda request: httpx.Response(503), robots=True)
    with pytest.raises(RobotsDisallowed):
        client.get("https://shop.test/anything")


def test_token_bucket_waits_when_empty():
    now = [0.0]
    waits: list[float] = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    bucket = TokenBucket(rate=2.0, burst=1, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        bucket.acquire()

    assert waits == [pytest.approx(0.5), pytest.approx(0.5)]
