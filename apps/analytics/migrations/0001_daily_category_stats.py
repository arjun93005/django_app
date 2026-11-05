"""Materialized view: per-category daily price statistics.

The price history is stored as change events, so a product that didn't change
on a given day has no row for that day. The view turns events into a
*daily, forward-filled* series:

1. ``history``: one row per change with the previous price (``lag``) and the
   date the price stopped being valid (``lead`` of the next change).
2. ``daily_prices``: each validity interval is expanded into one row per day
   with ``LATERAL generate_series`` - the classic gaps-and-islands fill.
3. Aggregate per (category, day) and join the drop/increase counts, computed
   with ``count(*) FILTER (WHERE ...)``.

A unique index on (category_id, day) allows ``REFRESH ... CONCURRENTLY``,
which rebuilds the data without blocking readers.
"""

import django.db.models.deletion
from django.db import migrations, models

CREATE_VIEW = r"""
CREATE MATERIALIZED VIEW analytics_daily_category_stats AS
WITH horizon AS (
    SELECT (current_date - 180) AS first_day
),
history AS (
    SELECT p.category_id,
           pp.observed_at::date                                             AS day,
           pp.price,
           pp.price - lag(pp.price) OVER w                                  AS delta,
           coalesce(lead(pp.observed_at::date) OVER w, current_date + 1)    AS valid_until
    FROM catalog_pricepoint pp
    JOIN catalog_product p ON p.id = pp.product_id
    WHERE p.category_id IS NOT NULL
    WINDOW w AS (PARTITION BY pp.product_id ORDER BY pp.observed_at)
),
daily_prices AS (
    -- A price superseded on the same day expands to zero rows, so the last
    -- price of each day wins.
    SELECT h.category_id, d::date AS day, h.price
    FROM history h
    CROSS JOIN horizon
    CROSS JOIN LATERAL generate_series(
        greatest(h.day, horizon.first_day), h.valid_until - 1, interval '1 day'
    ) AS d
),
price_stats AS (
    SELECT category_id, day,
           count(*)               AS products_tracked,
           round(avg(price), 2)   AS avg_price,
           min(price)             AS min_price,
           max(price)             AS max_price
    FROM daily_prices
    GROUP BY category_id, day
),
change_stats AS (
    SELECT category_id, day,
           count(*) FILTER (WHERE delta < 0) AS price_drops,
           count(*) FILTER (WHERE delta > 0) AS price_increases
    FROM history
    WHERE day >= (SELECT first_day FROM horizon)
    GROUP BY category_id, day
)
SELECT s.category_id::text || ':' || s.day::text     AS id,
       s.category_id,
       s.day,
       s.products_tracked::int                       AS products_tracked,
       s.avg_price,
       s.min_price,
       s.max_price,
       coalesce(c.price_drops, 0)::int               AS price_drops,
       coalesce(c.price_increases, 0)::int           AS price_increases
FROM price_stats s
LEFT JOIN change_stats c ON c.category_id = s.category_id AND c.day = s.day
WITH DATA;

CREATE UNIQUE INDEX analytics_dcs_category_day
    ON analytics_daily_category_stats (category_id, day);
"""

DROP_VIEW = "DROP MATERIALIZED VIEW IF EXISTS analytics_daily_category_stats;"


class Migration(migrations.Migration):
    initial = True

    dependencies = [("catalog", "0002_search_trigger_and_storage")]

    operations = [
        migrations.RunSQL(CREATE_VIEW, DROP_VIEW),
        # Register the unmanaged model in migration state (no DDL is emitted).
        migrations.CreateModel(
            name="DailyCategoryStats",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("day", models.DateField()),
                ("products_tracked", models.IntegerField()),
                ("avg_price", models.DecimalField(decimal_places=2, max_digits=12)),
                ("min_price", models.DecimalField(decimal_places=2, max_digits=12)),
                ("max_price", models.DecimalField(decimal_places=2, max_digits=12)),
                ("price_drops", models.IntegerField()),
                ("price_increases", models.IntegerField()),
                (
                    "category",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="catalog.category",
                    ),
                ),
            ],
            options={"managed": False, "db_table": "analytics_daily_category_stats", "ordering": ["day"]},
        ),
    ]
