"""robots.txt handling following RFC 9309 semantics.

* 2xx  -> parse and obey the rules (and Crawl-delay, which tightens our rate limit)
* 4xx  -> "unavailable": no restrictions
* 5xx / network error -> "unreachable": assume complete disallow
"""

import logging
import threading
import time
import urllib.robotparser
from collections.abc import Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], tuple[int, str]]


class RobotsPolicy:
    def __init__(self, fetch: Fetcher, user_agent: str, ttl: float = 3600.0):
        self._fetch = fetch
        self._user_agent = user_agent
        self._ttl = ttl
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | bool]] = {}
        self._lock = threading.Lock()

    def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            cached = self._cache.get(origin)
            if cached and cached[0] > time.monotonic():
                return cached[1]

        try:
            status, body = self._fetch(f"{origin}/robots.txt")
        except Exception as exc:  # noqa: BLE001
            logger.warning("robots.txt unreachable for %s (%s): disallowing", origin, exc)
            status, body = 503, ""

        if 200 <= status < 300:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.splitlines())
            policy: urllib.robotparser.RobotFileParser | bool = parser
        elif 400 <= status < 500:
            policy = True  # allow everything
        else:
            policy = False  # disallow everything

        with self._lock:
            self._cache[origin] = (time.monotonic() + self._ttl, policy)
        return policy

    def allowed(self, url: str) -> bool:
        policy = self._parser_for(url)
        if isinstance(policy, bool):
            return policy
        return policy.can_fetch(self._user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        policy = self._parser_for(url)
        if isinstance(policy, bool):
            return None
        delay = policy.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None
