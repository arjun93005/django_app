from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import DailyCategoryStats
from apps.analytics.services import cheapest_per_category, refresh_daily_category_stats
from apps.catalog.models import Category, PricePoint

from .conftest import add_price, make_product


@pytest.mark.django_db
def test_materialized_view_forward_fills_change_only_history(source, category):
    product = make_product(source, external_id="A", price="100.00", category=category)
    PricePoint.objects.filter(product=product).update(observed_at=timezone.now() - timedelta(days=3))
    add_price(product, "80.00", days_ago=1)

    assert refresh_daily_category_stats() is True

    rows = {r.day: r for r in DailyCategoryStats.objects.filter(category=category)}
    today = timezone.now().date()
    assert sorted(rows) == [today - timedelta(days=d) for d in (3, 2, 1, 0)]
    assert rows[today - timedelta(days=2)].avg_price == Decimal("100.00")  # no row that day: filled forward
    assert rows[today - timedelta(days=1)].avg_price == Decimal("80.00")
    assert rows[today - timedelta(days=1)].price_drops == 1
    assert rows[today].products_tracked == 1


@pytest.mark.django_db
def test_category_trend_endpoint_reads_the_view(api_client, source, category):
    make_product(source, external_id="A", price="50.00", category=category)
    refresh_daily_category_stats()

    body = api_client.get(reverse("analytics-category-trend", args=[category.slug]), {"days": 7}).json()

    assert body["series"][-1]["avg_price"] == "50.00"


@pytest.mark.django_db
def test_price_drops_uses_lag_over_full_history(api_client, source, category):
    old = make_product(source, external_id="OLD", price="200.00", category=category)
    PricePoint.objects.filter(product=old).update(observed_at=timezone.now() - timedelta(days=60))
    add_price(old, "150.00", days_ago=1)  # previous point is *outside* the 7-day window
    small = make_product(source, external_id="SMALL", price="100.00", category=category)
    add_price(small, "98.00")

    body = api_client.get(reverse("analytics-price-drops"), {"days": 7, "min_pct": 5}).json()

    assert body["count"] == 1
    (drop,) = body["results"]
    assert drop["title"] == old.title
    assert drop["previous_price"] == "200.00"
    assert drop["change_pct"] == "-25.00"


@pytest.mark.django_db
def test_cheapest_per_category_is_top_n_per_group(source):
    audio = Category.objects.create(name="Audio", slug="audio")
    video = Category.objects.create(name="Video", slug="video")
    for i, price in enumerate(["30", "10", "20", "5"]):
        make_product(source, external_id=f"a{i}", price=price, category=audio)
    make_product(source, external_id="v0", price="99", category=video)
    make_product(source, external_id="v1", price="1", category=video, in_stock=False)

    rows = [(p.category.slug, p.position, p.current_price) for p in cheapest_per_category(per_category=2)]

    assert rows == [("audio", 1, Decimal("5.00")), ("audio", 2, Decimal("10.00")), ("video", 1, Decimal("99.00"))]


@pytest.mark.django_db
def test_overview_counts(api_client, source):
    make_product(source, external_id="A")
    make_product(source, external_id="B", in_stock=False)
    body = api_client.get(reverse("analytics-overview")).json()
    assert body["products"]["total"] == 2
    assert body["products"]["in_stock"] == 1
    assert body["price_points"]["total"] == 2
