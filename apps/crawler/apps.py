from django.apps import AppConfig


class CrawlerConfig(AppConfig):
    name = "apps.crawler"
    label = "crawler"

    def ready(self) -> None:
        # Import source implementations so they register themselves.
        from . import sources  # noqa: F401
