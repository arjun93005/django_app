from decimal import Decimal

from rest_framework import serializers

from ..models import Category, PricePoint, Product


class CategorySerializer(serializers.ModelSerializer):
    product_count = serializers.IntegerField(read_only=True)
    in_stock_count = serializers.IntegerField(read_only=True)
    avg_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "product_count", "in_stock_count", "avg_price"]


class PricePointSerializer(serializers.ModelSerializer):
    class Meta:
        model = PricePoint
        fields = ["price", "currency", "in_stock", "observed_at"]


class ProductListSerializer(serializers.ModelSerializer):
    # These read from select_related() joins - no extra queries per row.
    source = serializers.CharField(source="source.slug", read_only=True)
    category = serializers.CharField(source="category.slug", default=None, read_only=True)
    previous_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=None)
    price_change_pct = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "source",
            "external_id",
            "title",
            "brand",
            "category",
            "url",
            "image_url",
            "currency",
            "current_price",
            "previous_price",
            "price_change_pct",
            "in_stock",
            "rating",
            "price_changed_at",
        ]

    def get_price_change_pct(self, product) -> float | None:
        previous = getattr(product, "previous_price", None)
        if not previous:
            return None
        return float(((product.current_price - previous) / previous * 100).quantize(Decimal("0.01")))


class ProductSearchSerializer(ProductListSerializer):
    rank = serializers.FloatField(read_only=True)

    class Meta(ProductListSerializer.Meta):
        fields = [*ProductListSerializer.Meta.fields, "rank"]


class ProductDetailSerializer(ProductListSerializer):
    recent_prices = PricePointSerializer(many=True, read_only=True, source="recent_price_points")

    class Meta(ProductListSerializer.Meta):
        fields = [
            *ProductListSerializer.Meta.fields,
            "description",
            "attributes",
            "stock_quantity",
            "first_seen_at",
            "last_seen_at",
            "recent_prices",
        ]
