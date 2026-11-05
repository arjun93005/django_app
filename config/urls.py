from django.contrib import admin
from django.urls import include, path

from apps.core.views import healthz, readyz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.catalog.api.urls")),
    path("api/v1/crawler/", include("apps.crawler.api.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
]
