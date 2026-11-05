"""Export API.

Downloads use the *X-Accel-Redirect* pattern: Django authenticates and
authorises the request, then responds with an empty body and a header that
tells Nginx to serve ``/protected-exports/<file>`` from an ``internal``
location. The uWSGI worker is released in milliseconds instead of being tied
up streaming a large file to a slow client; Nginx handles sendfile, range
requests and slow clients efficiently. Without Nginx (dev) we fall back to
Django's FileResponse.
"""

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from rest_framework import mixins, permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Export
from .services import FILTERS, export_path
from .tasks import generate_export


class ExportSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Export
        fields = [
            "id",
            "status",
            "filters",
            "row_count",
            "file_size",
            "error",
            "created_at",
            "finished_at",
            "download_url",
        ]
        read_only_fields = ["status", "row_count", "file_size", "error", "created_at", "finished_at"]

    def validate_filters(self, value):
        unknown = set(value) - set(FILTERS)
        if unknown:
            raise serializers.ValidationError(f"unsupported filters: {sorted(unknown)}")
        return value

    def get_download_url(self, export) -> str | None:
        if export.status != Export.Status.READY:
            return None
        return self.context["request"].build_absolute_uri(f"/api/v1/exports/{export.pk}/download/")


class ExportViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = ExportSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "exports"

    def get_queryset(self):
        return Export.objects.filter(requested_by=self.request.user)

    def perform_create(self, serializer):
        export = serializer.save(requested_by=self.request.user)
        # Only enqueue once the row is committed, or the worker may not see it.
        transaction.on_commit(lambda: generate_export.delay(str(export.pk)))

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        response.status_code = status.HTTP_202_ACCEPTED
        return response

    @action(detail=True)
    def download(self, request, pk=None):
        export = self.get_object()  # scoped to the requesting user
        if export.status != Export.Status.READY:
            return Response({"detail": f"export is {export.status}"}, status=status.HTTP_409_CONFLICT)

        disposition = f'attachment; filename="{export.file_name}"'
        if settings.EXPORTS_USE_X_ACCEL:
            response = HttpResponse(content_type="text/csv")
            response["X-Accel-Redirect"] = f"{settings.EXPORTS_X_ACCEL_PREFIX}{export.file_name}"
            response["Content-Disposition"] = disposition
            return response

        path = export_path(export.file_name)
        if not path.exists():
            raise Http404("export file missing")
        return FileResponse(open(path, "rb"), as_attachment=True, filename=export.file_name, content_type="text/csv")
