"""Database engine setup.

Works with any SQLAlchemy URL. Queries stick to portable SQL, so pointing
`GEO_DATABASE_URL` at Postgres (`postgresql+psycopg://...`, after installing
psycopg) needs no code change; only SQLite gets the connection tuning below.
"""

import json
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url

# How long a connection waits for another one's write lock before giving up
# with "database is locked".
SQLITE_BUSY_TIMEOUT_S = 5

# JSON columns are written strictly: NaN/Infinity would be stored as invalid
# JSON (and couldn't be returned to clients), so fail loudly instead. Unicode
# is kept as-is rather than \u-escaped.
_json_dumps = partial(json.dumps, ensure_ascii=False, allow_nan=False)


def create_db_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, json_serializer=_json_dumps, pool_pre_ping=True)

    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        json_serializer=_json_dumps,
        # Connections are used from the threadpool and the background processing
        # thread, never by two threads at once (the pool hands each one out
        # exclusively), so sqlite3's same-thread check is unnecessary.
        connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_S},
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _configure_sqlite_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        # SQLite ignores foreign keys (and so ON DELETE CASCADE) unless asked, per connection.
        cursor.execute("PRAGMA foreign_keys=ON")
        # Write-ahead logging: readers don't block the writer and vice versa, so
        # status polls keep working while a file's measurements are being stored.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Safe with WAL (no corruption); only the last commits can be lost on power failure.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_S * 1000}")
    finally:
        cursor.close()
