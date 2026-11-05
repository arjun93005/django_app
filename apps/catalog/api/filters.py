import django_filters
from rest_framework.exceptions import ValidationError

from ..models import Product


class ProductFilter(django_filters.FilterSet):
    category = django_filters.CharFilter(field_name="category__slug")
    source = django_filters.CharFilter(field_name="source__slug")
    brand = django_filters.CharFilter(field_name="brand", lookup_expr="iexact")
    min_price = django_filters.NumberFilter(field_name="current_price", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="current_price", lookup_expr="lte")
    in_stock = django_filters.BooleanFilter()
    changed_since = django_filters.IsoDateTimeFilter(field_name="price_changed_at", lookup_expr="gte")
    attr = django_filters.CharFilter(method="filter_attribute", help_text="key:value, e.g. color:black")

    class Meta:
        model = Product
        fields = ["category", "source", "brand", "in_stock"]

    def filter_attribute(self, queryset, name, value):
        key, sep, raw = value.partition(":")
        if not sep or not key:
            raise ValidationError({"attr": "expected key:value"})
        parsed: object = int(raw) if raw.lstrip("-").isdigit() else raw
        # JSONB containment (@>) is served by the jsonb_path_ops GIN index.
        return queryset.filter(attributes__contains={key: parsed})
