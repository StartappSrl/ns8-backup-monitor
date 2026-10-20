#!/usr/bin/env python3
"""
backup-monitor poller.

Runs forever inside the module's container. Every POLL_INTERVAL seconds it:
  1. connects to the configured IMAP mailbox,
  2. for every configured folder, looks for messages from any of the
     configured sender addresses, not seen before,
  3. parses each one into one or more backup-report records
     (see parsing.py),
  4. stores new records in a local SQLite database at /state/reports.db,
     which the "list-reports" NS8 action later reads to serve the UI.

Configuration comes entirely from environment variables (written by the
configure-module action into the systemd unit's env-file):

  IMAP_HOST             required
  IMAP_PORT             default: 993
  IMAP_SSL              "true"/"false", default: true
  IMAP_USERNAME         required
  IMAP_PASSWORD         required
  IMAP_FOLDERS          comma-separated, default: INBOX
  IMAP_SENDER_FILTERS   comma-separated, required - only mail from these
                        addresses is analyzed
  POLL_INTERVAL         seconds, default: 300
"""

from __future__ import annotations

import email
import html as html_lib
import imaplib
import logging
import os
import re
import signal
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

import parsing
from db import (
    APP_VERSION, DB_PATH, UNRECOGNIZED_SQL, default_night_date, digest_settings, get_db,
    local_now, meta_get, meta_set, night_summary, scan_status, utc_iso_now,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backup-monitor")

_running = True

# Set by trigger_scan_now() (called from the web app's "Scansiona ora"
# button) to cut the sleep between cycles short instead of waiting out
# the rest of POLL_INTERVAL - checked once per second in main()'s sleep
# loop below.
_scan_now = threading.Event()


def trigger_scan_now() -> None:
    """Ask the poller loop to start its next cycle immediately."""
    _scan_now.set()


def _handle_signal(signum, frame):
    global _running
    log.info("Received signal %s, shutting down after this cycle.", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


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
    # Without an explicit timeout, imaplib's underlying socket blocks
    # forever on a stalled connection or a server that stops responding
    # mid-command - the poller has been observed staying "active (running)"
    # for hours with no new log output at all after such a hang, since
    # nothing ever raises to be caught by run_cycle's own error handling
    # or the outer loop. This applies to every subsequent read/write on
    # the connection, not just the initial connect.
    timeout = int(os.environ.get("IMAP_TIMEOUT", "60"))

    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    else:
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        try:
            conn.starttls()
        except imaplib.IMAP4.error:
            log.warning("STARTTLS not available/needed on %s:%s", host, port)

    conn.login(username, password)
    return conn


def parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


def build_sender_search(sender_filters: list[str]) -> str:
    """Build an IMAP SEARCH criteria string matching any of the given
    From addresses. IMAP's OR is binary and left-nested for 3+ terms:
    OR FROM a OR FROM b FROM c matches a OR b OR c."""
    terms = [f'FROM "{s}"' for s in sender_filters]
    while len(terms) > 1:
        a = terms.pop(0)
        b = terms.pop(0)
        terms.insert(0, f"OR {a} {b}")
    return f"({terms[0]})"


def build_since_clause(retention_days: int) -> str:
    """Build an IMAP SEARCH "SINCE <date>" criterion so the server itself
    filters out anything older than the retention window, instead of the
    poller fetching and parsing the ENTIRE mailbox history every cycle
    (which, with a large/long-lived mailbox, is slow enough to make the
    poller effectively unable to keep up - by the time one full pass
    finishes, it's already due to start the next). Messages older than
    retention are purged from the reports table right after anyway, so
    there is no reason to ever fetch them.

    A day of slack beyond the exact retention window guards against
    timezone/off-by-one edges around SINCE's date-only (no time-of-day)
    granularity.
    """
    since_date = datetime.now(timezone.utc) - timedelta(days=retention_days + 1)
    # IMAP SINCE date format: DD-Mon-YYYY (RFC 3501), e.g. "04-Sep-2026".
    return f'SINCE {since_date.strftime("%d-%b-%Y")}'


def run_cycle(conn_db: sqlite3.Connection) -> dict:
    """One full pass over every configured folder.

    Returns {"processed": <new messages handled>, "folder_errors": [...]} so
    main() can record it for the dashboard's status bar: a folder that
    can't be opened (e.g. a mistyped path) doesn't abort the cycle, but it
    must not pass silently as a fully healthy scan either.
    """
    folders = parse_csv_env("IMAP_FOLDERS", "INBOX")
    sender_filters = parse_csv_env("IMAP_SENDER_FILTERS", "")
    processed = 0
    folder_errors: list[str] = []
    if not sender_filters:
        log.warning("No sender filter configured, skipping cycle")
        return {"processed": 0, "folder_errors": ["Nessun filtro mittente configurato"]}
    retention_days = int(os.environ.get("RETENTION_DAYS", "15"))
    search_criteria = f"{build_since_clause(retention_days)} {build_sender_search(sender_filters)}"

    imap = connect_imap()
    try:
        cur = conn_db.cursor()
        for folder in folders:
            status, data = imap.select(folder, readonly=True)
            if status != "OK":
                log.error("Cannot select folder %s: %s", folder, data)
                folder_errors.append(f"Cartella non selezionabile: {folder}")
                continue

            uidvalidity_resp = imap.response("UIDVALIDITY")
            uidvalidity = uidvalidity_resp[1][0].decode() if uidvalidity_resp and uidvalidity_resp[1][0] else "0"

            status, data = imap.uid("search", None, search_criteria)
            if status != "OK":
                log.error("IMAP search failed in folder %s: %s", folder, data)
                folder_errors.append(f"Ricerca fallita nella cartella: {folder}")
                continue

            uids = data[0].split() if data and data[0] else []
            log.info("Folder %s: %d message(s) matching %s", folder, len(uids), sender_filters)

            for uid in uids:
                uid_str = uid.decode()
                msg_key = f"{folder}::{uidvalidity}::{uid_str}"
                cur.execute("SELECT 1 FROM processed_messages WHERE msg_key = ?", (msg_key,))
                if cur.fetchone():
                    continue  # already processed

                status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    log.warning("Could not fetch message uid=%s in folder %s", uid_str, folder)
                    continue

                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                subject = decode_mime_words(msg.get("Subject", ""))
                source_label = subject or f"{folder}-uid-{uid_str}"
                sender_email = parseaddr(msg.get("From", ""))[1].lower()

                try:
                    records = parsing.parse_email_message(raw_bytes, source_label)
                except Exception:
                    log.exception("Failed to parse message uid=%s subject=%r", uid_str, subject)
                    records = []

                for rec in records:
                    cur.execute(
                        """
                        INSERT INTO reports (
                            natural_key, source, sender, timestamp, user, backup_set,
                            destination, status, data_size, ip_address,
                            start_end, job_id, severity, log_excerpt, log_summary
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(natural_key) DO UPDATE SET
                            source=excluded.source,
                            sender=excluded.sender,
                            timestamp=excluded.timestamp,
                            user=excluded.user,
                            backup_set=excluded.backup_set,
                            destination=excluded.destination,
                            status=excluded.status,
                            data_size=excluded.data_size,
                            ip_address=excluded.ip_address,
                            start_end=excluded.start_end,
                            job_id=excluded.job_id,
                            severity=excluded.severity,
                            log_excerpt=excluded.log_excerpt,
                            log_summary=excluded.log_summary
                        """,
                        (
                            rec["natural_key"], rec["source"], sender_email, rec["timestamp"],
                            rec["user"], rec["backup_set"], rec["destination"],
                            rec["status"], rec["data_size"], rec["ip_address"],
                            rec["start_end"], rec["job_id"], rec["severity"],
                            rec.get("log_excerpt", ""),
                            rec.get("log_summary", ""),
                        ),
                    )

                cur.execute(
                    "INSERT OR IGNORE INTO processed_messages (msg_key) VALUES (?)",
                    (msg_key,),
                )
                conn_db.commit()
                processed += 1
                log.info(
                    "Processed message uid=%s folder=%s subject=%r -> %d record(s)",
                    uid_str, folder, subject, len(records),
                )
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return {"processed": processed, "folder_errors": folder_errors}


def purge_old_reports(conn_db: sqlite3.Connection) -> None:
    """Delete report rows older than RETENTION_DAYS, keeping a rolling
    history window. processed_messages is left untouched (it's just IMAP
    UID bookkeeping, not user-facing data, and keeping it prevents
    re-parsing old messages that are still sitting in the mailbox).

    Age is measured from the backup's own date (the normalized "timestamp"
    column), falling back to when the report was first read only for rows
    with no usable timestamp. Using received_at alone meant that after a
    rescan every historical report looked brand new and lingered another
    full retention period."""
    retention_days = int(os.environ.get("RETENTION_DAYS", "15"))
    cur = conn_db.execute(
        "DELETE FROM reports WHERE "
        "COALESCE(datetime(NULLIF(timestamp, '')), received_at) < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    conn_db.commit()
    if cur.rowcount:
        log.info("Retention cleanup: removed %d report(s) older than %d day(s)", cur.rowcount, retention_days)


# -- morning e-mail digest ------------------------------------------------
#
# Sent once a day at the configured time (settings live in the "meta" table,
# edited by admins from the dashboard). It reuses the IMAP mailbox's own
# credentials against the SAME host: the password is never sent anywhere an
# admin can redirect it, so there is deliberately no SMTP host setting in the
# UI (an SMTP_HOST / SMTP_FROM environment variable can override if ever
# needed).

DIGEST_WINDOW_HOURS = 6          # past the configured time, give up for the day
DIGEST_RETRY_MINUTES = 5
DIGEST_WAIT_FOR_FRESH_MINUTES = 30


def _fmt_ts(s: str) -> str:
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").strftime("%d/%m %H:%M")
    except ValueError:
        return s


def _fmt_date(s: str) -> str:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        return s


def build_digest(conn_db: sqlite3.Connection, d, summary: dict | None = None,
                 pdf_status: str | None = None) -> tuple[str, str, str]:
    """(subject, plain_text, html) for the night of date `d`.

    `summary` lets the caller reuse the night_summary() it already computed
    for the PDF, so the e-mail body and its attachment can never disagree.
    `pdf_status` is "attached", "failed" or None (not requested)."""
    s = summary if summary is not None else night_summary(conn_db, d)
    bs = s["by_severity"]
    crit = [p for p in s["problems"] if p["severity"] == "CRITICAL"]
    warn = [p for p in s["problems"] if p["severity"] == "WARNING"]
    missing = s["missing"]
    unrecognized = conn_db.execute(
        f"SELECT COUNT(*) FROM reports WHERE {UNRECOGNIZED_SQL}"
    ).fetchone()[0]
    st = scan_status(conn_db)
    fqdn = os.environ.get("FQDN", "").strip()
    link = f"https://{fqdn}/" if fqdn else ""

    def plural(n: int, one: str, many: str) -> str:
        return f"{n} {one if n == 1 else many}"

    parts = [plural(s["total_clients"], "cliente", "clienti")]
    if bs["CRITICAL"]:
        parts.append(plural(bs["CRITICAL"], "critico", "critici"))
    if bs["WARNING"]:
        parts.append(f"{bs['WARNING']} attenzione")
    if missing:
        parts.append(plural(len(missing), "mancante", "mancanti"))
    if not (bs["CRITICAL"] or bs["WARNING"] or missing):
        parts.append("tutto OK")
    subject = f"[Backup] Notte del {d.strftime('%d/%m')}: " + " · ".join(parts)

    window = f"{_fmt_ts(s['start'])} → {_fmt_ts(s['end'])}"
    stale_note = ""
    if st["state"] in ("stale", "error", "waiting"):
        when = (
            f"ultima scansione riuscita {int(st['age_seconds'] // 60)} minuti fa"
            if st["age_seconds"] is not None else "nessuna scansione riuscita registrata"
        )
        stale_note = f"ATTENZIONE: i dati potrebbero non essere aggiornati ({when})."

    def line_problem(p):
        bits = [p["user"], p["backup_set"]] + ([p["destination"]] if p["destination"] else [])
        return f"{' · '.join(bits)} — {p['status'] or 'senza dettaglio'}"

    def line_missing(m):
        return (f"{m['user']} — presente in {m['seen_in']} delle {m['of']} notti "
                f"confrontate, ultima volta la notte del {_fmt_date(m['last_night'])}")

    cap = 40

    def section(title, items, fmt):
        if not items:
            return []
        out = ["", f"{title} ({len(items)})"] + [f"- {fmt(i)}" for i in items[:cap]]
        if len(items) > cap:
            out.append(f"- …e altri {len(items) - cap}")
        return out

    text = [
        f"Notte del {d.strftime('%d/%m/%Y')} ({window})",
        f"Clienti con report: {s['total_clients']} "
        f"(OK {bs['OK'] + bs['INFO']} · Attenzione {bs['WARNING']} · Critici {bs['CRITICAL']})",
    ]
    if stale_note:
        text += ["", stale_note]
    text += section("CRITICI", crit, line_problem)
    text += section("ATTENZIONE", warn, line_problem)
    if s["insufficient_history"]:
        text += ["", "Clienti mancanti: storico insufficiente per rilevarli "
                     "(servono almeno 3 notti confrontabili)."]
    else:
        text += section("MANCANTI", missing, line_missing)
        if not missing:
            text += ["", "Nessun cliente mancante."]
    if unrecognized:
        text += ["", f"Report non riconosciuti nel database: {unrecognized}"]
    pdf_line = {
        "attached": "Il report PDF completo è in allegato.",
        "failed": "Il report PDF non è stato allegato a causa di un errore di generazione.",
    }.get(pdf_status or "")
    if pdf_line:
        text += ["", pdf_line]
    if link:
        text += ["", f"Dashboard: {link}"]

    esc = html_lib.escape

    def html_section(title, color, items, fmt):
        if not items:
            return ""
        lis = "".join(f"<li>{esc(fmt(i))}</li>" for i in items[:cap])
        if len(items) > cap:
            lis += f"<li>…e altri {len(items) - cap}</li>"
        return (f'<h3 style="margin:18px 0 6px;color:{color};font-size:14px">'
                f"{esc(title)} ({len(items)})</h3><ul style='margin:0;padding-left:20px'>{lis}</ul>")

    body = [
        f'<p style="margin:0 0 4px;font-size:15px"><b>Notte del {esc(d.strftime("%d/%m/%Y"))}</b> '
        f'<span style="color:#6b7280">({esc(window)})</span></p>',
        f'<p style="margin:0 0 4px">Clienti con report: <b>{s["total_clients"]}</b> '
        f'(OK {bs["OK"] + bs["INFO"]} · Attenzione {bs["WARNING"]} · Critici {bs["CRITICAL"]})</p>',
    ]
    if stale_note:
        body.append(f'<p style="color:#b45309"><b>{esc(stale_note)}</b></p>')
    body.append(html_section("CRITICI", "#c43d3d", crit, line_problem))
    body.append(html_section("ATTENZIONE", "#a9700f", warn, line_problem))
    if s["insufficient_history"]:
        body.append("<p>Clienti mancanti: storico insufficiente per rilevarli "
                    "(servono almeno 3 notti confrontabili).</p>")
    elif missing:
        body.append(html_section("MANCANTI", "#c43d3d", missing, line_missing))
    else:
        body.append('<p style="color:#237a50">Nessun cliente mancante.</p>')
    if unrecognized:
        body.append(f"<p>Report non riconosciuti nel database: <b>{unrecognized}</b></p>")
    if pdf_line:
        color = "#b45309" if pdf_status == "failed" else "#5b6472"
        body.append(f'<p style="color:{color}">{esc(pdf_line)}</p>')
    if link:
        body.append(f'<p><a href="{esc(link)}">Apri la dashboard</a></p>')
    html = ('<div style="font-family:Arial,sans-serif;font-size:13px;color:#1b222c">'
            + "".join(body) + "</div>")
    return subject, "\n".join(text), html


def send_email(subject: str, text_body: str, html_body: str, recipients: list[str],
               mode: str = "starttls", context=None, host: str | None = None,
               port: int | None = None,
               attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    """Send through the mailbox's own SMTP service. Raises on any failure.

    Every socket operation is bounded by SMTP_TIMEOUT: an unbounded wait is
    exactly how the IMAP poller once hung silently for hours.
    """
    host = host or os.environ.get("SMTP_HOST") or os.environ["IMAP_HOST"]
    user = os.environ["IMAP_USERNAME"]
    password = os.environ["IMAP_PASSWORD"]
    timeout = int(os.environ.get("SMTP_TIMEOUT", "30"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM") or user
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for filename, data, mimetype in attachments or []:
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    ctx = context or ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port or 465, timeout=timeout, context=ctx) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port or 587, timeout=timeout) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(user, password)
            s.send_message(msg)


def send_digest_now(conn_db: sqlite3.Connection, test: bool = False, **smtp_kwargs) -> str:
    """Build and send the digest for the latest finished night. Returns the
    subject sent. Raises on failure (callers record or report the error)."""
    settings = digest_settings(conn_db)
    recipients = [r for r in re.split(r"[,\s;]+", settings["recipients"]) if r]
    if not recipients:
        raise RuntimeError("Nessun destinatario configurato.")
    d = default_night_date(local_now())
    summary = night_summary(conn_db, d)
    attachments: list[tuple[str, bytes, str]] = []
    pdf_status = None
    if settings["attach_pdf"]:
        # A PDF that can't be built must never stop the digest itself: send
        # the mail anyway and say so in its body.
        try:
            from nightreport import build_night_pdf
            pdf = build_night_pdf(summary, "", local_now(), APP_VERSION)
            attachments.append((f"backup-notte-{d.isoformat()}.pdf", pdf, "application/pdf"))
            pdf_status = "attached"
        except Exception as e:  # noqa: BLE001 - includes ImportError (module/library missing)
            log.warning("Digest PDF not attached: %s", e)
            pdf_status = "failed"
    subject, text, html = build_digest(conn_db, d, summary, pdf_status)
    if test:
        subject = "[PROVA] " + subject
    send_email(subject, text, html, recipients, mode=settings["mode"],
               attachments=attachments, **smtp_kwargs)
    return subject


def digest_tick(conn_db: sqlite3.Connection, now: datetime | None = None) -> None:
    """Called periodically: sends today's digest once, when it is due."""
    s = digest_settings(conn_db)
    if not s["enabled"] or not s["recipients"]:
        return
    now = now or local_now()
    m = re.match(r"^(\d\d):(\d\d)$", s["time"])
    if not m:
        return
    due = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=int(m.group(1)), minute=int(m.group(2)))
    if now < due or now > due + timedelta(hours=DIGEST_WINDOW_HOURS):
        return
    today = now.date().isoformat()
    if s["last_sent_date"] == today:
        return
    last_attempt = meta_get(conn_db, "digest_last_attempt")
    if last_attempt:
        try:
            if now - datetime.fromisoformat(last_attempt) < timedelta(minutes=DIGEST_RETRY_MINUTES):
                return
        except ValueError:
            pass
    # Prefer to wait (up to 30 min) for a fresh scan so the digest reflects
    # the very latest mail; past that, send anyway - the body flags stale data.
    st = scan_status(conn_db)
    fresh = st["age_seconds"] is not None and st["age_seconds"] <= st["stale_after"]
    if not fresh and now < due + timedelta(minutes=DIGEST_WAIT_FOR_FRESH_MINUTES):
        return
    meta_set(conn_db, "digest_last_attempt", now.isoformat(timespec="seconds"))
    try:
        subject = send_digest_now(conn_db)
    except Exception as e:  # noqa: BLE001 - recorded and surfaced in the UI
        meta_set(conn_db, "digest_last_error", f"{now:%d/%m %H:%M} {str(e)[:300]}")
        log.error("Digest send failed: %s", e)
        return
    meta_set(conn_db, "digest_last_sent_date", today)
    meta_set(conn_db, "digest_last_sent_at", now.isoformat(timespec="seconds"))
    meta_set(conn_db, "digest_last_error", "")
    log.info("Digest sent: %s", subject)


def _digest_loop() -> None:
    conn = get_db()  # own connection: sqlite connections are per-thread
    while _running:
        try:
            digest_tick(conn)
        except Exception:
            log.exception("Digest scheduler error")
        for _ in range(30):
            if not _running:
                break
            time.sleep(1)
    conn.close()


def main() -> None:
    required = ["IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_SENDER_FILTERS"]
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
    threading.Thread(target=_digest_loop, name="digest", daemon=True).start()

    log.info("backup-monitor poller started. Poll interval: %ss", poll_interval)
    while _running:
        cycle_start = time.time()
        meta_set(conn_db, "last_scan_started", utc_iso_now())
        try:
            result = run_cycle(conn_db)
            purge_old_reports(conn_db)
            now_iso = utc_iso_now()
            meta_set(conn_db, "last_scan_finished", now_iso)
            meta_set(conn_db, "last_scan_success", now_iso)
            meta_set(conn_db, "last_scan_ok", "1")
            meta_set(conn_db, "last_scan_error", "")
            meta_set(conn_db, "last_scan_new", result["processed"])
            meta_set(conn_db, "last_scan_warnings", "\n".join(result["folder_errors"]))
        except Exception as e:  # noqa: BLE001 - recorded for the status bar
            log.exception("Unexpected error during poll cycle")
            meta_set(conn_db, "last_scan_finished", utc_iso_now())
            meta_set(conn_db, "last_scan_ok", "0")
            meta_set(conn_db, "last_scan_error", f"{type(e).__name__}: {str(e)[:250]}")
        elapsed = time.time() - cycle_start
        sleep_for = max(5, poll_interval - elapsed)
        for _ in range(int(sleep_for)):
            if not _running:
                break
            if _scan_now.is_set():
                _scan_now.clear()
                log.info("Manual scan requested - starting next cycle now.")
                break
            time.sleep(1)

    conn_db.close()
    log.info("backup-monitor poller stopped.")


if __name__ == "__main__":
    sys.exit(main())
  
