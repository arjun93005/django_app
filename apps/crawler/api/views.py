from django.db.models import OuterRef, Subquery
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import CrawlRun, Source


class CrawlRunSerializer(serializers.ModelSerializer):
    source = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = CrawlRun
        fields = [
            "id",
            "source",
            "status",
            "trigger",
            "started_at",
            "finished_at",
            "duration_seconds",
            "pages_fetched",
            "pages_not_modified",
            "items_scraped",
            "items_invalid",
            "products_created",
            "products_updated",
            "price_changes",
            "errors",
            "retries",
            "http_status",
        ]

    def get_duration_seconds(self, run) -> float | None:
        return run.duration.total_seconds() if run.duration else None


class SourceSerializer(serializers.ModelSerializer):
    last_run_status = serializers.CharField(read_only=True)

    class Meta:
        model = Source
        fields = [
            "slug",
            "name",
            "base_url",
            "enabled",
            "crawl_interval",
            "concurrency",
            "last_run_started_at",
            "last_run_status",
        ]


class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SourceSerializer
    lookup_field = "slug"
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        latest = CrawlRun.objects.filter(source=OuterRef("pk")).order_by("-started_at").values("status")[:1]
        return Source.objects.annotate(last_run_status=Subquery(latest))

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def crawl(self, request, slug=None):
        from ..tasks import start_crawl

        source = self.get_object()
        result = start_crawl.delay(source.pk, trigger=f"api:{request.user}")
        return Response({"queued": True, "task_id": result.id}, status=status.HTTP_202_ACCEPTED)


class CrawlRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CrawlRunSerializer
    queryset = CrawlRun.objects.select_related("source")
    filterset_fields = {"source__slug": ["exact"], "status": ["exact"]}
