"""Two-tier response caching for read-heavy API endpoints.

Tier 1 - Nginx micro-cache: responses carry ``Cache-Control: public, max-age=N``
(a few seconds). Nginx's ``uwsgi_cache`` honours it, so bursts of identical
anonymous requests never reach uWSGI at all (see deploy/nginx).

Tier 2 - Redis: the serialized payload is cached for minutes under a key that
embeds a *namespace version*. Invalidation is O(1): when a crawl finishes we
``bump_namespace("catalog")`` and every old key becomes unreachable (and
expires on its own) - no ``KEYS``/``SCAN`` + delete storms.
"""

import hashlib
from functools import wraps

from django.core.cache import cache
from django.utils.cache import patch_cache_control
from rest_framework.response import Response


def _version_key(namespace: str) -> str:
    return f"nsver:{namespace}"


def namespace_version(namespace: str) -> int:
    version = cache.get(_version_key(namespace))
    if version is None:
        cache.add(_version_key(namespace), 1, timeout=None)
        version = cache.get(_version_key(namespace), 1)
    return int(version)


def bump_namespace(namespace: str) -> int:
    """Invalidate every cached response in ``namespace`` (atomic INCR in Redis)."""
    try:
        return cache.incr(_version_key(namespace))
    except ValueError:  # key missing or evicted
        cache.set(_version_key(namespace), 2, timeout=None)
        return 2


def cached_api_response(*, namespace: str, timeout: int = 300, edge_max_age: int = 5):
    """Decorator for DRF view methods returning cacheable GET responses."""

    def decorator(view_method):
        @wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            if request.method not in ("GET", "HEAD"):
                return view_method(self, request, *args, **kwargs)

            fingerprint = hashlib.blake2b(
                f"{request.get_full_path()}|{request.accepted_media_type}".encode(), digest_size=16
            ).hexdigest()
            key = f"api:{namespace}:v{namespace_version(namespace)}:{fingerprint}"

            payload = cache.get(key)
            if payload is not None:
                response = Response(payload)
                response["X-App-Cache"] = "HIT"
            else:
                response = view_method(self, request, *args, **kwargs)
                if response.status_code == 200:
                    # Cache the *data*, not rendered bytes, so content
                    # negotiation (JSON / browsable API) keeps working.
                    cache.set(key, response.data, timeout)
                response["X-App-Cache"] = "MISS"

            if response.status_code == 200:
                patch_cache_control(response, public=True, max_age=edge_max_age)
            return response

        return wrapper

    return decorator
