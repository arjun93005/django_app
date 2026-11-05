"""Health endpoints used by Docker, Nginx and load balancers.

* ``/healthz`` - liveness: is the uWSGI worker able to run Python at all?
  Deliberately does no I/O so a slow database never gets workers restarted.
* ``/readyz``  - readiness: can this instance actually serve traffic?
"""

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    checks: dict[str, str] = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        checks["database"] = f"error: {exc.__class__.__name__}"

    try:
        cache.set("readyz:probe", "1", timeout=5)
        checks["cache"] = "ok" if cache.get("readyz:probe") == "1" else "error: miss"
    except Exception as exc:  # noqa: BLE001
        checks["cache"] = f"error: {exc.__class__.__name__}"

    healthy = all(value == "ok" for value in checks.values())
    return JsonResponse({"status": "ok" if healthy else "degraded", "checks": checks}, status=200 if healthy else 503)
