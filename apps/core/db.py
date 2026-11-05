"""Small PostgreSQL helpers that the ORM doesn't expose directly."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


def advisory_key(name: str) -> int:
    """Map an arbitrary lock name to the signed 64-bit key Postgres expects."""
    digest = hashlib.blake2b(name.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def advisory_xact_lock(name: str) -> Iterator[bool]:
    """Try to take a *transaction-scoped* advisory lock; yields whether we got it.

    ``pg_try_advisory_xact_lock`` is released automatically at COMMIT/ROLLBACK,
    which makes it safe behind PgBouncer in transaction-pooling mode (a
    session-level ``pg_advisory_lock`` could be left dangling on a pooled
    backend that another client then inherits).
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [advisory_key(name)])
            acquired = cursor.fetchone()[0]
        yield acquired


def refresh_materialized_view(name: str, *, concurrently: bool = True) -> None:
    """REFRESH a materialized view.

    CONCURRENTLY keeps the view readable during the refresh (it diffs into the
    existing data and needs a unique index) but is not allowed on a view that
    has never been populated, so fall back to a blocking refresh in that case.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT ispopulated FROM pg_matviews WHERE matviewname = %s", [name])
        row = cursor.fetchone()
        if row is None:
            raise LookupError(f"materialized view {name!r} does not exist")
        mode = "CONCURRENTLY " if (concurrently and row[0]) else ""
        cursor.execute(f"REFRESH MATERIALIZED VIEW {mode}{connection.ops.quote_name(name)}")
