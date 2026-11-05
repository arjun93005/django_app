"""Load test for the read API (run against Nginx, not runserver).

    locust -f loadtest/locustfile.py --host http://localhost:8080 \
           --headless -u 100 -r 20 -t 60s --csv loadtest/results/cached

Two user classes (pick with `locust ... BrowsingUser` / `CacheBustingUser`):

* ``BrowsingUser`` - realistic anonymous traffic. Popular pages repeat, so the
  Nginx micro-cache and the Redis tier absorb most of it.
* ``CacheBustingUser`` - adds a unique query param to every request, defeating
  both cache tiers: measures raw uWSGI + Django + PostgreSQL throughput.

Watch alongside: `uwsgitop 127.0.0.1:9191` (worker busyness), the Nginx JSON
access log (`cache` and `upstream_time` fields) and pg_stat_statements.
"""

import random
import uuid
from urllib.parse import urlencode

from locust import FastHttpUser, constant, task

SEARCH_TERMS = ["wireless headphones", "keyboard", "gaming monitor", "laptop pro", "router", "keybaord", "camera"]
CATEGORIES = ["syn-audio", "syn-laptops", "syn-monitors", "syn-peripherals", "syn-wearables"]


class BrowsingUser(FastHttpUser):
    wait_time = constant(0)
    cache_bust = False
    product_ids: list[int] = []

    def _get(self, path: str, name: str, **params):
        if self.cache_bust:
            params["_"] = uuid.uuid4().hex
        return self.client.get(f"{path}?{urlencode(params)}" if params else path, name=name)

    def on_start(self):
        response = self._get("/api/v1/products/", "list", page_size=100)
        if response.status_code == 200:
            self.product_ids = [p["id"] for p in response.json()["results"]]

    @task(6)
    def list_products(self):
        self._get("/api/v1/products/", "list", category=random.choice(CATEGORIES))

    @task(4)
    def product_detail(self):
        if self.product_ids:
            self._get(f"/api/v1/products/{random.choice(self.product_ids)}/", "detail")

    @task(3)
    def search(self):
        self._get("/api/v1/products/search/", "search", q=random.choice(SEARCH_TERMS))

    @task(2)
    def categories(self):
        self._get("/api/v1/categories/", "categories")

    @task(1)
    def price_drops(self):
        self._get("/api/v1/analytics/price-drops/", "price-drops", days=7, min_pct=10)


class CacheBustingUser(BrowsingUser):
    cache_bust = True
