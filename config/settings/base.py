"""Settings shared by every environment.

Everything that differs between environments is read from the process
environment (12-factor), so one Docker image can run as the uWSGI web tier,
a Celery worker or Celery beat just by changing env vars / the command.
"""

from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env()
if (BASE_DIR / ".env").exists():
    environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-do-not-use-in-production")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "apps.core",
    "apps.crawler",
    "apps.catalog",
    "apps.analytics",
    "apps.exports",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestIDMiddleware",
    "apps.core.middleware.ServerTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL", default="postgres://pricelens:pricelens@localhost:5432/pricelens")}
_db = DATABASES["default"]
_db_options = _db.setdefault("OPTIONS", {})
_db_options["connect_timeout"] = 5
# Persistent connections: avoid a TCP + auth handshake per request.
_db["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
# ...but verify a reused connection is alive before handing it to a request.
_db["CONN_HEALTH_CHECKS"] = True

USE_PGBOUNCER = env.bool("USE_PGBOUNCER", default=False)
DB_POOL = env.bool("DB_POOL", default=False)

if USE_PGBOUNCER:
    # PgBouncer in *transaction* pooling mode may hand every transaction a
    # different backend. Anything bound to a backend session breaks:
    #   * server-side cursors used by QuerySet.iterator()  -> disable them
    #   * server-side prepared statements                 -> disable them
    #     (Django uses client-side binding by default, this is belt and braces)
    #   * session-level advisory locks -> we only use pg_*_xact_lock variants
    # The statement_timeout is set at role level (deploy/postgres/initdb) since
    # PgBouncer rejects the libpq "options" startup parameter.
    _db["DISABLE_SERVER_SIDE_CURSORS"] = True
    _db_options["prepare_threshold"] = None
else:
    # Guard rail: a runaway query is cancelled by Postgres before uWSGI's
    # harakiri has to kill the whole worker.
    _db_options["options"] = f"-c statement_timeout={env.int('DB_STATEMENT_TIMEOUT_MS', default=15000)}"
    if DB_POOL:
        # psycopg3 connection pool, one per uWSGI worker process. Requires
        # uWSGI `lazy-apps` so the pool (and its threads) is created after fork.
        _db["CONN_MAX_AGE"] = 0
        _db_options["pool"] = {
            "min_size": env.int("DB_POOL_MIN_SIZE", default=2),
            "max_size": env.int("DB_POOL_MAX_SIZE", default=8),
            "timeout": 10,
        }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Cache (Redis) - also used for DRF throttling and versioned API caching
# --------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "pl",
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# --------------------------------------------------------------------------
# Auth / i18n / static
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = env.path("STATIC_ROOT", default=BASE_DIR / "var" / "static")

# --------------------------------------------------------------------------
# Django REST framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticatedOrReadOnly"],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", default="300/min"),
        "user": env("THROTTLE_USER", default="1200/min"),
        "exports": env("THROTTLE_EXPORTS", default="20/hour"),
    },
    # Default for small admin-ish lists; the product list uses keyset
    # (cursor) pagination - see apps/catalog/api/views.py.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
}

# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL or "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
# Crawl tasks are long and I/O bound: acknowledge only after completion so a
# killed worker's task is redelivered, and don't let one worker hoard tasks.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 50
CELERY_TASK_TIME_LIMIT = 60 * 55
# With acks_late the Redis visibility timeout must exceed the longest task,
# otherwise the broker redelivers a still-running task to another worker.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 60 * 60 * 2}
CELERY_TASK_ROUTES = {
    "apps.crawler.tasks.*": {"queue": "crawl"},
    "apps.exports.tasks.*": {"queue": "default"},
    "apps.analytics.tasks.*": {"queue": "default"},
}
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_BEAT_SCHEDULE = {
    "dispatch-due-crawls": {
        "task": "apps.crawler.tasks.dispatch_due_crawls",
        "schedule": timedelta(minutes=1),
    },
    "crawler-housekeeping": {
        "task": "apps.crawler.tasks.housekeeping",
        "schedule": timedelta(minutes=5),
    },
    "refresh-daily-category-stats": {
        "task": "apps.analytics.tasks.refresh_daily_category_stats",
        "schedule": crontab(minute=15),
    },
}

# --------------------------------------------------------------------------
# Crawler
# --------------------------------------------------------------------------
CRAWLER = {
    # Identify honestly; site owners can contact / block us.
    "USER_AGENT": env(
        "CRAWLER_USER_AGENT",
        default="PriceLensBot/1.0 (+https://github.com/arjun93005/django_app)",
    ),
    "RESPECT_ROBOTS_TXT": env.bool("CRAWLER_RESPECT_ROBOTS", default=True),
    "HTTP_TIMEOUT": env.float("CRAWLER_HTTP_TIMEOUT", default=20.0),
    "MAX_RETRIES": env.int("CRAWLER_MAX_RETRIES", default=4),
    "FLUSH_EVERY": env.int("CRAWLER_FLUSH_EVERY", default=200),
    "CLAIM_BATCH": env.int("CRAWLER_CLAIM_BATCH", default=10),
    # A claimed frontier URL whose worker died becomes visible again after this.
    "VISIBILITY_TIMEOUT": timedelta(seconds=env.int("CRAWLER_VISIBILITY_TIMEOUT", default=300)),
    "MAX_RUN_DURATION": timedelta(hours=env.int("CRAWLER_MAX_RUN_HOURS", default=3)),
    "SELENIUM_REMOTE_URL": env("SELENIUM_REMOTE_URL", default=""),
    "SELENIUM_HEADLESS": env.bool("SELENIUM_HEADLESS", default=True),
    "DEMO_SHOP_URL": env("DEMO_SHOP_URL", default="http://127.0.0.1:5050"),
}

# --------------------------------------------------------------------------
# CSV exports (generated with COPY, served by Nginx via X-Accel-Redirect)
# --------------------------------------------------------------------------
EXPORTS_ROOT = env.path("EXPORTS_ROOT", default=BASE_DIR / "var" / "exports")
EXPORTS_USE_X_ACCEL = env.bool("EXPORTS_USE_X_ACCEL", default=False)
EXPORTS_X_ACCEL_PREFIX = "/protected-exports/"

# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------
SLOW_REQUEST_MS = env.int("SLOW_REQUEST_MS", default=500)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "apps.core.middleware.RequestIDLogFilter"}},
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s [%(name)s] [rid=%(request_id)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "selenium": {"level": "WARNING"},
        "urllib3": {"level": "WARNING"},
    },
}
