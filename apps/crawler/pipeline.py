"""Batched ingestion of scraped items into the catalog.

Per flush (a few hundred items) the pipeline issues a constant number of SQL
statements, regardless of batch size:

1. Resolve categories        - one INSERT ... ON CONFLICT DO NOTHING + one SELECT
2. Upsert products           - one INSERT ... SELECT FROM unnest(arrays) ON CONFLICT DO UPDATE
3. Record price changes      - one INSERT ... SELECT with a LATERAL "latest price" lookup

Why raw SQL here instead of ``bulk_create(update_conflicts=True)``?
  * ``price_changed_at`` must only move when the price actually changed - that
    needs a CASE over the *existing* row inside DO UPDATE.
  * ``RETURNING (xmax = 0)`` tells inserted rows from updated ones (xmax is 0
    for a freshly inserted tuple), giving accurate created/updated counters.
  * Passing columns as arrays and ``unnest``-ing them sends one statement with
    13 parameters instead of 13 x N placeholders.
"""

import logging
import threading
from dataclasses import dataclass

from django.db import connection, transaction
from django.utils import timezone
from django.utils.text import slugify
from psycopg.types.json import Jsonb

from apps.catalog.models import Category

from .items import ProductItem
from .models import CrawlRun, Source

logger = logging.getLogger(__name__)

UPSERT_PRODUCTS_SQL = """
INSERT INTO catalog_product AS p (
    source_id, external_id, url, title, brand, description, category_id, image_url,
    rating, currency, current_price, in_stock, stock_quantity, attributes,
    first_seen_at, last_seen_at, price_changed_at
)
SELECT %(source_id)s, v.external_id, v.url, v.title, v.brand, v.description, v.category_id, v.image_url,
       v.rating, v.currency, v.price, v.in_stock, v.stock_quantity, v.attributes,
       %(now)s, %(now)s, %(now)s
FROM unnest(
    %(external_ids)s::text[], %(urls)s::text[], %(titles)s::text[], %(brands)s::text[],
    %(descriptions)s::text[], %(category_ids)s::bigint[], %(image_urls)s::text[],
    %(ratings)s::smallint[], %(currencies)s::text[], %(prices)s::numeric[],
    %(in_stocks)s::boolean[], %(stock_quantities)s::integer[], %(attributes)s::jsonb[]
) AS v(external_id, url, title, brand, description, category_id, image_url,
       rating, currency, price, in_stock, stock_quantity, attributes)
ON CONFLICT (source_id, external_id) DO UPDATE SET
    url              = EXCLUDED.url,
    title            = EXCLUDED.title,
    brand            = EXCLUDED.brand,
    description      = EXCLUDED.description,
    category_id      = coalesce(EXCLUDED.category_id, p.category_id),
    image_url        = EXCLUDED.image_url,
    rating           = EXCLUDED.rating,
    currency         = EXCLUDED.currency,
    in_stock         = EXCLUDED.in_stock,
    stock_quantity   = EXCLUDED.stock_quantity,
    attributes       = EXCLUDED.attributes,
    -- every SET expression sees the *old* row, so ordering doesn't matter
    price_changed_at = CASE WHEN p.current_price IS DISTINCT FROM EXCLUDED.current_price
                            THEN EXCLUDED.last_seen_at ELSE p.price_changed_at END,
    current_price    = EXCLUDED.current_price,
    last_seen_at     = EXCLUDED.last_seen_at
RETURNING p.id, p.external_id, (p.xmax = 0) AS inserted
"""

INSERT_PRICE_CHANGES_SQL = """
INSERT INTO catalog_pricepoint (product_id, price, currency, in_stock, observed_at, crawl_run_id)
SELECT v.product_id, v.price, v.currency, v.in_stock, %(now)s, %(run_id)s
FROM unnest(%(product_ids)s::bigint[], %(prices)s::numeric[], %(currencies)s::text[], %(in_stocks)s::boolean[])
     AS v(product_id, price, currency, in_stock)
LEFT JOIN LATERAL (
    -- index-only scan on pricepoint_product_latest (product_id, observed_at DESC) INCLUDE (price, in_stock)
    SELECT pp.price, pp.in_stock
    FROM catalog_pricepoint pp
    WHERE pp.product_id = v.product_id
    ORDER BY pp.observed_at DESC
    LIMIT 1
) AS last ON TRUE
WHERE last.price IS DISTINCT FROM v.price
   OR last.in_stock IS DISTINCT FROM v.in_stock
"""


@dataclass
class FlushResult:
    created: int = 0
    updated: int = 0
    price_changes: int = 0


class IngestionPipeline:
    """Buffers items and writes them in batches. Thread-safe."""

    def __init__(self, source: Source, run: CrawlRun | None, *, flush_every: int = 200):
        self.source = source
        self.run = run
        self.flush_every = flush_every
        self._buffer: dict[str, ProductItem] = {}
        self._lock = threading.Lock()
        self._category_ids: dict[str, int] = {}
        self.totals = FlushResult()

    def add(self, items: list[ProductItem]) -> FlushResult | None:
        with self._lock:
            for item in items:
                # De-duplicate within a batch: ON CONFLICT DO UPDATE raises
                # "cannot affect row a second time" if one statement touches
                # the same key twice.
                self._buffer[item.external_id] = item
            if len(self._buffer) < self.flush_every:
                return None
            batch, self._buffer = list(self._buffer.values()), {}
        return self._write(batch)

    def flush(self) -> FlushResult:
        with self._lock:
            batch, self._buffer = list(self._buffer.values()), {}
        return self._write(batch) if batch else FlushResult()

    # ------------------------------------------------------------------
    def _resolve_categories(self, names: set[str]) -> dict[str, int]:
        missing = {n for n in names if n not in self._category_ids}
        if missing:
            by_slug = {slugify(n)[:140] or "uncategorised": n for n in missing}
            Category.objects.bulk_create(
                [Category(slug=slug, name=name[:120]) for slug, name in by_slug.items()],
                ignore_conflicts=True,
            )
            slug_to_id = dict(Category.objects.filter(slug__in=by_slug).values_list("slug", "id"))
            for slug, name in by_slug.items():
                self._category_ids[name] = slug_to_id[slug]
        return self._category_ids

    def _write(self, items: list[ProductItem]) -> FlushResult:
        # Concurrent flushes lock product rows in key order, so two workers
        # upserting overlapping batches can't deadlock each other.
        items = sorted(items, key=lambda i: i.external_id)
        now = timezone.now()
        categories = self._resolve_categories({i.category for i in items if i.category})
        result = FlushResult()

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                UPSERT_PRODUCTS_SQL,
                {
                    "source_id": self.source.pk,
                    "now": now,
                    "external_ids": [i.external_id for i in items],
                    "urls": [i.url for i in items],
                    "titles": [i.title for i in items],
                    "brands": [i.brand for i in items],
                    "descriptions": [i.description for i in items],
                    "category_ids": [categories.get(i.category) if i.category else None for i in items],
                    "image_urls": [i.image_url for i in items],
                    "ratings": [i.rating for i in items],
                    "currencies": [i.currency for i in items],
                    "prices": [i.price for i in items],
                    "in_stocks": [i.in_stock for i in items],
                    "stock_quantities": [i.stock_quantity for i in items],
                    "attributes": [Jsonb(i.attributes) for i in items],
                },
            )
            returned = cursor.fetchall()
            ids = {external_id: pk for pk, external_id, _ in returned}
            result.created = sum(1 for *_, inserted in returned if inserted)
            result.updated = len(returned) - result.created

            cursor.execute(
                INSERT_PRICE_CHANGES_SQL,
                {
                    "now": now,
                    "run_id": self.run.pk if self.run else None,
                    "product_ids": [ids[i.external_id] for i in items],
                    "prices": [i.price for i in items],
                    "currencies": [i.currency for i in items],
                    "in_stocks": [i.in_stock for i in items],
                },
            )
            result.price_changes = cursor.rowcount

        self.totals.created += result.created
        self.totals.updated += result.updated
        self.totals.price_changes += result.price_changes
        logger.debug("flushed %d items: %s", len(items), result)
        return result
