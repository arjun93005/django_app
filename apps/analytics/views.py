from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.cache import cached_api_response

from .models import DailyCategoryStats
from .serializers import CheapestSerializer, PriceDropSerializer, TrendPointSerializer
from .services import biggest_price_drops, cheapest_per_category, overview


def _int_param(request, name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(request.query_params.get(name, default)), maximum))
    except ValueError as exc:
        raise ValidationError({name: "must be an integer"}) from exc


class OverviewView(APIView):
    @cached_api_response(namespace="catalog", timeout=60, edge_max_age=10)
    def get(self, request):
        return Response(overview())


class PriceDropsView(APIView):
    """GET /api/v1/analytics/price-drops/?days=7&min_pct=10&category=laptops"""

    @cached_api_response(namespace="catalog", timeout=300, edge_max_age=30)
    def get(self, request):
        try:
            min_pct = Decimal(request.query_params.get("min_pct", "5"))
        except InvalidOperation as exc:
            raise ValidationError({"min_pct": "must be a number"}) from exc
        rows = biggest_price_drops(
            days=_int_param(request, "days", 7, 365),
            min_drop_pct=min_pct,
            category=request.query_params.get("category") or None,
            limit=_int_param(request, "limit", 50, 500),
        )
        return Response({"count": len(rows), "results": PriceDropSerializer(rows, many=True).data})


class CheapestPerCategoryView(APIView):
    @cached_api_response(namespace="catalog", timeout=300, edge_max_age=30)
    def get(self, request):
        n = _int_param(request, "n", 3, 20)
        rows = cheapest_per_category(per_category=n)
        return Response({"per_category": n, "results": CheapestSerializer(rows, many=True).data})


class CategoryTrendView(APIView):
    """Daily series from the materialized view - a cheap indexed range read."""

    @cached_api_response(namespace="analytics", timeout=900, edge_max_age=60)
    def get(self, request, slug: str):
        days = _int_param(request, "days", 30, 180)
        since = timezone.localdate() - timedelta(days=days)
        rows = (
            DailyCategoryStats.objects.filter(category__slug=slug, day__gte=since)
            .order_by("day")
            .values("day", "products_tracked", "avg_price", "min_price", "max_price", "price_drops", "price_increases")
        )
        return Response({"category": slug, "days": days, "series": TrendPointSerializer(rows, many=True).data})
