from celery import shared_task
from django.utils import timezone

from .models import Export
from .services import run_export


@shared_task(ignore_result=True)
def generate_export(export_id: str) -> None:
    export = Export.objects.get(pk=export_id)
    try:
        run_export(export)
    except Exception as exc:
        Export.objects.filter(pk=export_id).update(
            status=Export.Status.FAILED, error=repr(exc)[:2000], finished_at=timezone.now()
        )
        raise
