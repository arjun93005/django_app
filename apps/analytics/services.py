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
WITH recently_changed AS (
    -- semi-join first, so the window function only runs over products that
    -- actually changed inside the window (BRIN index on observed_at)
    SELECT DISTINCT product_id
    FROM catalog_pricepoint
    WHERE observed_at >= %(since)s
),
changes AS (
    SELECT pp.product_id,
           pp.observed_at,
           pp.price,
           lag(pp.price) OVER w AS previous_price
    FROM catalog_pricepoint pp
    JOIN recently_changed rc USING (product_id)
    WINDOW w AS (PARTITION BY pp.product_id ORDER BY pp.observed_at)
)
SELECT p.id, p.title, p.currency, cat.slug AS category, s.slug AS source,
       c.previous_price, c.price, c.observed_at,
       round((c.price - c.previous_price) / c.previous_price * 100, 2) AS change_pct
FROM changes c
JOIN catalog_product p   ON p.id = c.product_id
JOIN crawler_source s    ON s.id = p.source_id
LEFT JOIN catalog_category cat ON cat.id = p.category_id
WHERE c.observed_at >= %(since)s
  AND c.previous_price > 0
  AND c.price < c.previous_price
  AND (c.previous_price - c.price) / c.previous_price * 100 >= %(min_drop_pct)s
  AND (%(category)s::text IS NULL OR cat.slug = %(category)s::text)
ORDER BY change_pct ASC, c.observed_at DESC
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
