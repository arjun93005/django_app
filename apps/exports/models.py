import uuid

from django.conf import settings
from django.db import models


class Export(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        READY = "ready"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exports")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    filters = models.JSONField(default=dict, blank=True)
    file_name = models.CharField(max_length=200, blank=True)
    row_count = models.PositiveIntegerField(null=True, blank=True)
    file_size = models.PositiveBigIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"export {self.id} ({self.status})"
