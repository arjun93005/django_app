#!/bin/sh
# Container entrypoint shared by web (uWSGI), worker/beat (Celery) and one-off
# management commands. Migrations are NOT run here: with several web replicas
# they would race; the compose file runs them once in a dedicated service.
set -eu

if [ "${WAIT_FOR_DB:-1}" = "1" ]; then
    python manage.py wait_for_db --timeout "${WAIT_FOR_DB_TIMEOUT:-60}"
fi

exec "$@"
