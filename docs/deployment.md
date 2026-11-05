# Deployment: uWSGI, Nginx, PgBouncer, PostgreSQL

The configuration lives in [deploy/](../deploy) and [docker-compose.yml](../docker-compose.yml).
This page explains the non-obvious settings. Everything here was exercised for real
(see "How this was verified" at the end).

## Request path and timeouts

```
client ─► Nginx ──(uwsgi protocol, TCP)──► uWSGI master ─► worker (process × threads) ─► Django
                                                                           └─► PgBouncer ─► Postgres
```

Timeouts are layered so the *innermost* component gives up first and reports why:

| Layer | Setting | Value |
|---|---|---|
| Postgres | `statement_timeout` (role level) | 15 s |
| uWSGI | `harakiri` (kills the worker, logs a traceback) | 30 s |
| Nginx | `uwsgi_read_timeout` | 35 s |

If Nginx timed out first, the client would get a 504 while the stuck worker kept
running. `idle_in_transaction_session_timeout` and `lock_timeout` are also set at role
level.

## uWSGI ([uwsgi.ini](../deploy/uwsgi/uwsgi.ini))

* **`socket` (uwsgi protocol) instead of `http-socket`** - Nginx speaks it natively
  (`uwsgi_pass`), it is cheaper to parse than HTTP, and Nginx passes request metadata
  (request id, scheme, client IP) as WSGI environ variables. TCP rather than a unix
  socket so Nginx can round-robin over `--scale web=N` replicas. The uwsgi protocol is
  one request per connection, so an upstream `keepalive` pool in Nginx would do
  nothing - a common copy-paste mistake from `proxy_pass` configs.
* **`processes` × `threads`** - requests are I/O-bound (Postgres, Redis), so 2 threads
  per process raise throughput at little memory cost. 8 × 2 in compose.
* **`lazy-apps = true`** - Django is imported in each worker *after* `fork()`. That costs
  copy-on-write memory sharing, but guarantees no DB connection, psycopg pool or thread
  is ever inherited across a fork - required when `DB_POOL=true` (Django 5.1+ native
  psycopg pool, one pool per worker) and required by `touch-chain-reload`.
* **`cheaper-algo = busyness`** - starts with 2 workers and spawns more (up to
  `processes`) when workers are busy or requests wait in the listen queue; idle
  workers are reaped. Observed under load: workers 3-8 spawned within seconds
  (`[busyness] 46 requests in listen queue, spawning 2 emergency worker(s)`).
* **`thunder-lock`** - serialises `accept()` so a new connection doesn't wake every
  worker (thundering herd).
* **`listen = 1024`** - the accept backlog; bursts queue here instead of being refused.
  Must not exceed `net.core.somaxconn`, which compose raises via `sysctls`.
* **Recycling** - `max-requests`, `max-worker-lifetime`, `reload-on-rss` bound memory
  leaks; `worker-reload-mercy` bounds how long a recycle waits. With lazy-apps a
  recycle re-imports Django (~1 s), so `max-requests` is sized to traffic: at 5000 the
  load test recycled each worker every ~35 s; it is now 50 000.
* **Zero-downtime deploys** - `touch-chain-reload`: touching `/app/var/reload` restarts
  workers *one at a time*, each new worker loaded before the next old one goes.
  Verified: 300 requests sent during a chain reload, 300 × `200`.
* **Containers** - `die-on-term` + `hook-master-start = unix_signal:15 gracefully_kill_them_all`
  make SIGTERM a graceful shutdown (by default uWSGI *reloads* on SIGTERM, which makes
  `docker stop` hang until SIGKILL). Verified: exit code 0, "goodbye to uWSGI".
* **`strict = true`** - an unknown or misspelt option is a startup error, not silently ignored.
* **Logging** - Nginx writes the access log (with timings and cache status); uWSGI only
  logs 4xx/5xx, slow requests (`log-slow`) and memory (`memory-report`). Live worker
  state: `uwsgitop 127.0.0.1:9191` (stats server).

The container health check ([docker/healthcheck.py](../docker/healthcheck.py)) speaks
the uwsgi protocol directly - the web container exposes no HTTP port for curl to hit.

## Nginx ([nginx.conf](../deploy/nginx/nginx.conf), [pricelens.conf](../deploy/nginx/conf.d/pricelens.conf))

* **Micro-cache** for anonymous API GETs ([api_cache.inc](../deploy/nginx/conf.d/snippets/api_cache.inc)).
  Freshness comes from Django's `Cache-Control: public, max-age=N` (5-60 s per endpoint),
  so the application decides what is cacheable. `uwsgi_cache_lock` lets one request
  refill an expired entry while others wait; `uwsgi_cache_use_stale updating` +
  `uwsgi_cache_background_update` serve the stale copy during the refill. Requests with
  an `Authorization` header or session cookie bypass the cache. Under load: 99.8 % hits,
  18.5k req/s.
* **`uwsgi_hide_header X-Request-ID`** - Django echoes the request id; a cached response
  would otherwise replay the id of the request that filled the cache. Nginx always adds
  the current `$request_id` itself.
* **Response buffering** (`uwsgi_buffering on`) - the uWSGI worker is released as soon
  as Django finishes; Nginx feeds slow clients.
* **`X-Accel-Redirect`** - export downloads are authorised by Django and then streamed by
  Nginx from an `internal` location (`/protected-exports/`), which clients cannot request
  directly (verified: 404).
* **Static files** - `ManifestStaticFilesStorage` puts a content hash in every filename,
  so `/static/` is served with `Cache-Control: public, max-age=31536000, immutable`.
  The static files are baked into the Nginx image at build time (multi-stage
  Dockerfile), avoiding the stale-named-volume trap.
* **Rate limiting** - `limit_req` per client IP: 30 r/s (burst 60) for the API, 5 r/s for
  search; `limit_conn` 50 per IP; status 429. Accounting has 1 ms granularity, so more
  than `burst` requests in the same millisecond are rejected at any rate.
* **Retries** - `uwsgi_next_upstream error timeout http_503` (the uwsgi module has no
  `http_502` condition - that exists only for `proxy_next_upstream`; Nginx refuses to
  start with it, which the verification run caught).
* **Logs** - JSON access log including `request_time`, `upstream_response_time`,
  `upstream_cache_status` and `request_id`.

## PgBouncer + Django

The web tier connects through PgBouncer in **transaction** pooling mode: 1000 client
connections share 20 server connections. Consequences handled in
[settings](../config/settings/base.py) (`USE_PGBOUNCER=true`):

* `DISABLE_SERVER_SIDE_CURSORS` - `QuerySet.iterator()` server-side cursors live across
  transactions on one backend.
* `prepare_threshold = None` - no server-side prepared statements (psycopg 3).
* `statement_timeout` moved to the role (`ALTER ROLE … SET`) because PgBouncer rejects
  the libpq `options` startup parameter.
* Only transaction-scoped advisory locks (`pg_try_advisory_xact_lock`) and
  `set_config(…, is_local => true)`.
* Migrations run once, in a dedicated one-shot service, **directly** against Postgres
  (DDL shouldn't go through a transaction pooler, and N web replicas must not race to migrate).

Without PgBouncer, `DB_POOL=true` enables Django's psycopg pool per uWSGI worker
(`CONN_MAX_AGE = 0` is then mandatory). Connection math to keep in mind:
`replicas × processes × pool max_size ≤ max_connections - headroom`.

## PostgreSQL

Settings in compose (sized for a ~2 GB container): `shared_buffers` 25 % of RAM,
`effective_cache_size` 75 %, `random_page_cost = 1.1` (SSD), `work_mem` 16 MB,
`pg_stat_statements` preloaded, `log_min_duration_statement = 250ms`, `log_lock_waits`,
`track_io_timing`.

Operational notes learned while building this:

* After a bulk load run `VACUUM (ANALYZE)`: it refreshes statistics, merges GIN
  *pending lists* (until then the planner prices the trigram/FTS indexes as expensive -
  it chose a 205 ms seq scan over a 40 ms index scan) and sets visibility-map bits so
  index-only scans report `Heap Fetches: 0`.
* `now()` is frozen at transaction start; the frontier claim compares against
  `clock_timestamp()`. With `now()`, a worker running inside a transaction never saw
  URLs queued after the transaction began and spun forever (caught by a test).
* Redis runs with `maxmemory-policy volatile-lru`: under memory pressure only keys with
  a TTL (cache entries) are evicted, never the Celery queues sharing the instance.

## Scaling out

* **Web**: `docker compose up -d --scale web=3`; Nginx resolves `web` to all replicas.
  Sessions live in Postgres/Redis (`cached_db`), so any replica serves any user.
* **Crawlers**: `--scale crawler=4`; frontier workers coordinate only through
  `SKIP LOCKED`. Per-host politeness is per worker process, so lower
  `rate_limit_per_host` as workers are added.
* **Database**: reads already hit indexes or the materialized view; the next steps would
  be a read replica for analytics and range-partitioning `catalog_pricepoint` by month
  (the BRIN index and time-ordered inserts are already partition-friendly).

## How this was verified

Docker isn't available on the development machine, so the shipped configs were run
for real in a sudo-free Linux environment: PostgreSQL 16 and Nginx 1.24 unpacked from
Ubuntu packages, uWSGI 2.0.30, with **only paths and ports rewritten** in
`uwsgi.ini`/`nginx.conf`. Checked: `nginx -t`, uwsgi-protocol health probe, micro-cache
MISS → HIT, BYPASS when authenticated, gzip, immutable static headers, trigram search,
COPY export served via X-Accel-Redirect (and 404 on direct access), 429s from
`limit_req`, chain reload under traffic, cheaper scaling, graceful SIGTERM, and the load
tests in [performance.md](performance.md). The Docker images and the full compose stack
(PgBouncer, Celery, Selenium Grid) are exercised by the `stack` job in CI.
