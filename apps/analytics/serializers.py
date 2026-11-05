"""Output serializers: keep money as fixed-point strings, like the catalog API."""

from rest_framework import serializers


def Money(**kwargs) -> serializers.DecimalField:  # reads like a field class at the call site
    return serializers.DecimalField(max_digits=12, decimal_places=2, **kwargs)


class TrendPointSerializer(serializers.Serializer):
    day = serializers.DateField()
    products_tracked = serializers.IntegerField()
    avg_price = Money()
    min_price = Money()
    max_price = Money()
    price_drops = serializers.IntegerField()
    price_increases = serializers.IntegerField()


class PriceDropSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    currency = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    source = serializers.CharField()
    previous_price = Money()
    price = Money()
    change_pct = serializers.DecimalField(max_digits=7, decimal_places=2)
    observed_at = serializers.DateTimeField()


class CheapestSerializer(serializers.Serializer):
    category = serializers.CharField(source="category.slug")
    position = serializers.IntegerField()
    id = serializers.IntegerField()
    title = serializers.CharField()
    price = Money(source="current_price")
    currency = serializers.CharField()
