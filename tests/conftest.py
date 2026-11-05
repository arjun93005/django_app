from decimal import Decimal
from pathlib import Path

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.crawler.items import ProductItem
from apps.crawler.models import Source

FIXTURES = Path(__file__).parent / "fixtures"


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


def item(external_id: str, price: str = "10.00", **extra) -> ProductItem:
    data = {
        "external_id": external_id,
        "url": f"https://shop.test/p/{external_id}",
        "title": f"Item {external_id}",
        "price": Decimal(price),
        "currency": "USD",
        "in_stock": True,
    }
    data.update(extra)
    return ProductItem(**data)
