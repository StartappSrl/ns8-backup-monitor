"""Shared SQLite schema/connection helper for backup-monitor.

Both the poller loop and the web app run inside the same container and
share the same database file at /state/reports.db (mounted from the
module's state directory), so they use this one module to stay in sync.

It also holds the small amount of read-side logic both of them need:
scan status bookkeeping (the "meta" table), the overnight-window summary
(who reported, who is missing) and validation of the e-mail digest
settings - kept here rather than in new modules so the container image
needs no extra files.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Optional

try:  # zoneinfo needs tzdata (system package or the "tzdata" wheel)
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

DB_PATH = os.environ.get("DB_PATH", "/state/reports.db")

# Bump on every release: shown in the dashboard so "which code is the
# container actually running?" never needs a trip to the server again.
APP_VERSION = "2026.10.13-3"

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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS trusted_devices (
    token_hash   TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    last_used_at REAL
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


# -- key/value store ------------------------------------------------------

def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# -- time -------------------------------------------------------------------

def app_tz():
    """Timezone report timestamps are expressed in (they are stored as naive
    local times, exactly as printed in the e-mails)."""
    name = os.environ.get("APP_TIMEZONE", "Europe/Rome")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def local_now() -> datetime:
    """Current time as a naive datetime in the app timezone."""
    return datetime.now(app_tz()).replace(tzinfo=None)


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# -- scan status ------------------------------------------------------------

def scan_status(conn: sqlite3.Connection) -> dict:
    """Summarise the poller's last cycles for the dashboard status bar and
    for the digest's "is the data fresh?" check."""
    poll_interval = int(os.environ.get("POLL_INTERVAL", "300"))
    stale_after = max(600, 3 * poll_interval)
    now = datetime.now(timezone.utc)

    started = _parse_iso(meta_get(conn, "last_scan_started"))
    finished = _parse_iso(meta_get(conn, "last_scan_finished"))
    success = _parse_iso(meta_get(conn, "last_scan_success"))
    ok_flag = meta_get(conn, "last_scan_ok")
    error = meta_get(conn, "last_scan_error")
    warnings = [w for w in meta_get(conn, "last_scan_warnings").split("\n") if w]

    scanning = bool(started and (finished is None or started > finished))
    scan_seconds = int((now - started).total_seconds()) if scanning and started else None
    age = int((now - success).total_seconds()) if success else None

    if started is None and success is None:
        state = "waiting"
    elif scanning and scan_seconds is not None and scan_seconds < 1800:
        state = "scanning"
    elif ok_flag == "0":
        state = "error"
    elif age is None or age > stale_after:
        state = "stale"
    elif warnings:
        state = "warning"
    else:
        state = "ok"

    return {
        "state": state,
        "version": APP_VERSION,
        "last_success": success.isoformat(timespec="seconds") if success else None,
        "age_seconds": age,
        "scan_seconds": scan_seconds,
        "error": error,
        "warnings": warnings,
        "new_messages": int(meta_get(conn, "last_scan_new", "0") or 0),
        "poll_interval": poll_interval,
        "stale_after": stale_after,
    }


# -- overnight window -------------------------------------------------------
#
# A "notte" is the window overnight backup jobs run in: 13:00 the day BEFORE
# its date through 06:00 ON its date (night of 23/09 = 22/09 13:00 -> 23/09
# 06:00). Reports in between (06:00-13:00) belong to no window.

NIGHT_START = dtime(13, 0)
NIGHT_END = dtime(6, 0)
_TS_FMT = "%Y-%m-%dT%H:%M:%S"
_TS_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*"

PRIOR_WINDOWS = 14          # previous nights examined (two full weeks, so a weekend
                            # date always has 4 comparable weekend nights)
EXPECT_MIN_WINDOWS = 3      # fewer comparable nights than this = can't judge

_SEVERITY_RANK = {"INFO": 0, "OK": 1, "WARNING": 2, "CRITICAL": 3}

# A report with no user, status or backup set at all is one no parser
# understood (every field blank) - what the "non riconosciuti" badge counts.
UNRECOGNIZED_SQL = (
    "(COALESCE(user,'') = '' AND COALESCE(status,'') = '' AND COALESCE(backup_set,'') = '')"
)


def night_window(d: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d - timedelta(days=1), NIGHT_START),
        datetime.combine(d, NIGHT_END),
    )


def night_date_for(ts: datetime) -> Optional[date]:
    """The night a timestamp belongs to, or None for daytime (06:00-13:00)."""
    t = ts.time()
    if t <= NIGHT_END:
        return ts.date()
    if t >= NIGHT_START:
        return ts.date() + timedelta(days=1)
    return None


def default_night_date(now: datetime) -> date:
    """Most recent night that has finished (or the one that just ended)."""
    return now.date() if now.time() >= NIGHT_END else now.date() - timedelta(days=1)


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime((s or "")[:19], _TS_FMT)
    except ValueError:
        return None


def _day_type(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


def _worst(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def night_summary(conn: sqlite3.Connection, d: date, sender: str = "") -> dict:
    """Who reported during the night of `d`, and who is missing.

    "Clients" are distinct users counted once however many reports they
    have. Only reports that carry a backup set count: account-level notices
    (quota exceeded, settings updated) say nothing about whether a backup
    actually ran, so they must not make a client look present.

    A client is "expected" when it appeared in more than half of the
    comparable previous nights (same weekday/weekend type when there are
    enough of those, otherwise all previous nights with data); it is
    "missing" when expected but absent from this night. With fewer than
    EXPECT_MIN_WINDOWS comparable nights the history is too thin to say.
    """
    start, end = night_window(d)
    sender_sql = " AND sender = ?" if sender else ""
    base = (
        "SELECT user, backup_set, destination, status, severity, timestamp FROM reports "
        "WHERE COALESCE(user,'') != '' AND COALESCE(backup_set,'') != '' "
        f"AND timestamp GLOB '{_TS_GLOB}' AND timestamp >= ? AND timestamp <= ?"
        + sender_sql
    )

    def fetch(a: datetime, b: datetime):
        params: list = [a.strftime(_TS_FMT), b.strftime(_TS_FMT)]
        if sender:
            params.append(sender)
        return conn.execute(base, params).fetchall()

    # -- this night
    clients: dict[str, dict] = {}
    problems: list[dict] = []
    seen_problem = set()
    rows = fetch(start, end)
    for r in rows:
        key = r["user"].strip().lower()
        sev = r["severity"] or "INFO"
        c = clients.setdefault(key, {"user": r["user"].strip(), "severity": "INFO", "reports": 0})
        c["severity"] = _worst(c["severity"], sev)
        c["reports"] += 1
        if sev in ("CRITICAL", "WARNING"):
            sig = (key, r["backup_set"], r["destination"], r["status"], sev)
            if sig not in seen_problem:
                seen_problem.add(sig)
                problems.append({
                    "user": r["user"].strip(), "backup_set": r["backup_set"],
                    "destination": r["destination"] or "", "status": r["status"] or "",
                    "severity": sev, "timestamp": r["timestamp"],
                })
    by_severity = {"CRITICAL": 0, "WARNING": 0, "OK": 0, "INFO": 0}
    for c in clients.values():
        by_severity[c["severity"]] = by_severity.get(c["severity"], 0) + 1
    problems.sort(key=lambda p: (-_SEVERITY_RANK[p["severity"]], p["user"].lower()))

    # -- previous nights -> expected clients
    prior_dates = [d - timedelta(days=i) for i in range(1, PRIOR_WINDOWS + 1)]
    earliest = night_window(prior_dates[-1])[0]
    latest = night_window(prior_dates[0])[1]
    windows: dict[date, dict[str, str]] = {}
    for r in fetch(earliest, latest):
        ts = _parse_ts(r["timestamp"])
        nd = night_date_for(ts) if ts else None
        if nd in prior_dates:
            windows.setdefault(nd, {})[r["user"].strip().lower()] = r["user"].strip()

    with_data = [pd for pd in prior_dates if windows.get(pd)]
    same_type = [pd for pd in with_data if _day_type(pd) == _day_type(d)]
    if len(same_type) >= EXPECT_MIN_WINDOWS:
        considered, basis = same_type, "same_day_type"
    elif len(with_data) >= EXPECT_MIN_WINDOWS:
        considered, basis = with_data, "all"
    else:
        considered, basis = [], "none"

    missing: list[dict] = []
    if considered:
        # strictly more than half, so a client that only runs some of the
        # nights (e.g. Saturday-only) isn't "expected" on all of them
        threshold = max(2, len(considered) // 2 + 1)
        counts: dict[str, int] = {}
        for pd in considered:
            for k in windows[pd]:
                counts[k] = counts.get(k, 0) + 1
        for k, n in counts.items():
            if n >= threshold and k not in clients:
                last_night = max(pd for pd in with_data if k in windows[pd])
                name = windows[last_night][k]
                missing.append({
                    "user": name, "seen_in": n, "of": len(considered),
                    "last_night": last_night.isoformat(),
                })
        missing.sort(key=lambda m: (-m["seen_in"], m["user"].lower()))

    return {
        "date": d.isoformat(),
        "start": start.strftime(_TS_FMT),
        "end": end.strftime(_TS_FMT),
        "total_clients": len(clients),
        "total_reports": len(rows),
        "by_severity": by_severity,
        "clients": sorted(clients.values(), key=lambda c: c["user"].lower()),
        "problems": problems,
        "missing": missing,
        "insufficient_history": not considered,
        "considered_nights": len(considered),
        "comparison_basis": basis,
    }


# -- e-mail digest settings -------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DIGEST_MODES = ("starttls", "ssl")
MAX_RECIPIENTS = 10


def digest_settings(conn: sqlite3.Connection) -> dict:
    return {
        "enabled": meta_get(conn, "digest_enabled", "0") == "1",
        "recipients": meta_get(conn, "digest_recipients", ""),
        "time": meta_get(conn, "digest_time", "07:00"),
        "mode": meta_get(conn, "digest_smtp_mode", "starttls"),
        "last_sent_date": meta_get(conn, "digest_last_sent_date", ""),
        "last_sent_at": meta_get(conn, "digest_last_sent_at", ""),
        "last_error": meta_get(conn, "digest_last_error", ""),
    }


def validate_digest_settings(data: dict) -> tuple[Optional[dict], str]:
    """Returns (clean_settings, "") or (None, error message)."""
    enabled = bool(data.get("enabled"))
    raw = str(data.get("recipients") or "")
    recipients = [x for x in re.split(r"[,\s;]+", raw.strip()) if x]
    if len(recipients) > MAX_RECIPIENTS:
        return None, f"Al massimo {MAX_RECIPIENTS} destinatari."
    for r in recipients:
        if not _EMAIL_RE.match(r):
            return None, f"Indirizzo non valido: {r}"
    if enabled and not recipients:
        return None, "Indica almeno un destinatario per abilitare il riepilogo."
    t = str(data.get("time") or "07:00").strip()
    if not _TIME_RE.match(t):
        return None, "Orario non valido (usa HH:MM)."
    mode = str(data.get("mode") or "starttls")
    if mode not in DIGEST_MODES:
        return None, "Modalità SMTP non valida."
    return {"enabled": enabled, "recipients": recipients, "time": t, "mode": mode}, ""


def save_digest_settings(conn: sqlite3.Connection, clean: dict) -> None:
    meta_set(conn, "digest_enabled", "1" if clean["enabled"] else "0")
    meta_set(conn, "digest_recipients", ", ".join(clean["recipients"]))
    meta_set(conn, "digest_time", clean["time"])
    meta_set(conn, "digest_smtp_mode", clean["mode"])
