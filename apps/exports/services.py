"""CSV exports generated with PostgreSQL ``COPY ... TO STDOUT``.

COPY streams rows straight out of the database in CSV format - no model
instantiation, no Python-level serialization - typically 10-50x faster than
iterating a QuerySet and writing rows with the csv module, at flat memory.
The file is written to a temp name and atomically renamed, so Nginx can never
serve a half-written export.
"""

import os
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import Export

EXPORT_SQL = """
COPY (
    SELECT p.id, s.slug AS source, p.external_id, p.title, p.brand, c.name AS category,
           p.currency, p.current_price, p.in_stock, p.stock_quantity, p.rating, p.url,
           p.price_changed_at, p.last_seen_at
    FROM catalog_product p
    JOIN crawler_source s ON s.id = p.source_id
    LEFT JOIN catalog_category c ON c.id = p.category_id
    WHERE {where}
    ORDER BY p.id
) TO STDOUT WITH (FORMAT csv, HEADER true)
"""

# Whitelisted filter fragments; only *values* come from the user and they are
# bound as parameters (psycopg merges them client-side for COPY).
FILTERS = {
    "source": "s.slug = %s",
    "category": "c.slug = %s",
    "in_stock": "p.in_stock = %s",
    "min_price": "p.current_price >= %s",
    "max_price": "p.current_price <= %s",
}


def export_path(file_name: str) -> Path:
    return Path(settings.EXPORTS_ROOT) / file_name


def build_query(filters: dict) -> tuple[str, list]:
    clauses, params = ["TRUE"], []
    for key, value in filters.items():
        if key in FILTERS and value not in (None, ""):
            clauses.append(FILTERS[key])
            params.append(value)
    return EXPORT_SQL.format(where=" AND ".join(clauses)), params


def run_export(export: Export) -> Export:
    Export.objects.filter(pk=export.pk).update(status=Export.Status.RUNNING)
    file_name = f"products-{timezone.now():%Y%m%d-%H%M%S}-{export.pk.hex[:8]}.csv"
    final_path = export_path(file_name)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_suffix(".csv.part")

    sql, params = build_query(export.filters)
    size = 0
    # cursor.cursor is the underlying psycopg 3 cursor, which exposes COPY.
    with connection.cursor() as cursor, open(tmp_path, "wb") as fh, cursor.cursor.copy(sql, params) as copy:
        for chunk in copy:
            fh.write(chunk)
            size += len(chunk)
    os.replace(tmp_path, final_path)

    with open(final_path, "rb") as fh:
        rows = max(sum(1 for _ in fh) - 1, 0)  # minus header

    export.status = Export.Status.READY
    export.file_name = file_name
    export.file_size = size
    export.row_count = rows
    export.finished_at = timezone.now()
    export.save(update_fields=["status", "file_name", "file_size", "row_count", "finished_at"])
    return export
