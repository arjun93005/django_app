"""Analytical queries: hand-written SQL where it reads better, ORM where it composes better."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import Count, F, Max, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from apps.catalog.models import PricePoint, Product
from apps.core.db import advisory_xact_lock, refresh_materialized_view

DAILY_STATS_VIEW = "analytics_daily_category_stats"


def refresh_daily_category_stats() -> bool:
    """REFRESH the materialized view; returns False if a refresh is already running."""
    with advisory_xact_lock(f"refresh:{DAILY_STATS_VIEW}") as acquired:
        if acquired:
            refresh_materialized_view(DAILY_STATS_VIEW, concurrently=True)
        return acquired


PRICE_DROPS_SQL = """
SELECT p.id, p.title, p.currency, cat.slug AS category, s.slug AS source,
       prev.price AS previous_price, cur.price, cur.observed_at,
       round((cur.price - prev.price) / prev.price * 100, 2) AS change_pct
FROM catalog_pricepoint cur                       -- recent rows only: BRIN on observed_at
CROSS JOIN LATERAL (
    -- The observation right before this one, even if it is older than the
    -- window: one index-only probe on (product_id, observed_at DESC) INCLUDE (price).
    SELECT pp.price
    FROM catalog_pricepoint pp
    WHERE pp.product_id = cur.product_id
      AND pp.observed_at < cur.observed_at
    ORDER BY pp.observed_at DESC
    LIMIT 1
) AS prev
JOIN catalog_product p   ON p.id = cur.product_id
JOIN crawler_source s    ON s.id = p.source_id
LEFT JOIN catalog_category cat ON cat.id = p.category_id
WHERE cur.observed_at >= %(since)s
  AND prev.price > 0
  AND cur.price < prev.price
  AND (prev.price - cur.price) / prev.price * 100 >= %(min_drop_pct)s
  AND (%(category)s::text IS NULL OR cat.slug = %(category)s::text)
ORDER BY change_pct ASC, cur.observed_at DESC
LIMIT %(limit)s
"""


def biggest_price_drops(
    *, days: int = 7, min_drop_pct: Decimal = Decimal("5"), category: str | None = None, limit: int = 50
):
    params = {
        "since": timezone.now() - timedelta(days=days),
        "min_drop_pct": min_drop_pct,
        "category": category,
        "limit": limit,
    }
    with connection.cursor() as cursor:
        cursor.execute(PRICE_DROPS_SQL, params)
        columns = [col.name for col in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def cheapest_per_category(*, per_category: int = 3):
    """Top-N-per-group with the ORM: ROW_NUMBER() OVER (PARTITION BY category ...).

    Filtering on a window expression isn't legal SQL in a WHERE clause; since
    Django 4.2 the ORM wraps the query in a subquery and filters outside it
    (the portable equivalent of QUALIFY).
    """
    return (
        Product.objects.filter(in_stock=True, category__isnull=False)
        .select_related("category")
        .only("id", "title", "currency", "current_price", "category__slug", "category__name")
        .annotate(
            position=Window(
                RowNumber(),
                partition_by=[F("category_id")],
                order_by=[F("current_price").asc(), F("id").asc()],
            )
        )
        .filter(position__lte=per_category)
        .order_by("category__name", "position")
    )


def overview() -> dict:
    """Dashboard counters in two aggregate queries (conditional aggregation)."""
    day_ago = timezone.now() - timedelta(hours=24)
    products = Product.objects.aggregate(
        total=Count("id"),
        in_stock=Count("id", filter=Q(in_stock=True)),
        changed_24h=Count("id", filter=Q(price_changed_at__gte=day_ago)),
        sources=Count("source", distinct=True),
        last_seen=Max("last_seen_at"),
    )
    points = PricePoint.objects.aggregate(
        total=Count("id"),
        last_24h=Count("id", filter=Q(observed_at__gte=day_ago)),
    )
    return {"products": products, "price_points": points}
