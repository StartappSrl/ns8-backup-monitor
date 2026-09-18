"""Shared SQLite schema/connection helper for backup-monitor.

Both the poller loop and the web app run inside the same container and
share the same database file at /state/reports.db (mounted from the
module's state directory), so they use this one module to stay in sync.
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/state/reports.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    natural_key TEXT PRIMARY KEY,
    source      TEXT,
    sender      TEXT,
    timestamp   TEXT,
    user        TEXT,
    backup_set  TEXT,
    destination TEXT,
    status      TEXT,
    data_size   TEXT,
    ip_address  TEXT,
    start_end   TEXT,
    job_id      TEXT,
    severity    TEXT,
    log_excerpt TEXT,
    received_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS processed_messages (
    msg_key TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    totp_secret     TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    disabled        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS login_attempts (
    username        TEXT PRIMARY KEY,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    locked_until    REAL NOT NULL DEFAULT 0
);
"""

# Columns added after the table's original creation, for databases created
# before they existed - CREATE TABLE IF NOT EXISTS doesn't alter an
# already-existing table, so each is added here via ALTER TABLE instead.
_REPORTS_MIGRATIONS = [
    "ALTER TABLE reports ADD COLUMN sender TEXT",
    "ALTER TABLE reports ADD COLUMN log_excerpt TEXT",
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for stmt in _REPORTS_MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn
