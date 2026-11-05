# Architecture

## Components

| Component | Role |
|---|---|
| **Nginx** | Edge: client keep-alive, gzip, response buffering, static files, protected downloads, micro-cache, per-IP rate limits. Talks to uWSGI over the *uwsgi* binary protocol. |
| **uWSGI** | Process manager for the Django WSGI app: master + adaptive pool of workers (processes × threads), worker recycling, harakiri, zero-downtime chain reloads. |
| **Django + DRF** | Catalog / analytics / export / crawler-control APIs, admin. |
| **PgBouncer** | Transaction-pooling proxy: many client connections (uWSGI threads × replicas, Celery) share a few Postgres backends. |
| **PostgreSQL** | System of record *and* the crawl work queue (frontier), full-text search, analytics. |
| **Redis** | Django cache (two-tier response cache, DRF throttles) and Celery broker/results. |
| **Celery** | `beat` dispatches due crawls; `crawler` workers run crawl tasks (queue `crawl`); `worker` runs exports / MV refreshes (queue `default`). |
| **Selenium Grid** | Headless Chrome for the few steps that genuinely need a JavaScript engine. |
| **demo-target** | The SPA crawl target used to demonstrate API reverse engineering. |

## Data model

```
crawler_source 1──* crawler_crawlrun           (one *running* run per source: partial unique index)
       │ 1
       ├──* crawler_frontierurl                (work queue; unique (source, url_hash))
       │ 1
       └──* catalog_product *──1 catalog_category
                  │ 1
                  └──* catalog_pricepoint      (append-only, written on change only)

analytics_daily_category_stats                 (materialized view over pricepoint + product)
exports_export                                 (async CSV export jobs)
```

* `catalog_product` - current state of a product; `search_vector` is maintained by a
  PL/pgSQL trigger; `fillfactor = 85` leaves room for HOT updates (every crawl rewrites
  `last_seen_at`, an unindexed column, so updates stay heap-only).
* `catalog_pricepoint` - the price *history* as change events. A crawl of 600 products
  where 122 changed writes 122 rows, not 600. Readers treat it as a step function.
  Indexes: `(product_id, observed_at DESC) INCLUDE (price, in_stock)` for "latest price"
  lookups (index-only) and BRIN on `observed_at` for time ranges.

## Crawl pipeline

```
           seeds ─► frontier (pending) ◄──────────────── new links
                        │ claim N rows: UPDATE … FROM (SELECT … FOR UPDATE SKIP LOCKED)
     ┌──────────────────┼──────────────────┐
  worker 1          worker 2   …        worker N           (threads or Celery tasks)
     │ HttpClient: robots.txt, token bucket per host, retries/backoff, conditional GET
     │ Source.process(): parse → ProductItem (pydantic validation)
     ▼
  IngestionPipeline (buffer ~200 items)
     1. categories     INSERT … ON CONFLICT DO NOTHING
     2. products       INSERT … SELECT FROM unnest(arrays) ON CONFLICT DO UPDATE … RETURNING (xmax = 0)
     3. price changes  INSERT … SELECT … LEFT JOIN LATERAL (latest point) WHERE IS DISTINCT FROM
  CrawlRun counters    UPDATE … SET pages = pages + n (F expressions, no lost updates)
```

Two source strategies:

* **FrontierSource** (books.toscrape.com) - link-following crawl. Any number of workers
  share the frontier; `SKIP LOCKED` hands each a disjoint batch (tested with 4 threads
  and 4 connections). A worker that dies leaves rows `in_progress`; `housekeeping`
  returns them to `pending` after a visibility timeout, like SQS.
* **StreamSource** (DemoShop) - an API paged by cursor, inherently sequential. After each
  page the pipeline is flushed and the cursor stored on the run, so `crawl --resume`
  continues where a failed run stopped (at-least-once; upserts are idempotent).

## Key design decisions

**Postgres as the crawl queue.** The frontier needs deduplication (unique index),
priorities, retries with backoff, per-URL state (ETag, last status) and visibility of
progress - all relational. `SKIP LOCKED` provides the concurrency primitive, so no
second queueing system (and no dual-write consistency problem) is needed. Celery is
used for *orchestration* (fan-out, chaining, scheduling), not for per-URL messages.

**Invariants in the database.** "One running crawl per source" is a partial unique
index, not a `SELECT … then INSERT` race in Python. Price/rating sanity are CHECK
constraints. The scheduler and the MV refresh are guarded by transaction-scoped
advisory locks (`pg_try_advisory_xact_lock`) - safe behind PgBouncer, unlike
session-level locks.

**Raw SQL where it pays, the ORM everywhere else.** The ingestion hot path needs a
conditional `DO UPDATE`, `xmax` and `LATERAL`, which the ORM cannot express; the API
uses the ORM (`select_related`, `only`, `Subquery`, sliced `Prefetch`, `Window` +
filtering on window expressions, conditional aggregation) and every list endpoint is
covered by a query-count test.

**Browser only where needed.** A real Chrome is ~100x more expensive per page than an
HTTP request. Selenium bootstraps the session (it must execute the site's challenge
code) and is kept as a degraded fallback; the catalogue itself is fetched with httpx
using a Python port of the request signer.

**Caching that is cheap to invalidate.** Responses are cached in Redis under
`api:<namespace>:v<version>:<hash>`; finishing a crawl increments the namespace
version (one `INCR`), orphaning all old keys, which then expire. Nginx adds a few
seconds of micro-cache on top, governed by the `Cache-Control` header Django emits.

**Observability built in.** Nginx generates `$request_id` and forwards it; Django logs
it and returns it, so one id joins the access log and application logs. Every
response carries `Server-Timing` (app time, DB time, query count); uWSGI exposes a JSON
stats socket; Postgres runs `pg_stat_statements` and logs statements over 250 ms.

## Failure handling summary

| Failure | Handling |
|---|---|
| Transient network error / 5xx / 429 | retried with full-jitter exponential backoff; `Retry-After` honoured; 429 halves the host's rate (AIMD) |
| robots.txt unreachable (5xx) | treated as "disallow all" (RFC 9309) |
| Page parse error | counted as invalid item / non-retryable URL failure; the run continues |
| Worker process killed | frontier visibility timeout + Celery `acks_late` redelivery; stuck runs failed after `MAX_RUN_DURATION` |
| Crash mid stream-crawl | resume from the checkpointed cursor |
| Session expired (401) | new session bootstrapped in Chrome, request retried |
| Clock skew (403 `E_CLOCK`) | offset learned from the server's `Date` header |
| Signature rejected (403 `E_SIG`) | logged as a contract change; crawl degrades to DOM scraping with Selenium |
| Slow request | Postgres `statement_timeout` (15 s) < uWSGI `harakiri` (30 s) < Nginx `uwsgi_read_timeout` (35 s) |
