"""Catalog API.

Query-count discipline: every endpoint issues a constant number of SQL
queries regardless of page size (enforced in tests/test_api.py with
``django_assert_num_queries``):

* list    -> 1 query: JOINs via select_related, ``.only()`` to skip the wide
             description/attributes/search_vector columns, previous price
             via a correlated subquery. CursorPagination needs no COUNT(*).
* detail  -> 2 queries: product + a *sliced* Prefetch of the last 30 price
             points (Django turns the slice into a ROW_NUMBER() window).
"""

from datetime import timedelta

from django.db.models import Avg, Count, Prefetch, Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from apps.core.cache import cached_api_response

from ..models import Category, PricePoint, Product
from ..search import search_products
from .filters import ProductFilter
from .serializers import (
    CategorySerializer,
    PricePointSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
    ProductSearchSerializer,
)

LIST_FIELDS = (
    "id",
    "external_id",
    "title",
    "brand",
    "url",
    "image_url",
    "currency",
    "current_price",
    "in_stock",
    "rating",
    "price_changed_at",
    "source__slug",
    "category__slug",
)


class ProductCursorPagination(CursorPagination):
    """Keyset pagination: ``WHERE id < last_seen ORDER BY id DESC LIMIT n``.

    Cost is constant however deep the client pages (OFFSET pagination scans and
    discards every skipped row) and results stay stable while crawls insert
    new products concurrently.
    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
    ordering = "-id"


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    filterset_class = ProductFilter
    pagination_class = ProductCursorPagination

    def get_queryset(self):
        qs = Product.objects.select_related("source", "category")
        if self.action in ("list", "search"):
            return qs.only(*LIST_FIELDS).with_previous_price()
        if self.action == "retrieve":
            recent = PricePoint.objects.order_by("-observed_at")[:30]
            return qs.with_previous_price().prefetch_related(
                Prefetch("price_points", queryset=recent, to_attr="recent_price_points")
            )
        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ProductDetailSerializer
        return ProductListSerializer

    @cached_api_response(namespace="catalog", timeout=120, edge_max_age=5)
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @cached_api_response(namespace="catalog", timeout=300, edge_max_age=10)
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @action(detail=False)
    @cached_api_response(namespace="catalog", timeout=120, edge_max_age=5)
    def search(self, request):
        """GET /products/search/?q=wireless headphones&category=headphones"""
        queryset = self.filter_queryset(self.get_queryset())
        results, strategy = search_products(queryset, request.query_params.get("q", ""))
        return Response(
            {"strategy": strategy, "count": len(results), "results": ProductSearchSerializer(results, many=True).data}
        )

    @action(detail=True)
    def history(self, request, pk=None):
        """Price history as a step series.

        Points are stored only on change, so the series starts with the last
        point *before* the window (the price in effect at its start); without
        it a chart would begin at the first change instead of the window edge.
        """
        try:
            days = min(int(request.query_params.get("days", 90)), 3650)
        except ValueError as exc:
            raise ValidationError({"days": "must be an integer"}) from exc
        product = self.get_object()
        since = timezone.now() - timedelta(days=days)
        points = product.price_points.all()
        anchor = points.filter(observed_at__lt=since).order_by("-observed_at")[:1]
        window = points.filter(observed_at__gte=since).order_by("observed_at")
        series = [*anchor, *window]
        return Response(
            {
                "product": product.pk,
                "days": days,
                "points": PricePointSerializer(series, many=True).data,
            }
        )


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CategorySerializer
    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        # One GROUP BY query; conditional aggregation compiles to
        # count(*) FILTER (WHERE in_stock) in PostgreSQL.
        return Category.objects.annotate(
            product_count=Count("products"),
            in_stock_count=Count("products", filter=Q(products__in_stock=True)),
            avg_price=Avg("products__current_price"),
        ).order_by("name")

    @cached_api_response(namespace="catalog", timeout=300, edge_max_age=30)
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
