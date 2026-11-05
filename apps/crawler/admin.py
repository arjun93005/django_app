from django.contrib import admin, messages

from .models import CrawlRun, FrontierURL, Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "implementation", "enabled", "crawl_interval", "concurrency", "last_run_started_at")
    list_editable = ("enabled",)
    actions = ["crawl_now"]

    @admin.action(description="Start a crawl now (Celery)")
    def crawl_now(self, request, queryset):
        from .tasks import start_crawl

        for source in queryset:
            start_crawl.delay(source.pk, trigger=f"admin:{request.user}")
        self.message_user(request, f"queued {queryset.count()} crawl(s)", messages.SUCCESS)


@admin.register(CrawlRun)
class CrawlRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "status",
        "trigger",
        "started_at",
        "finished_at",
        "pages_fetched",
        "items_scraped",
        "products_created",
        "price_changes",
        "errors",
    )
    list_filter = ("status", "source")
    list_select_related = ("source",)
    readonly_fields = [f.name for f in CrawlRun._meta.fields]


@admin.register(FrontierURL)
class FrontierURLAdmin(admin.ModelAdmin):
    list_display = ("url", "source", "kind", "status", "attempts", "last_status_code", "last_crawled_at")
    list_filter = ("status", "kind", "source")
    search_fields = ("url",)
    # The frontier can hold millions of rows: never COUNT(*) it for the changelist.
    show_full_result_count = False
