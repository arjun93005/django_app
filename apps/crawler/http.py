"""HTTP layer shared by every crawler.

Robustness features, each of which exists because real sites do this to you:

* **Per-host token bucket** - politeness; the rate is shared by all threads.
* **Adaptive throttling (AIMD)** - on HTTP 429 the host's rate is halved, then
  creeps back up on success, so we converge on what the server tolerates.
* **Retries with exponential backoff + full jitter** for connection errors and
  429/5xx, honouring ``Retry-After`` (seconds or HTTP-date).
* **Conditional GETs** - ``If-None-Match`` / ``If-Modified-Since`` turn
  unchanged pages into cheap 304s.
* **robots.txt** enforcement, including ``Crawl-delay``.
* Stats (status histogram, retries, bytes) that roll up into ``CrawlRun``.
"""

import email.utils
import logging
import random
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .robots import RobotsPolicy

logger = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    pass


class TokenBucket:
    """Thread-safe token bucket with AIMD rate adaptation."""

    def __init__(self, rate: float, burst: float = 1.0, *, clock=time.monotonic, sleep=time.sleep):
        self.max_rate = rate
        self.min_rate = min(rate, 0.2)
        self.rate = rate
        self.capacity = max(burst, 1.0)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            self._sleep(wait)

    def penalize(self) -> None:
        with self._lock:
            self.rate = max(self.min_rate, self.rate / 2)

    def reward(self) -> None:
        with self._lock:
            self.rate = min(self.max_rate, self.rate + self.max_rate * 0.05)

    def cap(self, rate: float) -> None:
        with self._lock:
            self.max_rate = min(self.max_rate, rate)
            self.rate = min(self.rate, self.max_rate)


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                return min(retry_after, self.backoff_max)
        # "Full jitter" (AWS architecture blog): spreads retries of many
        # workers instead of having them hammer the server in lock-step.
        return random.uniform(0, min(self.backoff_max, self.backoff_base * 2**attempt))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


@dataclass
class HttpStats:
    status: Counter = field(default_factory=Counter)
    retries: int = 0
    bytes_received: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, response: httpx.Response) -> None:
        with self._lock:
            self.status[str(response.status_code)] += 1
            self.bytes_received += len(response.content)

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def snapshot_and_reset(self) -> tuple[dict[str, int], int]:
        with self._lock:
            status, retries = dict(self.status), self.retries
            self.status.clear()
            self.retries = 0
            return status, retries


class CrawlerHttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        rate_per_host: float = 2.0,
        retry: RetryPolicy | None = None,
        timeout: float = 20.0,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.retry = retry or RetryPolicy()
        self.stats = HttpStats()
        self._rate = rate_per_host
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=True,
            transport=transport,
            # One pooled client shared by all worker threads: keep-alive
            # connections are reused instead of a TLS handshake per page.
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        self.robots = RobotsPolicy(self._fetch_robots, user_agent) if respect_robots else None

    # -- plumbing -----------------------------------------------------------
    def _fetch_robots(self, url: str) -> tuple[int, str]:
        response = self._client.get(url)
        return response.status_code, response.text

    def bucket_for(self, url: str) -> TokenBucket:
        host = urlsplit(url).netloc
        with self._buckets_lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = self._buckets[host] = TokenBucket(self._rate, sleep=self._sleep)
                if self.robots is not None:
                    delay = self.robots.crawl_delay(url)
                    if delay:
                        bucket.cap(1.0 / delay)
            return bucket

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- public API ---------------------------------------------------------
    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self.robots is not None and not self.robots.allowed(url):
            raise RobotsDisallowed(url)

        bucket = self.bucket_for(url)
        attempts = self.retry.max_attempts
        for attempt in range(1, attempts + 1):
            bucket.acquire()
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:  # timeouts, resets, DNS, protocol errors
                if attempt == attempts:
                    raise
                delay = self.retry.delay(attempt)
                logger.info("transport error %s on %s, retry %d in %.1fs", exc.__class__.__name__, url, attempt, delay)
                self.stats.record_retry()
                self._sleep(delay)
                continue

            self.stats.record(response)
            if response.status_code == 429:
                bucket.penalize()
            elif response.status_code < 400:
                bucket.reward()

            if response.status_code in self.retry.retry_statuses and attempt < attempts:
                delay = self.retry.delay(attempt, response)
                logger.info("HTTP %s on %s, retry %d in %.1fs", response.status_code, url, attempt, delay)
                self.stats.record_retry()
                self._sleep(delay)
                continue
            return response
        raise AssertionError("unreachable")

    def get(self, url: str, *, etag: str = "", last_modified: str = "", **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return self.request("GET", url, headers=headers, **kwargs)


def build_http_client(source, **overrides) -> CrawlerHttpClient:
    from django.conf import settings

    conf = settings.CRAWLER
    options = {
        "user_agent": conf["USER_AGENT"],
        "rate_per_host": source.rate_limit_per_host,
        "retry": RetryPolicy(max_attempts=conf["MAX_RETRIES"] + 1),
        "timeout": conf["HTTP_TIMEOUT"],
        "respect_robots": conf["RESPECT_ROBOTS_TXT"],
    }
    options.update(overrides)
    return CrawlerHttpClient(**options)
