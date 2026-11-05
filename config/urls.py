from django.contrib import admin
from django.urls import path

from apps.core.views import healthz, readyz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("admin/", admin.site.urls),
]
