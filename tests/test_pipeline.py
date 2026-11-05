from decimal import Decimal

import pytest

from apps.catalog.models import Category, PricePoint, Product
from apps.crawler.pipeline import IngestionPipeline

from .conftest import item


@pytest.mark.django_db
def test_upsert_distinguishes_inserts_from_updates(source):
    pipeline = IngestionPipeline(source, None)

    first = pipeline.add([item("A"), item("B")]) or pipeline.flush()
    pipeline.add([item("A", title="Renamed"), item("C")])
    second = pipeline.flush()

    assert (first.created, first.updated) == (2, 0)
    assert (second.created, second.updated) == (1, 1)
    assert Product.objects.get(external_id="A").title == "Renamed"
    assert Product.objects.count() == 3


@pytest.mark.django_db
def test_price_points_are_only_written_on_change(source):
    pipeline = IngestionPipeline(source, None)

    def crawl(price, in_stock=True):
        pipeline.add([item("A", price=price, in_stock=in_stock)])
        return pipeline.flush().price_changes

    assert crawl("10.00") == 1  # first observation
    assert crawl("10.00") == 0  # unchanged: no new row
    assert crawl("8.50") == 1  # price drop
    assert crawl("8.50", in_stock=False) == 1  # availability change
    assert list(PricePoint.objects.order_by("observed_at").values_list("price", flat=True)) == [
        Decimal("10.00"),
        Decimal("8.50"),
        Decimal("8.50"),
    ]


@pytest.mark.django_db
def test_price_changed_at_moves_only_when_price_changes(source):
    pipeline = IngestionPipeline(source, None)
    pipeline.add([item("A", price="10.00")])
    pipeline.flush()
    changed_at = Product.objects.get().price_changed_at

    pipeline.add([item("A", price="10.00", title="New title")])
    pipeline.flush()
    product = Product.objects.get()
    assert product.price_changed_at == changed_at
    assert product.last_seen_at > changed_at

    pipeline.add([item("A", price="12.00")])
    pipeline.flush()
    assert Product.objects.get().price_changed_at > changed_at


@pytest.mark.django_db
def test_duplicate_keys_in_one_batch_do_not_break_the_upsert(source):
    pipeline = IngestionPipeline(source, None, flush_every=1000)
    pipeline.add([item("A", price="1.00"), item("A", price="2.00")])  # same key twice
    pipeline.flush()
    assert Product.objects.get().current_price == Decimal("2.00")


@pytest.mark.django_db
def test_categories_are_created_once_and_linked(source):
    pipeline = IngestionPipeline(source, None)
    pipeline.add([item("A", category="Noise Cancelling"), item("B", category="Noise Cancelling")])
    pipeline.flush()
    assert Category.objects.get().slug == "noise-cancelling"
    assert set(Product.objects.values_list("category__slug", flat=True)) == {"noise-cancelling"}


@pytest.mark.django_db
def test_search_vector_is_maintained_by_the_trigger(source):
    pipeline = IngestionPipeline(source, None)
    pipeline.add([item("A", title="Wireless Headphones", brand="Auralis")])
    pipeline.flush()
    assert Product.objects.filter(search_vector="headphone").exists()  # stemmed match

    pipeline.add([item("A", title="Mechanical Keyboard", brand="Auralis")])
    pipeline.flush()
    assert not Product.objects.filter(search_vector="headphone").exists()
    assert Product.objects.filter(search_vector="keyboards").exists()
