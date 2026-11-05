# PriceLens - Project Summary

> A product & price-intelligence platform built with **Django, PostgreSQL, Nginx + uWSGI,
> Celery, and Selenium/HTTP crawlers**. It collects products and prices from e-commerce
> sites, including a JavaScript single-page app whose private API had to be reverse
> engineered. It stores the full price history efficiently in PostgreSQL and serves it
> through a REST API built to scale horizontally.

---

## 1. What the project does

E-commerce prices change constantly. PriceLens answers questions like:

* *What does product X cost right now, and what did it cost over the last 90 days?*
* *Which products dropped in price by more than 10 % this week?*
* *What are the three cheapest in-stock items per category?*
* *How has the average price of a category moved day by day?*

To answer them it must (1) **collect** data from sites that don't want to be scraped
easily, (2) **store** a growing price history without the database exploding, and
(3) **serve** read-heavy API traffic quickly and reliably.

| Stage | How |
|---|---|
| Collect | Concurrent crawlers: link-following HTML crawl of books.toscrape.com and a reverse-engineered signed API of a JavaScript SPA (DemoShop) |
| Store | PostgreSQL: current catalogue + change-only price history, full-text search, a materialized view for daily analytics |
| Serve | Django REST Framework behind Nginx and uWSGI, two-tier caching, CSV exports streamed by Nginx |
| Operate | Celery beat/workers, PgBouncer, Redis, Selenium Grid, Docker Compose, CI that boots the whole stack |

---

## 2. Tech stack

| Layer | Technology | Why it's here |
|---|---|---|
| Web framework | Django 5.2 LTS, Django REST Framework, django-filter | ORM, admin, migrations, mature API tooling |
| App server | **uWSGI 2.0** | process/thread management, adaptive scaling, zero-downtime reloads |
| Reverse proxy | **Nginx** | buffering, caching, rate limiting, static & protected file serving |
| Database | **PostgreSQL 16** (+ pg_trgm, pg_stat_statements) | relational integrity plus advanced features: SKIP LOCKED, GIN/BRIN, materialized views, window functions |
| Pooling | PgBouncer (transaction mode) / psycopg 3 pool | thousands of client connections on a few backends |
| Cache / broker | Redis | response cache, DRF throttling, Celery broker |
| Background jobs | Celery (beat + workers, chords) | scheduled and fan-out crawls, exports, MV refreshes |
| Crawling | httpx, parsel, pydantic | fast pooled HTTP, CSS/XPath parsing, validation of scraped data |
| Browser automation | **Selenium 4** (local Chrome / Selenium Grid), Chrome DevTools Protocol | JS challenge solving, network capture for API discovery, DOM fallback |
| Packaging | Docker multi-stage builds, Docker Compose | reproducible 11-service stack |
| Quality | pytest + pytest-django (real PostgreSQL), Ruff, GitHub Actions, Locust | 68 tests, lint, full-stack smoke test, load tests |

---

## 3. Architecture at a glance

```
           ┌─────────────── request path ───────────────┐
client ──► Nginx ──uwsgi protocol──► uWSGI workers ──► Django ──► PgBouncer ──► PostgreSQL
           │ micro-cache, rate limits, X-Accel             │                      ▲
           │ immutable static files                        ▼                      │
           │                                             Redis ◄── Celery ────────┤
           └───────────────────────────────────────────────────────│              │
                                              crawl path:          ├─ httpx ──► websites
                                                                   └─ Selenium Grid (Chrome)
```

* **Read path**: Nginx answers most anonymous requests from its micro-cache. Misses go
  to uWSGI → Django, which answers from Redis or runs one optimized SQL query.
* **Write path**: crawlers claim URLs from a work queue *inside PostgreSQL*, fetch and
  parse pages, and write in batches with set-based SQL.
* **Scheduling**: Celery beat dispatches due crawls; frontier crawls fan out to N
  workers with a Celery chord, then a finalizer invalidates caches and refreshes analytics.

---

## 4. Highlights by skill area

### 4.1 Building and optimizing scalable Django apps with Nginx + uWSGI

**uWSGI** ([deploy/uwsgi/uwsgi.ini](deploy/uwsgi/uwsgi.ini))
* Binary **uwsgi protocol** between Nginx and uWSGI (cheaper than HTTP proxying); TCP so
  Nginx can load-balance across `--scale web=N` replicas.
* **processes × threads** model (8 × 2) for I/O-bound requests; `enable-threads`,
  `thunder-lock` against thundering-herd wakeups, `listen = 1024` backlog matched to `somaxconn`.
* **`lazy-apps`**: Django loads *after* fork in each worker, so no DB connection or
  connection pool is ever shared across processes. This is required for the per-worker
  psycopg pool and for chain reloads.
* **Adaptive scaling** with the `cheaper` busyness algorithm: starts with 2 workers and
  grew to 8 under load in testing.
* **Reliability**: `harakiri` (30 s) kills stuck requests with a traceback, plus worker
  recycling by request count, lifetime and memory.
* **Zero-downtime deploys** with `touch-chain-reload`: 300/300 requests succeeded during a reload.
* **Graceful SIGTERM** for containers; `strict` mode rejects misspelt options.
* A container **health check that speaks the uwsgi binary protocol** directly
  ([docker/healthcheck.py](docker/healthcheck.py)).

**Nginx** ([deploy/nginx/](deploy/nginx))
* **Micro-caching** of anonymous API GETs, driven by the `Cache-Control` header Django
  sends. Includes **stampede protection** (`uwsgi_cache_lock`) and
  **stale-while-revalidate** (`use_stale updating` + background update). Authenticated
  requests bypass the cache.
* **Per-IP rate limiting** (`limit_req` / `limit_conn`, separate budget for search).
* **X-Accel-Redirect**: Django authorizes a CSV download and Nginx streams the file from
  an `internal` location, so the uWSGI worker is freed immediately.
* **Immutable static files**: hashed names via `ManifestStaticFilesStorage`, a 1-year
  cache policy, baked into the Nginx image at build time.
* **Layered timeouts**: Postgres `statement_timeout` 15 s < uWSGI `harakiri` 30 s < Nginx
  read timeout 35 s, so the innermost layer fails first and logs the cause.
* JSON access logs with upstream timing, cache status and a request id that is shared
  with Django's logs.

**Django-level optimization**
* **Constant query counts**: the product list is **exactly 1 SQL query** for any page
  size (`select_related`, `.only()`, a correlated subquery for the previous price).
  The product detail is 2 queries (sliced `Prefetch`). Tests fail on any N+1 regression.
* **Keyset (cursor) pagination**: no `COUNT(*)` and no `OFFSET` scans, and stable
  results while crawls insert data.
* **Two-tier caching** ([apps/core/cache.py](apps/core/cache.py)): Redis payload cache
  keyed by a *namespace version*. Invalidation after a crawl is a single atomic `INCR`
  instead of deleting keys by pattern.
* **Observability**: a `Server-Timing` header on every response (app time, DB time,
  query count, visible in browser devtools), request-id propagation and slow-request logging.
* **Connection management**: persistent connections with health checks, an opt-in
  psycopg 3 pool, and a PgBouncer-compatible mode (no server-side cursors or prepared
  statements; transaction-scoped locks only).

### 4.2 Reverse engineering web APIs and building robust crawlers

**Reverse engineering a private API** ([docs/reverse-engineering.md](docs/reverse-engineering.md))
1. **Discover**: `manage.py discover_api` drives Chrome, records **Chrome DevTools
   Protocol** network events, fetches response bodies, and summarizes each endpoint
   (query parameters, custom headers, JSON shape). Cookies are redacted.
2. **Probe**: replay requests to learn the contract (session cookie, timestamp window,
   which parts the signature covers, rate limits, opaque base64 keyset cursors).
3. **Read the minified bundle**: identify HMAC-SHA256 from the SHA-256 constants and
   the ipad/opad XOR, and de-obfuscate the XOR-encoded key.
4. **Port and prove**: a Python signer that reproduces the browser's signatures
   **byte-for-byte** (`--verify-signer`: 3/3 matched), also checked in tests.

**Hybrid crawling strategy** ([apps/crawler/sources/demo_shop.py](apps/crawler/sources/demo_shop.py))
* **Selenium only where JavaScript is unavoidable**: headless Chrome solves the site's
  per-request JS challenge once and hands over the **HttpOnly** session cookie
  (readable through WebDriver, not through `document.cookie`).
* **Plain HTTP for bulk data**: pages are signed in Python and fetched with pooled
  httpx connections. 600 products take ~1.5 s after the 3 s browser bootstrap.
* **Self-healing**: a new session on 401, clock re-sync from the server's `Date` header
  on clock-skew errors, backoff on 429/5xx.
* **Graceful degradation**: if signatures start being rejected (the site shipped a new
  bundle), the crawler falls back to scraping the rendered DOM with Selenium. It clicks
  "Load more" and extracts all cards in one `execute_script` per page.
* **Checkpoint/resume**: the cursor is saved after each page; `crawl --resume` continues
  from it.

**Robust crawling infrastructure**
* **Postgres-backed URL frontier** using `FOR UPDATE SKIP LOCKED`: any number of threads
  or Celery workers consume it without double-processing (tested with 4 concurrent
  connections). Includes priorities, retry backoff, and a visibility timeout for crashed workers.
* **HTTP client** ([apps/crawler/http.py](apps/crawler/http.py)):
  * per-host token bucket with **AIMD** (halve the rate on 429, recover slowly)
  * full-jitter exponential backoff that honors `Retry-After`
  * **robots.txt per RFC 9309**, including `Crawl-delay`
  * **conditional GETs** (ETag / Last-Modified): 54 of 60 re-crawled pages returned `304`
* **Validation**: every scraped record passes a pydantic model; bad pages are counted
  and skipped instead of crashing the run.
* **Ethics**: an honest User-Agent, robots.txt compliance, and politeness limits. The
  reverse-engineering target is a local mock site built for the purpose, and the public
  target is a scraping sandbox.

### 4.3 PostgreSQL / SQL

| Technique | Where / why |
|---|---|
| `FOR UPDATE SKIP LOCKED` work queue | crawl frontier: concurrent consumers without a separate queue system |
| `INSERT … SELECT FROM unnest(arrays) ON CONFLICT DO UPDATE` | batch upserts in one statement; `CASE` in `DO UPDATE` moves `price_changed_at` only on a real change |
| `RETURNING (xmax = 0)` | distinguishes inserted from updated rows for accurate run statistics |
| `LEFT JOIN LATERAL` latest-row probe | writes a price point **only when price or stock changed** |
| Covering index `INCLUDE (price, in_stock)` | index-only "latest price" lookups (`Heap Fetches: 0`) |
| BRIN index on `observed_at` | time-range scans over append-only history with a tiny index |
| GIN: `tsvector`, `gin_trgm_ops`, `jsonb_path_ops` | full-text search, typo-tolerant search, JSONB containment filters |
| Partial indexes | pending frontier rows only; in-stock products by category |
| PL/pgSQL triggers | weighted search vector kept correct under bulk upserts; recomputed only when text changes |
| Materialized view + `REFRESH … CONCURRENTLY` | daily per-category stats with `lag`/`lead` windows, `LATERAL generate_series` forward-fill and `count(*) FILTER` |
| Window functions in the ORM | top-N per category with `ROW_NUMBER()` and filtering on the window (Django's QUALIFY emulation) |
| Partial unique index | the database guarantees "one running crawl per source" |
| Advisory locks (`pg_try_advisory_xact_lock`) | single scheduler per tick; safe behind PgBouncer |
| `COPY … TO STDOUT` | CSV exports streamed straight from Postgres via psycopg 3 |
| `fillfactor = 85` | room for HOT updates when every crawl touches `last_seen_at` |
| `set_config(…, is_local => true)` | per-transaction trigram threshold, PgBouncer-safe |
| Server-side data generation | `generate_series` seeds 50k products + 770k price points in ~40 s |

---

## 5. Measured results

All measured on a 28-core laptop (WSL2, PostgreSQL 16, Nginx 1.24, uWSGI 2.0.30), with
the load generator on the same machine. Methodology is in [docs/performance.md](docs/performance.md).

| Metric | Result |
|---|---|
| Cached API traffic through Nginx (50 concurrent clients) | **18,585 req/s**, p95 3 ms, 0 errors, 99.8 % cache hits |
| Uncached traffic (every request reaches Django + PostgreSQL) | **937 req/s**, p95 78 ms, 0 errors |
| Uncached throughput, 4 → 8 uWSGI workers | 578 → 937 req/s (1.6×) |
| API list page on 50k products / 770k price points | 0.36 ms, 1 query |
| Frontier claim on 200k URLs | 1.7 ms |
| Change detection for 200 scraped items | 1.5 ms |
| "Biggest price drops" query after an EXPLAIN-driven rewrite | **585 ms → 154 ms** |
| Typo search after fixing GIN pending lists with VACUUM | **205 ms → 41 ms** |
| Price-history rows written on a re-crawl of 600 products | 122 (only the changes) |

---

## 6. Engineering challenges solved

Real problems hit during development; most of them are visible as `fix:` / `perf:`
commits in the history.

| Problem | Root cause | Fix |
|---|---|---|
| Crawl workers spun forever in tests | Postgres `now()` is frozen at transaction start, so new queue rows looked "in the future" | compare with `clock_timestamp()` |
| Nginx refused to start | `http_502` is valid for `proxy_next_upstream` but not for the `uwsgi_*` directives | use the uwsgi module's valid conditions |
| Cached responses carried another request's id | Django's `X-Request-ID` was stored in the Nginx cache | `uwsgi_hide_header X-Request-ID` |
| Price-drop query took 585 ms | `lag()` computed over every product's full history (seq scan of 770k rows) | BRIN range + `LATERAL` index-only predecessor probe |
| Planner chose a 205 ms seq scan over a 40 ms index | GIN "pending list" after a bulk load | `VACUUM (ANALYZE)` after seeding |
| Typo "keybaord" found nothing | pg_trgm default threshold 0.6; the word similarity was 0.44 | per-transaction threshold 0.4 via `set_config` |
| Selenium fallback hung on the last page | CSS `display: block` overrode the `hidden` attribute | fixed CSS; treat "no new cards" as end of list |
| 71 % of load-test requests got 429 | DRF's `UserRateThrottle` also throttles *anonymous* users by IP | env-configurable throttle rates |
| Workers restarted every ~35 s under load | `max-requests = 5000` + lazy-apps re-import | `max-requests = 50000` |
| Every app container failed to start in CI | repo created on Windows, so `entrypoint.sh` was committed without the exec bit (100644) | `git update-index --chmod=+x` + `chmod` in the Dockerfile |
| Celery workers crashed in the production image only | kombu imports `packaging` but declares it only in its `redis` extra; tests passed because pytest installs it | depend on `celery[redis]`; caught by the CI full-stack job |
| Crawler ~6× slower on Windows | `localhost` resolved to IPv6 first and fell back per connection | use `127.0.0.1` for the local target |

---

## 7. Testing and quality

* **68 tests** with pytest-django against a **real PostgreSQL**, because the code relies
  on Postgres-only features on purpose. Highlights:
  * 4 threads with separate connections claim from the frontier and never get the same URL
  * query-count tests guard against N+1 queries
  * the reverse-engineered client runs against the target server's own verification
    code in-process (`httpx.WSGITransport`)
  * 3 opt-in tests drive real headless Chrome: challenge solving, CDP capture plus
    signature verification, and DOM fallback
* **GitHub Actions** runs three jobs:
  * Ruff lint and format checks
  * the full test suite, including the Chrome tests
  * a **full-stack job** that builds the Docker images, runs `nginx -t` on the shipped
    config, boots Nginx → uWSGI → PgBouncer → PostgreSQL with Redis, Celery and Selenium
    Grid, and crawls through both the CLI and the Celery queue
* The deployment configs were verified for real: `nginx.conf` and `uwsgi.ini` were run
  with only paths and ports changed, plus Locust load tests.

---

## 8. Design decisions and trade-offs

* **PostgreSQL as the crawl queue instead of a message broker.** Per-URL state
  (dedup, priority, retries, ETags) is relational and `SKIP LOCKED` gives safe
  concurrency. Celery is used for orchestration only, which avoids dual-write
  consistency problems.
* **Raw SQL on the hot path, the ORM elsewhere.** Ingestion needs `xmax`, conditional
  upserts and `LATERAL`, which the ORM can't express. The API stays on the ORM with
  enforced query budgets.
* **Change-only price history.** Storage grows with the number of changes, not crawls.
  The cost is that readers must forward-fill, which the history endpoint and the
  materialized view do.
* **`lazy-apps` in uWSGI.** It costs copy-on-write memory sharing but buys fork safety
  and zero-downtime chain reloads.
* **A browser only where needed.** Chrome is ~100× more expensive per page than an HTTP
  request, so it bootstraps sessions and acts as a fallback, not as the main fetcher.

---

## 9. What I would do next

* Range-partition `catalog_pricepoint` by month (the BRIN index and time-ordered inserts
  already suit this) and move analytics to a read replica.
* Prometheus exporters for uWSGI stats, Nginx `stub_status`, PgBouncer and
  `pg_stat_statements`, plus Grafana dashboards.
* Distributed per-host rate limiting in Redis, so politeness holds across many crawler
  processes.
* Proxy-pool and fingerprint-rotation support, only for targets that permit it.

---

## 10. Resume-ready bullet points

* Built a Django/DRF price-intelligence platform served by **Nginx + uWSGI** (uwsgi
  protocol, lazy-apps, adaptive *cheaper* workers, zero-downtime chain reloads). It
  sustained **~18.6k req/s at p95 3 ms** with Nginx micro-caching and **~940 req/s**
  uncached against PostgreSQL.
* **Reverse engineered a JavaScript SPA's private API** using Chrome DevTools Protocol
  capture and bundle analysis. Ported its obfuscated HMAC-SHA256 request signing to
  Python (verified byte-for-byte), using **Selenium** only to solve the JS session
  challenge and as a DOM-scraping fallback.
* Designed a **PostgreSQL-backed crawl frontier** (`FOR UPDATE SKIP LOCKED`) consumed by
  concurrent Celery workers, with retry backoff, adaptive per-host rate limiting,
  robots.txt compliance, conditional requests and checkpoint/resume.
* Modeled a change-only price history with covering, BRIN, GIN and partial indexes,
  trigger-maintained full-text search and a concurrently-refreshed materialized view.
  EXPLAIN-driven query rewrites cut the slowest analytics query **3.8×**.
* Shipped the system as an 11-service Docker Compose stack (PgBouncer, Redis, Celery,
  Selenium Grid) with CI that runs 68 tests and smoke-tests the full stack.

---

## 11. Where to look in the code

| Topic | File |
|---|---|
| uWSGI tuning | [deploy/uwsgi/uwsgi.ini](deploy/uwsgi/uwsgi.ini) |
| Nginx caching / rate limits / X-Accel | [deploy/nginx/conf.d/pricelens.conf](deploy/nginx/conf.d/pricelens.conf), [api_cache.inc](deploy/nginx/conf.d/snippets/api_cache.inc) |
| Query-optimized API | [apps/catalog/api/views.py](apps/catalog/api/views.py) |
| Two-tier cache | [apps/core/cache.py](apps/core/cache.py) |
| SKIP LOCKED frontier | [apps/crawler/frontier.py](apps/crawler/frontier.py) |
| Set-based ingestion SQL | [apps/crawler/pipeline.py](apps/crawler/pipeline.py) |
| Resilient HTTP client | [apps/crawler/http.py](apps/crawler/http.py) |
| CDP API discovery | [apps/crawler/discovery.py](apps/crawler/discovery.py) |
| Reverse-engineered client + Selenium | [apps/crawler/sources/demo_shop.py](apps/crawler/sources/demo_shop.py) |
| Materialized view | [apps/analytics/migrations/0001_daily_category_stats.py](apps/analytics/migrations/0001_daily_category_stats.py) |
| Analytical SQL | [apps/analytics/services.py](apps/analytics/services.py) |
| COPY exports | [apps/exports/services.py](apps/exports/services.py) |
| Compose stack | [docker-compose.yml](docker-compose.yml) |
| Query plans / load tests | [docs/query-plans.md](docs/query-plans.md), [docs/performance.md](docs/performance.md) |
