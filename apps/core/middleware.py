"""Request tracing and lightweight performance instrumentation.

``RequestIDMiddleware`` picks up Nginx's ``$request_id`` (forwarded as
``X-Request-ID``) so one id ties together the Nginx access log, Django logs
and the response the client sees.

``ServerTimingMiddleware`` adds a ``Server-Timing`` header - Chrome/Firefox
devtools render it in the Network tab - with total app time, DB time and the
number of SQL queries. It hooks ``connection.execute_wrapper`` instead of
``DEBUG=True`` query logging, so it is cheap enough to leave on in production
and makes N+1 regressions visible immediately.
"""

import logging
import time
import uuid
from contextvars import ContextVar

from django.conf import settings
from django.db import connection

logger = logging.getLogger("pricelens.request")

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class RequestIDLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex
        token = _request_id.set(request_id)
        request.request_id = request_id
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


class _QueryTimer:
    __slots__ = ("count", "seconds")

    def __init__(self) -> None:
        self.count = 0
        self.seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.count += 1
            self.seconds += time.perf_counter() - start


class ServerTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = getattr(settings, "SLOW_REQUEST_MS", 500)

    def __call__(self, request):
        timer = _QueryTimer()
        start = time.perf_counter()
        with connection.execute_wrapper(timer):
            response = self.get_response(request)
        total_ms = (time.perf_counter() - start) * 1000
        db_ms = timer.seconds * 1000

        response["Server-Timing"] = f'app;dur={total_ms:.1f}, db;dur={db_ms:.1f};desc="{timer.count} queries"'
        if total_ms >= self.slow_ms:
            logger.warning(
                "slow request %s %s status=%s total=%.0fms db=%.0fms queries=%d",
                request.method,
                request.get_full_path(),
                response.status_code,
                total_ms,
                db_ms,
                timer.count,
            )
        return response
