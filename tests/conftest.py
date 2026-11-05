from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalog.models import Category, PricePoint, Product
from apps.crawler.frontier import Link
from apps.crawler.items import ProductItem
from apps.crawler.models import Source
from apps.crawler.sources.base import FrontierSource, PageResult, register

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


@pytest.fixture
def category(db) -> Category:
    return Category.objects.get_or_create(slug="headphones", defaults={"name": "Headphones"})[0]


def make_product(source: Source, *, external_id: str, price: str = "10.00", category=None, **extra) -> Product:
    product = Product.objects.create(
        source=source,
        external_id=external_id,
        url=f"https://shop.test/p/{external_id}",
        title=extra.pop("title", f"Product {external_id}"),
        current_price=Decimal(price),
        category=category,
        **extra,
    )
    PricePoint.objects.create(
        product=product, price=product.current_price, currency=product.currency, in_stock=product.in_stock
    )
    return product


def add_price(product: Product, price: str, *, days_ago: float = 0, in_stock: bool = True) -> PricePoint:
    return PricePoint.objects.create(
        product=product,
        price=Decimal(price),
        currency=product.currency,
        in_stock=in_stock,
        observed_at=timezone.now() - timedelta(days=days_ago),
    )


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


@register
class FakeFrontierSource(FrontierSource):
    """In-memory site: 1 listing page linking to 5 product pages. No network."""

    key = "test_fake"

    def seeds(self):
        return [Link("https://shop.test/list", kind="listing", priority=10)]

    def process(self, entry, http):
        if entry.kind == "listing":
            return PageResult(200, links=[Link(f"https://shop.test/p/{i}", kind="detail") for i in range(5)])
        sku = entry.url.rsplit("/", 1)[-1]
        return PageResult(200, items=[item(f"SKU-{sku}", price=f"{10 + int(sku)}.00", category="Gadgets")])
