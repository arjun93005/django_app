# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#   builder  - compiles wheels (uWSGI is a C program built from source by pip)
#   runtime  - slim image with only the wheels + app; runs uWSGI / Celery
#   nginx    - Nginx with the collected static files baked in
#
#   docker build -f docker/Dockerfile --target runtime -t pricelens .
#   docker build -f docker/Dockerfile --target nginx   -t pricelens-nginx .

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpcre2-dev \
 && rm -rf /var/lib/apt/lists/*
COPY requirements/ /tmp/requirements/
RUN pip wheel --wheel-dir /wheels -r /tmp/requirements/prod.txt

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod \
    STATIC_ROOT=/app/var/static \
    EXPORTS_ROOT=/srv/exports \
    WEB_PROCESSES=8 \
    WEB_THREADS=2 \
    WEB_CHEAPER=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpcre2-8-0 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app && useradd --system --gid app --home-dir /app app

COPY --from=builder /wheels /wheels
COPY requirements/ /tmp/requirements/
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements/prod.txt \
 && rm -rf /wheels /tmp/requirements

WORKDIR /app
COPY --chown=app:app . .
# Hashed static files (ManifestStaticFilesStorage) are produced at build time,
# so the image is immutable and Nginx can cache them forever.
RUN DJANGO_SECRET_KEY=build-only-not-secret python manage.py collectstatic --noinput \
 && mkdir -p /srv/exports /app/var && touch /app/var/reload \
 && chown -R app:app /srv/exports /app/var

USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uwsgi", "--ini", "/app/deploy/uwsgi/uwsgi.ini"]

# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS nginx
RUN rm /etc/nginx/conf.d/default.conf
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/conf.d/ /etc/nginx/conf.d/
COPY --from=runtime /app/var/static /srv/static
