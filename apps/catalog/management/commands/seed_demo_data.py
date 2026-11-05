"""Generate a large synthetic catalogue *inside PostgreSQL* (generate_series).

Producing ~1M rows in Python and shipping them over the wire would take
minutes; set-based INSERT ... SELECT runs in seconds. Used to get realistic
EXPLAIN plans and load-test data:

    python manage.py seed_demo_data --products 50000 --days 180
"""

import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.catalog.models import Category
from apps.crawler.models import Source

CATEGORY_NAMES = [
    "Audio",
    "Cameras",
    "Gaming",
    "Laptops",
    "Monitors",
    "Networking",
    "Peripherals",
    "Phones",
    "Smart Home",
    "Storage",
    "Tablets",
    "Wearables",
]

PRODUCTS_SQL = """
INSERT INTO catalog_product (
    source_id, external_id, url, title, brand, description, category_id, image_url, rating,
    currency, current_price, in_stock, stock_quantity, attributes, first_seen_at, last_seen_at, price_changed_at
)
SELECT %(source_id)s,
       'SYN-' || lpad(g::text, 8, '0'),
       'https://synthetic.example/p/' || g,
       (ARRAY['Wireless','Noise-Cancelling','Ergonomic','Portable','Smart','Ultra-Slim','Gaming','Studio'])[1 + g %% 8]
         || ' ' || (ARRAY['Headphones','Keyboard','Monitor','Speaker','Camera',
                          'Laptop','Watch','Router','Mouse','Tablet'])[1 + (g / 8) %% 10]
         || ' ' || (ARRAY['Pro','Max','Air','Lite','Plus','X','Neo','Edge'])[1 + (g / 80) %% 8]
         || ' ' || (g %% 97),
       (ARRAY['Auralis','Northwind','Kestrel','Lumio','Vantage','Orbitron','Halcyon','Zephyr'])[1 + (g / 3) %% 8],
       'Synthetic product ' || g || ' generated for benchmarking.',
       (%(category_ids)s::bigint[])[1 + g %% %(n_categories)s],
       '',
       1 + g %% 5,
       'USD',
       round((20 + random() * 980)::numeric, 2),
       random() > 0.1,
       (random() * 100)::int,
       jsonb_build_object('color', (ARRAY['black','silver','white','blue'])[1 + g %% 4], 'weight_g', 100 + g %% 2000),
       now() - make_interval(days => %(days)s), now(), now()
FROM generate_series(1, %(products)s) AS g
ON CONFLICT (source_id, external_id) DO NOTHING
"""

# One opening price per product plus a change on ~change_rate of all days.
# ORDER BY observed_at writes the heap in time order - exactly how real
# crawls append - which is what makes the BRIN index on observed_at effective.
PRICE_POINTS_SQL = """
INSERT INTO catalog_pricepoint (product_id, price, currency, in_stock, observed_at)
SELECT product_id, price, 'USD', in_stock, observed_at
FROM (
    SELECT p.id AS product_id,
           round((p.current_price * (0.75 + random() * 0.5))::numeric, 2) AS price,
           random() > 0.08 AS in_stock,
           now() - make_interval(days => d.day) - make_interval(secs => random() * 80000) AS observed_at
    FROM catalog_product p
    CROSS JOIN generate_series(0, %(days)s) AS d(day)
    WHERE p.source_id = %(source_id)s
      AND (d.day = %(days)s OR random() < %(change_rate)s)
) AS changes
ORDER BY observed_at
"""

# DISTINCT ON: the latest row per product in a single sorted pass.
SYNC_CURRENT_PRICE_SQL = """
UPDATE catalog_product AS p
   SET current_price = latest.price, in_stock = latest.in_stock, price_changed_at = latest.observed_at
  FROM (
        SELECT DISTINCT ON (pp.product_id) pp.product_id, pp.price, pp.in_stock, pp.observed_at
        FROM catalog_pricepoint pp
        JOIN catalog_product prod ON prod.id = pp.product_id AND prod.source_id = %(source_id)s
        ORDER BY pp.product_id, pp.observed_at DESC
  ) AS latest
 WHERE p.id = latest.product_id
"""

FRONTIER_SQL = """
INSERT INTO crawler_frontierurl (
    source_id, url, url_hash, kind, status, priority, depth, attempts, next_attempt_at,
    locked_by, etag, last_modified, last_error
)
SELECT %(source_id)s, 'https://synthetic.example/p/' || g, md5('https://synthetic.example/p/' || g)::uuid,
       'detail', CASE WHEN g %% 20 = 0 THEN 'pending' ELSE 'done' END, (g %% 10)::smallint, 1, 0, now(),
       '', '', '', ''
FROM generate_series(1, %(frontier)s) AS g
ON CONFLICT DO NOTHING
"""


class Command(BaseCommand):
    help = "Generate synthetic products, price history and frontier rows server-side."

    def add_arguments(self, parser):
        parser.add_argument("--products", type=int, default=50_000)
        parser.add_argument("--days", type=int, default=180)
        parser.add_argument("--change-rate", type=float, default=0.08, help="share of days with a price change")
        parser.add_argument("--frontier", type=int, default=200_000)
        parser.add_argument("--purge", action="store_true", help="delete previous synthetic data first")

    def step(self, label: str, sql: str, params: dict) -> None:
        start = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.rowcount
        self.stdout.write(f"  {label:<28} {rows:>10,} rows  {time.perf_counter() - start:6.1f}s")

    def handle(self, *args, **opts):
        source, _ = Source.objects.update_or_create(
            slug="synthetic",
            defaults={
                "name": "Synthetic benchmark data",
                "base_url": "https://synthetic.example/",
                "implementation": "books_toscrape",
                "enabled": False,
            },
        )
        Category.objects.bulk_create(
            [Category(name=n, slug=f"syn-{n.lower().replace(' ', '-')}") for n in CATEGORY_NAMES], ignore_conflicts=True
        )
        category_ids = list(Category.objects.filter(slug__startswith="syn-").values_list("id", flat=True))
        params = {
            "source_id": source.pk,
            "products": opts["products"],
            "days": opts["days"],
            "change_rate": opts["change_rate"],
            "frontier": opts["frontier"],
            "category_ids": category_ids,
            "n_categories": len(category_ids),
        }

        self.stdout.write(f"seeding synthetic data into source #{source.pk}")
        with transaction.atomic():
            # Bulk generation legitimately exceeds the 15s statement_timeout
            # guard rail; lift it for this transaction only.
            self.step("disable statement timeout", "SET LOCAL statement_timeout = 0", {})
            if opts["purge"]:
                self.step(
                    "purge price points",
                    "DELETE FROM catalog_pricepoint pp USING catalog_product p "
                    "WHERE pp.product_id = p.id AND p.source_id = %(source_id)s",
                    params,
                )
                self.step("purge products", "DELETE FROM catalog_product WHERE source_id = %(source_id)s", params)
                self.step("purge frontier", "DELETE FROM crawler_frontierurl WHERE source_id = %(source_id)s", params)
            self.step("products", PRODUCTS_SQL, params)
            self.step("price points", PRICE_POINTS_SQL, params)
            self.step("sync current prices", SYNC_CURRENT_PRICE_SQL, params)
            self.step("frontier urls", FRONTIER_SQL, params)
            # Fresh planner statistics, otherwise EXPLAIN shows plans for empty tables.

        # Outside the transaction (VACUUM can't run in one). After a bulk load this
        #  * refreshes planner statistics (else plans are made for empty tables),
        #  * merges the GIN indexes' fastupdate *pending lists* - until then the
        #    planner prices the search indexes as expensive and may seq-scan,
        #  * sets visibility-map bits, so index-only scans skip the heap.
        for table in ("catalog_product", "catalog_pricepoint", "crawler_frontierurl"):
            self.step(f"vacuum analyze {table}", f"VACUUM (ANALYZE) {table}", {})
        self.stdout.write(self.style.SUCCESS("done"))
