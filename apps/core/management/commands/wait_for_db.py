import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = "Block until the default database accepts connections (used by the container entrypoint)."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=60)

    def handle(self, *args, timeout: int, **options):
        deadline = time.monotonic() + timeout
        delay = 0.5
        while True:
            try:
                connections["default"].ensure_connection()
                self.stdout.write(self.style.SUCCESS("database is ready"))
                return
            except OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"database not ready after {timeout}s: {exc}") from exc
                self.stdout.write(f"waiting for database ({exc.__class__.__name__}) ...")
                time.sleep(delay)
                delay = min(delay * 2, 5)
