#!/usr/bin/env python3
"""
backup-monitor poller.

Runs forever inside the module's container. Every POLL_INTERVAL seconds it:
  1. connects to the configured IMAP mailbox,
  2. looks for messages from IMAP_SENDER_FILTER not seen before,
  3. parses each one into one or more backup-report records
     (see parsing.py),
  4. stores new records in a local SQLite database at /state/reports.db,
     which the "list-reports" NS8 action later reads to serve the UI.

Configuration comes entirely from environment variables (written by the
configure-module action into the systemd unit's env-file):

  IMAP_HOST            required
  IMAP_PORT            default: 993
  IMAP_SSL             "true"/"false", default: true
  IMAP_USERNAME        required
  IMAP_PASSWORD        required
  IMAP_FOLDER          default: INBOX
  IMAP_SENDER_FILTER   required - only mail From this address is analyzed
  POLL_INTERVAL        seconds, default: 300
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import signal
import sqlite3
import sys
import time
from email.header import decode_header

import parsing

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backup-monitor")

DB_PATH = os.environ.get("DB_PATH", "/state/reports.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    natural_key TEXT PRIMARY KEY,
    source      TEXT,
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
    received_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS processed_messages (
    uidvalidity TEXT,
    uid         TEXT,
    PRIMARY KEY (uidvalidity, uid)
);
"""

_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("Received signal %s, shutting down after this cycle.", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    decoded = []
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def connect_imap() -> imaplib.IMAP4:
    host = os.environ["IMAP_HOST"]
    port = int(os.environ.get("IMAP_PORT", "993"))
    use_ssl = env_bool("IMAP_SSL", True)
    username = os.environ["IMAP_USERNAME"]
    password = os.environ["IMAP_PASSWORD"]

    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)
        try:
            conn.starttls()
        except imaplib.IMAP4.error:
            log.warning("STARTTLS not available/needed on %s:%s", host, port)

    conn.login(username, password)
    return conn


def run_cycle(conn_db: sqlite3.Connection) -> None:
    folder = os.environ.get("IMAP_FOLDER", "INBOX")
    sender_filter = os.environ["IMAP_SENDER_FILTER"]

    imap = connect_imap()
    try:
        status, data = imap.select(folder, readonly=True)
        if status != "OK":
            log.error("Cannot select folder %s: %s", folder, data)
            return

        uidvalidity_resp = imap.response("UIDVALIDITY")
        uidvalidity = uidvalidity_resp[1][0].decode() if uidvalidity_resp and uidvalidity_resp[1][0] else "0"

        status, data = imap.uid("search", None, f'(FROM "{sender_filter}")')
        if status != "OK":
            log.error("IMAP search failed: %s", data)
            return

        uids = data[0].split() if data and data[0] else []
        log.info("Folder %s: %d message(s) from %s", folder, len(uids), sender_filter)

        cur = conn_db.cursor()
        for uid in uids:
            uid_str = uid.decode()
            cur.execute(
                "SELECT 1 FROM processed_messages WHERE uidvalidity=? AND uid=?",
                (uidvalidity, uid_str),
            )
            if cur.fetchone():
                continue  # already processed

            status, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                log.warning("Could not fetch message uid=%s", uid_str)
                continue

            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)
            subject = decode_mime_words(msg.get("Subject", ""))
            source_label = subject or f"uid-{uid_str}"

            try:
                records = parsing.parse_email_message(raw_bytes, source_label)
            except Exception:
                log.exception("Failed to parse message uid=%s subject=%r", uid_str, subject)
                records = []

            for rec in records:
                cur.execute(
                    """
                    INSERT INTO reports (
                        natural_key, source, timestamp, user, backup_set,
                        destination, status, data_size, ip_address,
                        start_end, job_id, severity
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(natural_key) DO UPDATE SET
                        status=excluded.status,
                        severity=excluded.severity
                    """,
                    (
                        rec["natural_key"], rec["source"], rec["timestamp"],
                        rec["user"], rec["backup_set"], rec["destination"],
                        rec["status"], rec["data_size"], rec["ip_address"],
                        rec["start_end"], rec["job_id"], rec["severity"],
                    ),
                )

            cur.execute(
                "INSERT OR IGNORE INTO processed_messages (uidvalidity, uid) VALUES (?,?)",
                (uidvalidity, uid_str),
            )
            conn_db.commit()
            log.info(
                "Processed message uid=%s subject=%r -> %d record(s)",
                uid_str, subject, len(records),
            )
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def main() -> None:
    required = ["IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_SENDER_FILTER"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error("Missing required configuration: %s. Waiting for configuration...", ", ".join(missing))
        # Idle rather than crash-looping: configure-module will restart us
        # once the module is actually configured.
        while missing:
            time.sleep(30)
            missing = [k for k in required if not os.environ.get(k)]

    poll_interval = int(os.environ.get("POLL_INTERVAL", "300"))
    conn_db = get_db()

    log.info("backup-monitor poller started. Poll interval: %ss", poll_interval)
    while _running:
        cycle_start = time.time()
        try:
            run_cycle(conn_db)
        except Exception:
            log.exception("Unexpected error during poll cycle")
        elapsed = time.time() - cycle_start
        sleep_for = max(5, poll_interval - elapsed)
        for _ in range(int(sleep_for)):
            if not _running:
                break
            time.sleep(1)

    conn_db.close()
    log.info("backup-monitor poller stopped.")


if __name__ == "__main__":
    sys.exit(main())
