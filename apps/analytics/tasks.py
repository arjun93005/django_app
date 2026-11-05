from celery import shared_task

from apps.core.cache import bump_namespace

from .services import refresh_daily_category_stats as _refresh


@shared_task(ignore_result=True)
def refresh_daily_category_stats() -> bool:
    refreshed = _refresh()
    if refreshed:
        bump_namespace("analytics")
    return refreshed
