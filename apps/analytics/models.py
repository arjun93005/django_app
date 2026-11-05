from django.db import models


class DailyCategoryStats(models.Model):
    """Read-only model over the ``analytics_daily_category_stats`` materialized view.

    The view (migration 0001) rolls the change-only price history up into one
    row per category per day, *forward-filling* prices so a product that did
    not change on a given day still counts with its last known price. It is
    refreshed CONCURRENTLY after each crawl, so dashboards read a precomputed
    table instead of scanning the price history on every request.
    """

    id = models.TextField(primary_key=True)  # "<category_id>:<day>"
    category = models.ForeignKey("catalog.Category", on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    day = models.DateField()
    products_tracked = models.IntegerField()
    avg_price = models.DecimalField(max_digits=12, decimal_places=2)
    min_price = models.DecimalField(max_digits=12, decimal_places=2)
    max_price = models.DecimalField(max_digits=12, decimal_places=2)
    price_drops = models.IntegerField()
    price_increases = models.IntegerField()

    class Meta:
        managed = False
        db_table = "analytics_daily_category_stats"
        ordering = ["day"]

    def __str__(self) -> str:
        return self.id
