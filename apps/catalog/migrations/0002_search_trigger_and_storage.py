"""Database-side search vector maintenance + storage tuning for catalog_product.

Why a trigger and not a GeneratedField / save() override?
  * Products are written in bulk with INSERT ... ON CONFLICT DO UPDATE, which
    bypasses Model.save() and signals.
  * A trigger lets us skip recomputing the tsvector when the text did not
    change (WHEN clause on the UPDATE trigger) - every crawl re-upserts every
    product, but titles almost never change.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION catalog_product_search_vector() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_vector :=
          setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A')
       || setweight(to_tsvector('english', coalesce(NEW.brand, '')), 'B')
       || setweight(to_tsvector('english', coalesce(NEW.description, '')), 'C');
    RETURN NEW;
END
$$;

CREATE TRIGGER catalog_product_search_vector_insert
    BEFORE INSERT ON catalog_product
    FOR EACH ROW EXECUTE FUNCTION catalog_product_search_vector();

-- OLD is not available to INSERT triggers, hence the separate UPDATE trigger
-- that only fires when one of the indexed text columns really changed.
CREATE TRIGGER catalog_product_search_vector_update
    BEFORE UPDATE OF title, brand, description ON catalog_product
    FOR EACH ROW
    WHEN (OLD.title IS DISTINCT FROM NEW.title
       OR OLD.brand IS DISTINCT FROM NEW.brand
       OR OLD.description IS DISTINCT FROM NEW.description)
    EXECUTE FUNCTION catalog_product_search_vector();

-- Every crawl rewrites last_seen_at for every product. Leaving 15% free space
-- per page lets Postgres do HOT (heap-only tuple) updates - the new row
-- version goes on the same page and no index needs a new entry, as long as
-- no indexed column changed value.
ALTER TABLE catalog_product SET (fillfactor = 85);

-- price history is append-only: pack pages completely.
ALTER TABLE catalog_pricepoint SET (fillfactor = 100);

UPDATE catalog_product
   SET search_vector = setweight(to_tsvector('english', coalesce(title, '')), 'A')
                    || setweight(to_tsvector('english', coalesce(brand, '')), 'B')
                    || setweight(to_tsvector('english', coalesce(description, '')), 'C');
"""

BACKWARD = r"""
DROP TRIGGER IF EXISTS catalog_product_search_vector_update ON catalog_product;
DROP TRIGGER IF EXISTS catalog_product_search_vector_insert ON catalog_product;
DROP FUNCTION IF EXISTS catalog_product_search_vector();
ALTER TABLE catalog_product RESET (fillfactor);
ALTER TABLE catalog_pricepoint RESET (fillfactor);
"""


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
