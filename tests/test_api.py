from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import PricePoint
from apps.core.cache import bump_namespace

from .conftest import add_price, make_product


@pytest.fixture
def catalog(source, category):
    products = [make_product(source, external_id=f"P{i}", price=f"{100 + i}.00", category=category) for i in range(3)]
    PricePoint.objects.update(observed_at=timezone.now() - timedelta(days=1))
    add_price(products[0], "80.00")  # a 20% drop recorded today
    products[0].current_price = Decimal("80.00")
    products[0].save()
    return products


def _count_queries(client, url):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return len(ctx.captured_queries)


@pytest.mark.django_db
def test_product_list_uses_a_constant_number_of_queries(api_client, source, category):
    url = reverse("product-list")
    make_product(source, external_id="only-one", category=category)
    few = _count_queries(api_client, url)

    for i in range(40):
        make_product(source, external_id=f"bulk-{i}", category=category)
    bump_namespace("catalog")
    many = _count_queries(api_client, url)

    assert few == many == 1, "list endpoint must not issue per-row queries (N+1)"


@pytest.mark.django_db
def test_product_list_shape_and_previous_price(api_client, catalog):
    response = api_client.get(reverse("product-list"))
    body = response.json()
    dropped = next(p for p in body["results"] if p["external_id"] == "P0")

    assert dropped["previous_price"] == "100.00"
    assert dropped["price_change_pct"] == -20.0
    assert dropped["category"] == "headphones"
    assert dropped["source"] == "test_source"
    assert "next" in body and "count" not in body  # cursor pagination: no COUNT(*)
    assert response["Server-Timing"].startswith("app;dur=")
    assert response["Cache-Control"] == "public, max-age=5"


@pytest.mark.django_db
def test_cursor_pagination_walks_all_rows_without_duplicates(api_client, source):
    for i in range(7):
        make_product(source, external_id=f"X{i}")
    url, seen = reverse("product-list") + "?page_size=3", []
    while url:
        body = api_client.get(url).json()
        seen += [p["id"] for p in body["results"]]
        url = body["next"]
    assert len(seen) == len(set(seen)) == 7
    assert seen == sorted(seen, reverse=True)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("min_price=101&max_price=101.99", {"P1"}),
        ("category=headphones&in_stock=true", {"P0", "P1", "P2"}),
        ("category=nope", set()),
        ("attr=color:black", {"P2"}),
    ],
)
def test_product_filters(api_client, catalog, query, expected):
    catalog[2].attributes = {"color": "black", "weight_g": 250}
    catalog[2].save()
    body = api_client.get(f"{reverse('product-list')}?{query}").json()
    assert {p["external_id"] for p in body["results"]} == expected


@pytest.mark.django_db
def test_product_detail_prefetches_recent_prices_in_two_queries(api_client, catalog, django_assert_num_queries):
    with django_assert_num_queries(2):
        body = api_client.get(reverse("product-detail", args=[catalog[0].pk])).json()
    assert [p["price"] for p in body["recent_prices"]] == ["80.00", "100.00"]


@pytest.mark.django_db
def test_search_uses_fulltext_then_trigram_fallback(api_client, source):
    make_product(source, external_id="H1", title="Auralis Wireless Headphones Pro")
    make_product(source, external_id="K1", title="Kestrel Mechanical Keyboard")
    url = reverse("product-search")

    fulltext = api_client.get(url, {"q": "headphone"}).json()
    assert fulltext["strategy"] == "fulltext"
    assert [p["external_id"] for p in fulltext["results"]] == ["H1"]

    typo = api_client.get(url, {"q": "keybaord"}).json()
    assert typo["strategy"] == "trigram"
    assert [p["external_id"] for p in typo["results"]] == ["K1"]


@pytest.mark.django_db
def test_history_includes_the_price_in_effect_at_window_start(api_client, source):
    product = make_product(source, external_id="H")
    product.price_points.update(observed_at=product.price_points.first().observed_at.replace(year=2020))
    add_price(product, "9.00", days_ago=2)

    body = api_client.get(reverse("product-history", args=[product.pk]), {"days": 7}).json()

    assert [p["price"] for p in body["points"]] == ["10.00", "9.00"]


@pytest.mark.django_db
def test_versioned_cache_hit_and_invalidation(api_client, catalog):
    url = reverse("product-list")
    assert api_client.get(url)["X-App-Cache"] == "MISS"
    assert api_client.get(url)["X-App-Cache"] == "HIT"
    bump_namespace("catalog")  # what finalize_run does after a crawl
    assert api_client.get(url)["X-App-Cache"] == "MISS"


@pytest.mark.django_db
def test_category_aggregates(api_client, catalog):
    (headphones,) = api_client.get(reverse("category-list")).json()
    assert headphones["product_count"] == 3
    assert headphones["in_stock_count"] == 3
    assert Decimal(headphones["avg_price"]) == Decimal("94.33")  # (80 + 101 + 102) / 3


@pytest.mark.django_db
def test_request_id_is_propagated(api_client):
    response = api_client.get("/healthz", HTTP_X_REQUEST_ID="abc123")
    assert response["X-Request-ID"] == "abc123"


@pytest.mark.django_db
def test_readyz_checks_dependencies(api_client):
    body = api_client.get("/readyz").json()
    assert body == {"status": "ok", "checks": {"database": "ok", "cache": "ok"}}
