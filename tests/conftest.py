import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.crawler.models import Source


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


def make_source(slug: str = "test_source", implementation: str = "test_fake", **extra) -> Source:
    defaults = {"name": slug, "base_url": "https://shop.test/", "implementation": implementation, **extra}
    return Source.objects.update_or_create(slug=slug, defaults=defaults)[0]


@pytest.fixture
def source(db) -> Source:
    return make_source()
