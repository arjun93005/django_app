from django.contrib import admin

from .models import Export


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = ("id", "requested_by", "status", "row_count", "file_size", "created_at", "finished_at")
    list_filter = ("status",)
    list_select_related = ("requested_by",)
