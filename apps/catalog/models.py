from django.contrib.postgres.indexes import BrinIndex, GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return self.name


class ProductQuerySet(models.QuerySet):
    def with_previous_price(self):
        """Annotate the price before the latest change (correlated subquery).

        Served by the (product_id, observed_at DESC) INCLUDE (price) index, so
        it is an index-only lookup per row - fine for a 50-row API page.
        """
        previous = PricePoint.objects.filter(product=OuterRef("pk")).order_by("-observed_at").values("price")[1:2]
        return self.annotate(previous_price=Subquery(previous))


class Product(models.Model):
    # The unique (source, external_id) constraint already provides an index
    # whose leading column is source_id, so skip Django's automatic FK index.
    source = models.ForeignKey("crawler.Source", on_delete=models.PROTECT, related_name="products", db_index=False)
    external_id = models.CharField(max_length=128, help_text="SKU / UPC in the source system")
    url = models.URLField(max_length=600)
    title = models.CharField(max_length=300)
    brand = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="products")
    image_url = models.URLField(max_length=600, blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    current_price = models.DecimalField(max_digits=12, decimal_places=2)
    in_stock = models.BooleanField(default=True)
    stock_quantity = models.IntegerField(null=True, blank=True)
    attributes = models.JSONField(default=dict, blank=True)

    # Maintained by a PL/pgSQL trigger (migration 0002) rather than Python:
    # the ingestion pipeline writes with INSERT ... ON CONFLICT, which bypasses
    # Model.save() and signals, so only a database-side trigger stays correct.
    search_vector = SearchVectorField(null=True, editable=False)

    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    price_changed_at = models.DateTimeField(default=timezone.now)

    objects = ProductQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="product_unique_per_source"),
            models.CheckConstraint(condition=Q(current_price__gte=0), name="product_price_non_negative"),
            models.CheckConstraint(
                condition=Q(rating__isnull=True) | Q(rating__gte=1, rating__lte=5),
                name="product_rating_1_to_5",
            ),
        ]
        indexes = [
            GinIndex(fields=["search_vector"], name="product_search_gin"),
            # pg_trgm index: typo-tolerant search and fast ILIKE '%term%'.
            GinIndex(fields=["title"], opclasses=["gin_trgm_ops"], name="product_title_trgm"),
            # jsonb_path_ops: smaller/faster GIN that supports the @> operator.
            GinIndex(fields=["attributes"], opclasses=["jsonb_path_ops"], name="product_attributes_gin"),
            # Partial index for the common "cheapest in-stock items in a category" access path.
            models.Index(
                fields=["category", "current_price"],
                condition=Q(in_stock=True),
                name="product_instock_cat_price",
            ),
            models.Index(fields=["-price_changed_at"], name="product_price_changed"),
        ]

    def __str__(self) -> str:
        return self.title


class PricePoint(models.Model):
    """Append-only price history.

    A row is written only when price or availability *changes* (the pipeline
    does the change detection in SQL), so the table grows with the number of
    changes, not with the number of crawls. Consumers must treat it as a step
    function ("price as of time T" = last point at or before T).
    """

    # Covered by the composite index below; a separate FK index would only
    # slow down this insert-heavy table.
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_points", db_index=False)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    in_stock = models.BooleanField()
    observed_at = models.DateTimeField(default=timezone.now)
    crawl_run = models.ForeignKey(
        "crawler.CrawlRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+", db_index=False
    )

    class Meta:
        get_latest_by = "observed_at"
        indexes = [
            # "latest price of product X": a backwards index scan that never
            # touches the heap thanks to INCLUDE (index-only scan).
            models.Index(
                fields=["product", "-observed_at"],
                include=["price", "in_stock"],
                name="pricepoint_product_latest",
            ),
            # Rows arrive in time order, so a BRIN index (a few kB for millions
            # of rows) is enough for time-range scans in analytics.
            BrinIndex(fields=["observed_at"], autosummarize=True, name="pricepoint_observed_brin"),
        ]

    def __str__(self) -> str:
        return f"{self.product_id} {self.price} @ {self.observed_at:%Y-%m-%d %H:%M}"
