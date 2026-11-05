from django.contrib import admin

from .models import Category, PricePoint, Product


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name",)


class PricePointInline(admin.TabularInline):
    model = PricePoint
    extra = 0
    fields = ("price", "currency", "in_stock", "observed_at")
    readonly_fields = fields
    ordering = ("-observed_at",)
    max_num = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "category", "current_price", "currency", "in_stock", "price_changed_at")
    list_filter = ("source", "in_stock", "category")
    list_select_related = ("source", "category")  # avoid N+1 on the changelist
    search_fields = ("title", "external_id")
    raw_id_fields = ("category",)
    readonly_fields = ("search_vector", "first_seen_at", "last_seen_at", "price_changed_at")
    inlines = [PricePointInline]
    show_full_result_count = False
