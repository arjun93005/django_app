from datetime import timedelta

from django.db import migrations

SOURCES = [
    {
        "slug": "books_toscrape",
        "name": "Books to Scrape",
        "base_url": "https://books.toscrape.com/",
        "implementation": "books_toscrape",
        "crawl_interval": timedelta(hours=12),
        "concurrency": 4,
        "rate_limit_per_host": 4.0,
    },
    {
        "slug": "demo_shop",
        "name": "DemoShop (SPA with signed private API)",
        "base_url": "http://127.0.0.1:5050/",
        "implementation": "demo_shop",
        "crawl_interval": timedelta(hours=1),
        "concurrency": 1,
        "rate_limit_per_host": 8.0,
    },
]


def create_sources(apps, schema_editor):
    Source = apps.get_model("crawler", "Source")
    for spec in SOURCES:
        Source.objects.update_or_create(slug=spec["slug"], defaults=spec)


def delete_sources(apps, schema_editor):
    apps.get_model("crawler", "Source").objects.filter(slug__in=[s["slug"] for s in SOURCES]).delete()


class Migration(migrations.Migration):
    dependencies = [("crawler", "0001_initial")]

    operations = [migrations.RunPython(create_sources, delete_sources)]
