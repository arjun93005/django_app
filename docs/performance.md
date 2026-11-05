# Performance

Everything on this page was measured; the commands to reproduce it are included.
Absolute numbers depend on hardware - the point is the *relative* effect of each
design decision and that the system degrades gracefully.

**Environment**: a 28-core laptop, WSL2 Ubuntu 24.04, PostgreSQL 16, Nginx 1.24,
uWSGI 2.0.30, Python 3.12. Load generator (Locust, 4 processes) on the same machine,
so it competes with the server for CPU. Dataset from `seed_demo_data`: 20k products /
260k price points for load tests, 50k / 770k for query plans.

## 1. Load tests (Nginx → uWSGI → Django → PostgreSQL)

```bash
python manage.py seed_demo_data --products 20000 --days 120
locust -f loadtest/locustfile.py BrowsingUser     --host http://localhost:8080 --headless -u 50 -t 30s --processes 4
locust -f loadtest/locustfile.py CacheBustingUser --host http://localhost:8080 --headless -u 50 -t 30s --processes 4
```

Traffic mix: product list by category (6), product detail (4), search incl. a typo (3),
categories (2), price drops (1). 50 closed-loop clients = 50 requests always in flight.

| Scenario | uWSGI | Requests | req/s | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|---|
| `BrowsingUser` (Nginx micro-cache active) | 8 × 2 | 558,168 | **18,585** | 2 ms | 3 ms | 4 ms | 0 |
| `CacheBustingUser` (unique URL per request: no cache tier helps) | 8 × 2 | 28,103 | **937** | 49 ms | 78 ms | 160 ms | 0 |
| `CacheBustingUser` | 4 × 2 | 17,376 | 578 | 81 ms | 110 ms | 220 ms | 0 |

* Nginx cache status during `BrowsingUser`: 557,256 HIT, 115 MISS, 248 STALE,
  568 UPDATING - with `uwsgi_cache_lock` + `use_stale updating`, expiry of a hot key
  costs **one** upstream request, not a stampede.
* Doubling uWSGI workers took uncached throughput from 578 to 937 req/s (1.6×) and
  cut p50 from 81 to 49 ms: requests are waiting for a free worker, not for Postgres
  (each request spends ~1-4 ms in the database according to `Server-Timing`).
* The *cheaper* subsystem started 2 workers and grew to 8 under load
  (`[busyness] 46 requests in listen queue, spawning 2 emergency worker(s)`).

### Problems the load tests exposed (and fixes)

| Symptom | Cause | Fix |
|---|---|---|
| 71 % of uncached requests failed with 429 | DRF's `UserRateThrottle` also throttles *anonymous* users by IP, on top of `AnonRateThrottle`; all load came from one IP | rates are env-configurable (`THROTTLE_ANON` / `THROTTLE_USER`), raised for the test |
| A handful of 429s after raising Nginx limits | `nginx -s reload` leaves old workers serving existing keep-alive connections with the *old* limits; plus `limit_req` counts in 1 ms buckets | full restart for benchmarks; documented |
| `Respawned uWSGI worker` every ~35 s in the uWSGI log during the uncached test | `max-requests = 5000` at ~150 req/s per worker; with lazy-apps each recycle re-imports Django (~1 s of reduced capacity) | `max-requests = 50000` (lifetime and RSS limits still bound leaks) |
| Duplicate `X-Request-ID`; cached responses carried a *stale* id | both Django and Nginx set the header, and the cached copy kept Django's | `uwsgi_hide_header X-Request-ID` |
| Two `Cache-Control` headers on static files | `expires 1y` plus `add_header Cache-Control` | single `Cache-Control: public, max-age=31536000, immutable` |

## 2. Query plans

```bash
python manage.py seed_demo_data --products 50000 --days 180
python manage.py explain_queries --out docs/query-plans.md
```

Full output: [query-plans.md](query-plans.md). 50,675 products, 770,452 price points,
200k frontier URLs.

| Query | Plan highlights | Time |
|---|---|---|
| Frontier claim (`FOR UPDATE SKIP LOCKED`, 10 rows) | Index Scan on partial index `frontier_claim_idx` → LockRows | 1.7 ms |
| Latest price of a product | Index Only Scan backward on `(product_id, observed_at DESC) INCLUDE (price)` | 0.3 ms |
| Change-detection insert, 200 items | 200 × LATERAL Index Only Scan, Heap Fetches: 0 | 1.5 ms |
| API list page (1 query: joins + previous-price subquery) | Index Scan Backward on PK, 51 × Index Only Scan | 0.36 ms |
| Full-text search | Bitmap Index Scan on GIN `product_search_gin` | 3.1 ms |
| Typo search (pg_trgm, `%>`) | Bitmap Index Scan on GIN `product_title_trgm` | 41 ms |
| Biggest price drops, 7 days | BRIN range + LATERAL predecessor probe | 154 ms |
| 7-day aggregate over price history | Bitmap Index Scan on BRIN (a few kB) | 6.4 ms |

### Two optimisations driven by EXPLAIN

**Price drops: 585 ms → 154 ms.** The first version computed
`lag(price) OVER (PARTITION BY product_id ORDER BY observed_at)` over every
recently-changed product's full history - a sequential scan of all 770k rows and a
sort. Only the rows *inside* the window need a predecessor, so the rewrite takes recent
rows from the BRIN index and fetches each predecessor with one index-only probe:

```sql
FROM catalog_pricepoint cur                         -- observed_at >= since (BRIN)
CROSS JOIN LATERAL (
    SELECT pp.price FROM catalog_pricepoint pp
    WHERE pp.product_id = cur.product_id AND pp.observed_at < cur.observed_at
    ORDER BY pp.observed_at DESC LIMIT 1           -- index-only scan
) AS prev
```

The window-function form is still the right tool in the materialized view, which must
read all history anyway.

**Trigram search: 205 ms → 41 ms.** Right after the bulk load the planner chose a
sequential scan. Freshly inserted GIN entries sit in the index's *pending list*
(`fastupdate`) until VACUUM merges them, and the planner costs the index accordingly.
`seed_demo_data` now finishes with `VACUUM (ANALYZE)`, which also sets visibility-map
bits so index-only scans stop visiting the heap.

## 3. Crawler throughput

| Crawl | Result |
|---|---|
| books.toscrape.com, 4 workers, 80 pages, polite 4 req/s per host | 22.6 s, 73 products, 0 errors |
| Re-crawl with stored ETags (60 pages) | 54 × `304 Not Modified`, 6 × `200` |
| DemoShop, 600 products (Chrome bootstrap ~3 s + 10 signed API pages) | 4.5 s total |
| DemoShop re-crawl | 600 upserts, 122 price points written (only the changed prices) |
| `seed_demo_data` (set-based SQL) | 50k products + 770k price points in ~40 s |

## 4. Guarding against regressions

* `tests/test_api.py::test_product_list_uses_a_constant_number_of_queries` - the list
  endpoint must use exactly 1 SQL query for 1 or 41 products (fails on any N+1).
* `test_product_detail_prefetches_recent_prices_in_two_queries` - 2 queries.
* Every response carries `Server-Timing: app;dur=…, db;dur=…;desc="N queries"`, and
  requests slower than `SLOW_REQUEST_MS` are logged with their query count.
* Postgres logs statements slower than 250 ms; `pg_stat_statements` is preloaded.
