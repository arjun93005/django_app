"""API discovery: record the XHR/fetch traffic of a page through Chrome DevTools.

This automates the first step of reverse engineering a site's private API -
what you would otherwise do by hand in the DevTools "Network" tab:

1. load the page in Chrome with performance logging enabled,
2. optionally interact with it (click "load more", scroll ...),
3. rebuild request/response pairs from ``Network.*`` CDP events, fetching
   response bodies with ``Network.getResponseBody``,
4. summarise each endpoint: method, path, query keys, *non-standard* request
   headers (where auth / signatures live), status codes and the JSON shape.

Cookies are redacted before anything is written to disk.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qsl, urlsplit

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver

from .browser import execute_cdp, get_log

STANDARD_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "connection",
    "content-length",
    "content-type",
    "host",
    "origin",
    "referer",
    "user-agent",
    "cache-control",
    "pragma",
    "dnt",
}
SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization"}


@dataclass
class Exchange:
    request_id: str
    method: str
    url: str
    resource_type: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str | None = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = ""
    body: str | None = None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.url).query, keep_blank_values=True))

    def redacted(self) -> dict:
        data = asdict(self)
        for key in ("request_headers", "response_headers"):
            data[key] = {k: ("<redacted>" if k.lower() in SENSITIVE_HEADERS else v) for k, v in data[key].items()}
        return data


def _lower(headers: dict) -> dict[str, str]:
    return {k.lower(): str(v) for k, v in (headers or {}).items()}


class NetworkRecorder:
    def __init__(self, driver: WebDriver):
        self.driver = driver
        self.exchanges: dict[str, Exchange] = {}

    def drain(self) -> None:
        """Consume pending performance-log entries (the log is cleared on read)."""
        finished: list[str] = []
        for entry in get_log(self.driver, "performance"):
            message = json.loads(entry["message"])["message"]
            method, params = message.get("method"), message.get("params", {})
            if method == "Network.requestWillBeSent":
                request = params["request"]
                self.exchanges[params["requestId"]] = Exchange(
                    request_id=params["requestId"],
                    method=request["method"],
                    url=request["url"],
                    resource_type=params.get("type", ""),
                    request_headers=_lower(request.get("headers")),
                    request_body=request.get("postData"),
                )
            elif method == "Network.requestWillBeSentExtraInfo":
                # The headers actually put on the wire (incl. cookies).
                exchange = self.exchanges.get(params["requestId"])
                if exchange:
                    exchange.request_headers.update(_lower(params.get("headers")))
            elif method == "Network.responseReceived":
                exchange = self.exchanges.get(params["requestId"])
                if exchange:
                    response = params["response"]
                    exchange.status = response.get("status")
                    exchange.response_headers = _lower(response.get("headers"))
                    exchange.mime_type = response.get("mimeType", "")
            elif method == "Network.loadingFinished":
                finished.append(params["requestId"])

        for request_id in finished:
            exchange = self.exchanges.get(request_id)
            if exchange and exchange.resource_type in ("XHR", "Fetch") and exchange.body is None:
                try:
                    exchange.body = execute_cdp(self.driver, "Network.getResponseBody", {"requestId": request_id})[
                        "body"
                    ]
                except WebDriverException:
                    exchange.body = None  # evicted from Chrome's buffer

    def wait_for_idle(self, quiet_seconds: float = 1.5, timeout: float = 20.0) -> None:
        """Drain until no new requests appear for ``quiet_seconds`` ("network idle")."""
        deadline = time.monotonic() + timeout
        last_count, quiet_since = -1, time.monotonic()
        while time.monotonic() < deadline:
            self.drain()
            if len(self.exchanges) != last_count:
                last_count, quiet_since = len(self.exchanges), time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_seconds:
                return
            time.sleep(0.25)

    def api_calls(self) -> list[Exchange]:
        return [e for e in self.exchanges.values() if e.resource_type in ("XHR", "Fetch")]


def json_shape(value, depth: int = 0):
    """Describe the structure of a JSON document (types, not values)."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {k: json_shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [json_shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


@dataclass
class EndpointSummary:
    method: str
    path: str
    calls: int
    statuses: list[int]
    query_keys: list[str]
    custom_request_headers: list[str]
    response_shape: object


def summarize(exchanges: list[Exchange]) -> list[EndpointSummary]:
    grouped: dict[tuple[str, str], list[Exchange]] = defaultdict(list)
    for exchange in exchanges:
        grouped[(exchange.method, exchange.path)].append(exchange)

    summaries = []
    for (method, path), calls in grouped.items():
        shape = None
        for call in calls:
            if call.body and "json" in call.mime_type:
                try:
                    shape = json_shape(json.loads(call.body))
                    break
                except ValueError:
                    continue
        summaries.append(
            EndpointSummary(
                method=method,
                path=path,
                calls=len(calls),
                statuses=sorted({c.status for c in calls if c.status}),
                query_keys=sorted({k for c in calls for k in c.query}),
                custom_request_headers=sorted(
                    {
                        h
                        for c in calls
                        for h in c.request_headers
                        if h not in STANDARD_HEADERS and not h.startswith((":", "sec-"))
                    }
                ),
                response_shape=shape,
            )
        )
    return summaries
