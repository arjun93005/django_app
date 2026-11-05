# PriceLens

[![CI](https://github.com/arjun93005/django_app/actions/workflows/ci.yml/badge.svg)](https://github.com/arjun93005/django_app/actions/workflows/ci.yml)

A product & price-intelligence platform: crawlers collect products and prices from
e-commerce sites (including a JavaScript SPA whose private API had to be reverse
engineered), PostgreSQL stores the catalogue and its price history, and a Django REST
API serves it through **Nginx → uWSGI**, built to scale horizontally.

```
                ┌──────────────── docker compose ─────────────────────────────────────────┐
 client ──────► │ Nginx ──uwsgi──► uWSGI (N workers) ─► Django ─► PgBouncer ─► PostgreSQL │
                │  micro-cache        lazy-apps, cheaper   │            ▲                  │
                │  rate limits        chain reload         ▼            │                  │
                │  X-Accel files                         Redis ◄── Celery beat / workers   │
                │                                     (cache+broker)     │                 │
                │                                                       ├─► httpx ──► sites │
                │                                                       └─► Selenium Grid  │
                └──────────────────────────────────────────────────────────────────────────┘
```

| Area | What is implemented (and where) |
|---|---|
| **Django at scale with uWSGI + Nginx** | uWSGI over the binary *uwsgi protocol*, `lazy-apps`, `thunder-lock`, adaptive *cheaper/busyness* workers, harakiri, `touch-chain-reload` zero-downtime deploys, graceful SIGTERM ([deploy/uwsgi/uwsgi.ini](deploy/uwsgi/uwsgi.ini)). Nginx micro-cache with stampede lock + stale-while-revalidate, per-IP rate limits, `X-Accel-Redirect` downloads, immutable hashed static files ([deploy/nginx](deploy/nginx)). Two-tier cache with O(1) invalidation ([apps/core/cache.py](apps/core/cache.py)), `Server-Timing` + request-id tracing ([apps/core/middleware.py](apps/core/middleware.py)), N+1-free endpoints enforced by query-count tests. |
| **Reverse engineering + robust crawlers** | CDP network capture to discover a private API ([apps/crawler/discovery.py](apps/crawler/discovery.py)); Python port of the site's obfuscated HMAC request signing, verified byte-for-byte against captured browser traffic; Selenium only where JavaScript is unavoidable (JS-challenge session bootstrap, DOM fallback) and plain HTTP for bulk data ([apps/crawler/sources/demo_shop.py](apps/crawler/sources/demo_shop.py)). Retries with jittered backoff + `Retry-After`, adaptive per-host rate limiting, robots.txt, conditional GETs, checkpoint/resume, self-healing sessions and clock skew ([apps/crawler/http.py](apps/crawler/http.py)). |
| **PostgreSQL / SQL** | URL frontier as a work queue with `FOR UPDATE SKIP LOCKED` ([apps/crawler/frontier.py](apps/crawler/frontier.py)); batched upserts via `unnest` arrays + `ON CONFLICT` + `xmax` insert detection, change-only history with a `LATERAL` probe ([apps/crawler/pipeline.py](apps/crawler/pipeline.py)); PL/pgSQL trigger for `tsvector` search, pg_trgm typo fallback; partial, covering (`INCLUDE`), GIN, BRIN indexes; a forward-filling materialized view refreshed `CONCURRENTLY`; DB-enforced invariants (partial unique index, CHECKs); `COPY` exports; advisory locks; PgBouncer transaction pooling. |

All numbers below were measured, not estimated - see [docs/performance.md](docs/performance.md).

| Measurement | Result |
|---|---|
| Cached anonymous API traffic through Nginx (50 concurrent clients) | **18,585 req/s**, p95 3 ms, 0 errors |
| Uncached (every request hits Django + PostgreSQL, 8 uWSGI workers) | **937 req/s**, p95 78 ms, 0 errors |
| Frontier claim (`SKIP LOCKED`) on 200k URLs | 1.7 ms |
| API list page on 50k products / 770k price points | 0.36 ms, 1 SQL query |
| "Biggest price drops" after an EXPLAIN-driven rewrite | 585 ms → 154 ms |
| Zero-downtime `touch-chain-reload` under traffic | 300 / 300 requests OK |

## Running it

**Full stack** (Docker):

```bash
docker compose up --build -d
docker compose exec web python manage.py crawl demo_shop          # Selenium Grid + signed API
docker compose exec web python manage.py crawl books_toscrape --max-pages 100
curl "http://localhost:8080/api/v1/products/?category=headphones"
```

Watch Selenium's browser live at http://localhost:7900 (noVNC, password `secret`).
Scale out with `docker compose up -d --scale web=3 --scale crawler=4`.

**Local development** (Python 3.12 + PostgreSQL 14+):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements/dev.txt
cp .env.example .env                      # point DATABASE_URL at your Postgres
python manage.py migrate
python -m demo_target.app &               # the SPA crawl target on :5050
python manage.py discover_api http://127.0.0.1:5050/ --click "#load-more" --verify-signer
python manage.py crawl demo_shop
python manage.py crawl books_toscrape --workers 4 --max-pages 80
python manage.py runserver
```

**Tests** (need PostgreSQL; the code relies on Postgres-only features on purpose):

```bash
pytest                                    # 65 tests
RUN_SELENIUM_TESTS=1 pytest -m selenium   # 3 more that drive headless Chrome
```

## The crawl targets

* **books.toscrape.com** - a public sandbox built for scraping practice. Server-rendered
  HTML, link-following crawl through the Postgres frontier with N concurrent workers;
  re-crawls send `If-None-Match`/`If-Modified-Since` (54 of 60 pages came back `304`).
* **DemoShop** ([demo_target/](demo_target/)) - a local SPA that behaves like a hostile
  production site: data only via an undocumented API, a JS challenge before a session
  cookie is issued, HMAC-signed requests with an obfuscated key, minified field names,
  opaque cursors, rate limiting and random 5xx. It lets the reverse-engineering workflow
  be shown end-to-end without hammering anyone's real site. The walkthrough is in
  [docs/reverse-engineering.md](docs/reverse-engineering.md).

## API

| Endpoint | Notes |
|---|---|
| `GET /api/v1/products/` | cursor-paginated; filters `category, source, brand, min_price, max_price, in_stock, changed_since, attr=key:value` |
| `GET /api/v1/products/{id}/` | with the last 30 price points (sliced `Prefetch`) |
| `GET /api/v1/products/{id}/history/?days=90` | step series incl. the price in effect at the window start |
| `GET /api/v1/products/search/?q=` | full-text (ranked) → trigram fallback for typos |
| `GET /api/v1/categories/` | counts + averages in one GROUP BY |
| `GET /api/v1/analytics/overview/` · `price-drops/` · `cheapest/` · `categories/{slug}/trend/` | analytical SQL, window functions, materialized view |
| `POST /api/v1/exports/` → `GET /api/v1/exports/{id}/download/` | async `COPY` export, streamed by Nginx (`X-Accel-Redirect`) |
| `GET /api/v1/crawler/sources/` · `runs/`, `POST sources/{slug}/crawl/` | crawl status + manual trigger (staff) |
| `GET /healthz`, `GET /readyz` | liveness (no I/O) / readiness (DB + cache) |

## Documentation

* [docs/architecture.md](docs/architecture.md) - components, data model, design decisions
* [docs/reverse-engineering.md](docs/reverse-engineering.md) - how the private API was taken apart
* [docs/deployment.md](docs/deployment.md) - uWSGI / Nginx / PgBouncer / Postgres configuration explained
* [docs/performance.md](docs/performance.md) - load tests, query plans, what was changed because of them
* [docs/query-plans.md](docs/query-plans.md) - raw `EXPLAIN (ANALYZE, BUFFERS)` output

## Layout

```
config/            settings (base/dev/prod/test), Celery app, WSGI entry point
apps/core/         health checks, request-id + Server-Timing middleware, cache + advisory-lock helpers
apps/crawler/      frontier, HTTP client, pipeline, Selenium + CDP discovery, sources, Celery tasks
apps/catalog/      products, price history, search, REST API, seed/EXPLAIN tooling
apps/analytics/    materialized view + analytical queries
apps/exports/      COPY-based CSV exports
demo_target/       the SPA crawl target (Flask)
deploy/            uwsgi.ini, nginx.conf, Postgres init SQL
docker/            multi-stage Dockerfile, entrypoint, uwsgi-protocol health check
loadtest/          Locust scenarios
tests/             pytest suite (runs against real PostgreSQL)
```
