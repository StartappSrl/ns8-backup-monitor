"""
Parsing logic for 1Backup-style backup report emails.

This mirrors the client-side logic originally prototyped as a standalone
HTML panel: it understands two layouts of the same data:

  - "line-pair" layout: the field label is alone on one line, its value is
    on the next line (typical of the report's HTML email body once tags
    are stripped, since each table cell becomes its own line).

  - "same-line" layout: label and value appear on the same line
    ("Destination 1Backup (1Backup)"), typical of text extracted from the
    PDF attachment, where a table row's cells sit close together.

A single email can describe MORE THAN ONE destination attempt for the
same backup job (e.g. a primary and a fallback destination), so parsing
returns a list of records, not a single one.
"""

from __future__ import annotations

import base64
import email
import re
import unicodedata
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Optional

# Canonical field name each label maps to. "Backup Time" (used in the HTML
# body) and "Start - End" (used in the PDF) are the same underlying field.
LABEL_MAP = {
    "User": "User",
    "Backup Set": "Backup Set",
    "Destination": "Destination",
    "Data Size": "Data Size",
    "Backup Quota": "Backup Quota",
    "Remaining Quota": "Remaining Quota",
    "Backup Job": "Backup Job",
    "Job Status": "Job Status",
    "Backup Time": "Start - End",
    "Start - End": "Start - End",
    "IP Address": "IP Address",
}
# Sorted longest-first so a more specific label is tried before a shorter
# one that could be a prefix of it.
LABELS_SORTED = sorted(LABEL_MAP.keys(), key=len, reverse=True)

# Heading lines that are a false-positive prefix match for a real label
# (e.g. "Backup Job Summary" starts with "Backup Job") and must be skipped.
IGNORE_LINES = {
    "backup job summary",
    "backup set settings",
    "backup logs",
    "backup files",
    "user interrupted",
}

CRITICAL_KEYS = [
    # English
    "failed", "error", "could not", "cannot", "unable", "corrupt",
    "denied", "timeout", "timed out", "disk full", "not enough space",
    "authentication", "missed",
    # Italiano
    "fallito", "fallita", "errore", "impossibile", "corrotto", "negato",
    "scaduto", "spazio esaurito", "autenticazione", "non è stato effettuato",
]
WARNING_KEYS = [
    # English
    "skipped", "still running", "warning", "quota", "partial", "retry",
    "delayed", "exceeded", "interrupted",
    # Italiano
    "saltato", "saltata", "ancora in corso", "attenzione", "parziale",
    "ritardo", "superata", "superato", "interrotto", "interrotta",
]
OK_KEYS = [
    # English
    "success", "completed successfully", "no errors",
    # Italiano
    "riuscito", "riuscita", "completato con successo", "nessun errore",
]


def classify_severity(status_text: str) -> str:
    s = (status_text or "").lower().strip()
    # Some senders (e.g. the "SQL Master Backup" / "SQL Server Management
    # Tool" notifications) use a status emoji instead of/alongside English
    # or Italian keywords - checked alongside the keyword lists, not
    # instead of them, since a status can carry both.
    if any(k in s for k in CRITICAL_KEYS) or "❌" in s or "🔴" in s:
        return "CRITICAL"
    if any(k in s for k in WARNING_KEYS) or "⚠" in s:
        return "WARNING"
    # "OK" is often followed by parenthetical detail, e.g.
    # "OK (no files backed up)" / "OK (nessun file sottoposto a backup)" -
    # match it as a prefix rather than requiring an exact "ok" status.
    if s.startswith("ok"):
        return "OK"
    if any(k in s for k in OK_KEYS) or "✅" in s:
        return "OK"
    return "INFO"


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"</(td|tr|p|div|h[1-6])>", re.IGNORECASE)


def strip_tags(html: str) -> str:
    text = _STYLE_RE.sub("", html)
    text = _SCRIPT_RE.sub("", text)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">"))
    text = re.sub(r"[ \t]+", " ", text)
    return text


def extract_records_from_text(raw_text: str) -> list[dict[str, str]]:
    """Extract one or more field-dicts ("blocks") from plain text, handling
    both the line-pair and same-line layouts described above."""
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        lower = line.lower()
        if lower in IGNORE_LINES:
            i += 1
            continue
        matched = False
        for label in LABELS_SORTED:
            canon = LABEL_MAP[label]
            label_lower = label.lower()
            if lower == label_lower:
                val = lines[i + 1].strip() if i + 1 < n else ""
                if canon == "User" and current.get("User"):
                    blocks.append(current)
                    current = {}
                current.setdefault(canon, val)
                matched = True
                break
            elif lower.startswith(label_lower) and len(line) > len(label):
                rest = line[len(label):].strip()
                if rest:
                    if canon == "User" and current.get("User"):
                        blocks.append(current)
                        current = {}
                    current.setdefault(canon, rest)
                    matched = True
                    break
        i += 1

    if current:
        blocks.append(current)
    return blocks


_SUBJECT_RE = re.compile(
    r"(?:Backup Report|Report di backup)\s*\[(.*?)\]\s*>\s*(.*?)\s*>\s*(.*?)\s*>\s*\S+\s*(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)


def parse_subject(subject: str) -> Optional[dict[str, str]]:
    m = _SUBJECT_RE.search(subject or "")
    if not m:
        return None
    return {
        "status": re.sub(r"\s+", " ", m.group(1)).strip(),
        "user": m.group(2).strip(),
        "set": m.group(3).strip(),
        "jobId": m.group(4).strip(),
    }


# A distinct 1Backup notification type for a schedule that never even ran
# (as opposed to a completed/skipped/interrupted job): e.g.
#   "Scheduled backup, ltvetecube > BUS > 2026-09-08-13-15-00, was missed"
# No brackets, no leading "Backup Report"/"Report di backup", and the
# status trails AFTER the job id rather than leading in brackets.
_MISSED_SCHEDULE_RE = re.compile(
    r"^(?:Scheduled backup|Il backup programmato),\s*(.*?)\s*>\s*(.*?)\s*>\s*(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}),\s*(.*)$",
    re.IGNORECASE,
)


def parse_missed_schedule_subject(subject: str) -> Optional[dict[str, str]]:
    m = _MISSED_SCHEDULE_RE.match((subject or "").strip())
    if not m:
        return None
    user, set_, job_id, tail = m.groups()
    return {
        "status": tail.strip(),
        "user": user.strip(),
        "set": set_.strip(),
        "jobId": job_id.strip(),
    }


# Another distinct 1Backup notification type: an ACCOUNT-level (not a
# single job's) quota being exceeded, e.g.:
#   "Account, aurogene, has exceeded its backup quota"
# Comma-separated, no job/date at all - always treated as CRITICAL
# regardless of the generic keyword-based classification (which treats a
# single job's "quota"/"exceeded" mention as only a WARNING), since this
# is the whole account's storage being over its limit, not one job.
_ACCOUNT_QUOTA_RE = re.compile(
    r"^Account,\s*(.*?),\s*(has exceeded its backup quota)\s*$",
    re.IGNORECASE,
)


def parse_account_quota_subject(subject: str) -> Optional[dict[str, str]]:
    m = _ACCOUNT_QUOTA_RE.match((subject or "").strip())
    if not m:
        return None
    user, tail = m.groups()
    return {"user": user.strip(), "status": tail.strip()}


# A second, unrelated report format seen from notifiche_backup@startappitalia.it
# ("SQL Master Backup" / "SQL Server Management Tool" notifications), e.g.:
#   "[LOGGIA] ✅ SQL COMPLETATO — Backup DB Principali (FULL) — 18/09/2026 22:05"
#   "[NAICI SRL] ✅ SQL COMPLETATO — Pianificazione (2 DB) — 19/09/2026 02:03"
# Unlike the 1Backup format, the customer/user name is the FIRST bracketed
# token, and fields are separated by em dashes rather than ">".
_BRACKET_DASH_SUBJECT_RE = re.compile(
    r"^\[(.*?)\]\s*(.*?)\s*[-–—―]\s*(.*?)\s*[-–—―]\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\s*$"
)


def parse_bracket_dash_subject(subject: str) -> Optional[dict[str, str]]:
    m = _BRACKET_DASH_SUBJECT_RE.match((subject or "").strip())
    if not m:
        return None
    user, status, job, date_str, time_str = m.groups()
    return {
        "user": user.strip(),
        "status": re.sub(r"\s+", " ", status).strip(),
        "set": job.strip(),
        "date": date_str.strip(),
        "time": time_str.strip(),
    }


def extract_sql_backup_detail(raw_text: str) -> str:
    """Pull the free-form "Dettaglio:" section out of a bracket-dash-format
    body (see parse_bracket_dash_subject) - the per-database breakdown
    that the header status line alone doesn't carry, similar in spirit to
    extract_log_excerpt() for the PDF-based format."""
    m = re.search(r"Dettaglio:\s*\n+(.*?)\s*Messaggio automatico", raw_text, re.DOTALL)
    if not m:
        return ""
    lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
    return " | ".join(lines)


# A third, unrelated report format: QNAP NAS "Hybrid Backup Sync" device
# notifications, e.g. subject "[Info][Hybrid Backup Sync] Notifica dal
# dispositivo: NASQNAP" with a body of colon-separated "Label: value"
# lines (Nome NAS / Gravità / Data/Ora / Nome App / Categoria / Messaggio).
# Detected by the presence of "Gravità:" - the customer/user here is the
# EMAIL SENDER address itself (e.g. elio@inlinea.tv), not anything in the
# subject or body, since QNAP devices are typically configured to send
# their own notifications directly from an owner's mailbox.
_QNAP_FIELD_RE = re.compile(
    r"^(Nome NAS|Gravità|Data/Ora|Nome App|Categoria)\s*:\s*(.*)$"
)
_QNAP_SEVERITY_MAP = {
    "info": "INFO",
    "informazioni": "INFO",
    "avviso": "WARNING",
    "warning": "WARNING",
    "errore": "CRITICAL",
    "error": "CRITICAL",
}


def parse_qnap_notification(raw_text: str) -> Optional[dict[str, str]]:
    if "Gravità" not in raw_text:
        return None

    fields: dict[str, str] = {}
    for line in raw_text.split("\n"):
        m = _QNAP_FIELD_RE.match(line.strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()

    if "Gravità" not in fields:
        return None

    msg_match = re.search(r"Messaggio:\s*(.*?)(?:\n\s*\n|\Z)", raw_text, re.DOTALL)
    message = re.sub(r"\s+", " ", msg_match.group(1)).strip() if msg_match else ""

    return {
        "nas_name": fields.get("Nome NAS", ""),
        "severity_label": fields.get("Gravità", ""),
        "date_time": fields.get("Data/Ora", ""),
        "app_name": fields.get("Nome App", ""),
        "category": fields.get("Categoria", ""),
        "message": message,
    }


def normalize_timestamp(raw: str) -> str:
    """Normalize the date formats seen across supported report formats into
    a single sortable ISO 8601 string (YYYY-MM-DDTHH:MM:SS), so the
    dashboard can reliably sort/compare dates regardless of which field or
    sender format they came from:

      - "17/09/2026 23:10:22 CEST"  (1Backup "Start - End" field)
      - "2026-09-17-23-00-00"       (1Backup "Backup Job" field, used as
                                      a fallback when Start - End is absent)
      - "2026/09/18 21:00:36"       (QNAP "Data/Ora" field)

    Anything not matching a known pattern is returned unchanged.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})", raw)
    if m:
        day, month, year, hh, mm, ss = m.groups()
        return f"{year}-{month}-{day}T{hh}:{mm}:{ss}"

    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", raw)
    if m:
        year, month, day, hh, mm, ss = m.groups()
        return f"{year}-{month}-{day}T{hh}:{mm}:{ss}"

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})$", raw)
    if m:
        year, month, day, hh, mm, ss = m.groups()
        return f"{year}-{month}-{day}T{hh}:{mm}:{ss}"

    return raw


def _clean_key_part(s: str) -> str:
    """Collapse embedded CR/LF (e.g. from folded email header lines) and
    other whitespace runs into single spaces. Values containing raw
    CRLF sequences survive a round-trip through an HTML attribute
    (write markup -> browser re-parses it) with the \\r silently
    dropped, which would make a later strict string comparison against
    the original Python-side value fail - used for natural_key, which
    the dashboard relies on as a stable, exact identifier for each row
    (e.g. to remember which rows the user expanded)."""
    return re.sub(r"\s+", " ", s or "").strip()


def _to_record(fields: dict[str, str], source: str, extra_id: str,
               subj_parsed: Optional[dict[str, str]],
               log_excerpt: str = "") -> dict[str, Any]:
    status = fields.get("Job Status") or (subj_parsed or {}).get("status", "") or ""
    start_end = fields.get("Start - End", "")
    raw_timestamp = (
        start_end.split(" - ")[0].strip() if start_end
        else fields.get("Backup Job") or (subj_parsed or {}).get("jobId", "") or ""
    )
    timestamp = normalize_timestamp(raw_timestamp)
    return {
        "natural_key": "|".join([
            _clean_key_part(source), _clean_key_part(extra_id),
            _clean_key_part(fields.get("Destination", "")),
        ]),
        "source": source,
        "timestamp": timestamp,
        "user": fields.get("User") or (subj_parsed or {}).get("user", "") or "",
        "backup_set": fields.get("Backup Set") or (subj_parsed or {}).get("set", "") or "",
        "destination": fields.get("Destination", ""),
        "status": status,
        "data_size": fields.get("Data Size", ""),
        "ip_address": fields.get("IP Address", ""),
        "start_end": start_end,
        "job_id": fields.get("Backup Job") or (subj_parsed or {}).get("jobId", "") or "",
        "severity": classify_severity(status),
        "log_excerpt": log_excerpt,
    }


def decode_mime_header(raw: str) -> str:
    """Decode an RFC 2047 encoded-word header value (e.g. '=?utf-8?q?...?='
    used by mail clients to carry non-ASCII characters, such as Italian
    accents, in headers like Subject) into plain text. A header that
    isn't MIME-encoded is returned unchanged. Without this, parse_subject
    would silently fail to match against the raw encoded-word form -
    matching poller.py's own decode_mime_words(), kept here too so this
    module works standalone on whatever msg.get('Subject') returns.

    Also normalizes to Unicode NFC (composed) form: an accented character
    can arrive as either a single precomposed codepoint or a base letter
    plus a separate combining accent - visually identical but a different
    byte sequence, which silently breaks any literal-text regex/substring
    match against it (this has bitten several of the format-specific
    parsers below; normalizing once here avoids the same class of bug
    recurring everywhere else that matches literal accented words)."""
    if not raw:
        return ""
    try:
        decoded = str(make_header(decode_header(raw)))
    except Exception:
        decoded = raw
    return unicodedata.normalize("NFC", decoded)


def parse_eml_bytes(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse a raw .eml message (as bytes) into a list of report records."""
    msg: Message = email.message_from_bytes(raw_bytes)
    subject = decode_mime_header(msg.get("Subject", "") or "")
    date_header = msg.get("Date", "") or ""

    def get_body_text() -> str:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")
                        # NFC-normalize for the same reason as
                        # decode_mime_header() above - literal accented
                        # words like "Gravità" are matched against this
                        # text further down.
                        return unicodedata.normalize("NFC", strip_tags(html))
                except Exception:
                    continue
        return ""

    # QNAP NAS "Hybrid Backup Sync" device notifications (see
    # parse_qnap_notification) - checked before the other formats since
    # its detection is based on the BODY containing "Gravità:", not the
    # subject, and its "user" is the sender's own email address rather
    # than anything extracted from the message content at all.
    body_text = get_body_text()
    qnap = parse_qnap_notification(body_text) if body_text else None
    if qnap:
        from_addr = parseaddr(msg.get("From", "") or "")[1]
        severity = _QNAP_SEVERITY_MAP.get(qnap["severity_label"].lower())
        status = qnap["message"] or qnap["severity_label"]
        if severity is None:
            severity = classify_severity(status)
        return [{
            "natural_key": "|".join([
                _clean_key_part(source), _clean_key_part(date_header or subject), "",
            ]),
            "source": source,
            "timestamp": normalize_timestamp(qnap["date_time"]),
            "user": from_addr or qnap["nas_name"],
            "backup_set": qnap["nas_name"] or qnap["app_name"],
            "destination": "",
            "status": status,
            "data_size": "",
            "ip_address": "",
            "start_end": "",
            "job_id": "",
            "severity": severity,
            "log_excerpt": f"{qnap['app_name']} — {qnap['category']}".strip(" —"),
        }]

    # The "SQL Master Backup" / "SQL Server Management Tool" notifications
    # use a completely different subject AND body layout from the 1Backup
    # format (see parse_bracket_dash_subject) - handled as its own path
    # entirely, since the LABEL_MAP-based body extraction below doesn't
    # apply to it at all (different field labels, no "Job Status" etc.).
    bracket_parsed = parse_bracket_dash_subject(subject)
    if bracket_parsed:
        detail = extract_sql_backup_detail(body_text) if body_text else ""
        raw_timestamp = f"{bracket_parsed['date']} {bracket_parsed['time']}:00"
        status = bracket_parsed["status"]
        return [{
            "natural_key": "|".join([
                _clean_key_part(source), _clean_key_part(date_header or subject), "",
            ]),
            "source": source,
            "timestamp": normalize_timestamp(raw_timestamp),
            "user": bracket_parsed["user"],
            "backup_set": bracket_parsed["set"],
            "destination": "",
            "status": status,
            "data_size": "",
            "ip_address": "",
            "start_end": "",
            "job_id": "",
            "severity": classify_severity(status),
            "log_excerpt": detail,
        }]

    # Account-level quota-exceeded notification (see
    # parse_account_quota_subject) - always CRITICAL, not run through the
    # generic keyword classifier, since this is a distinct, more serious
    # condition than a single job's destination-quota warning.
    account_quota = parse_account_quota_subject(subject)
    if account_quota:
        status = account_quota["status"]
        try:
            dt = parsedate_to_datetime(date_header) if date_header else None
            timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else ""
        except (TypeError, ValueError):
            timestamp = ""
        return [{
            "natural_key": "|".join([
                _clean_key_part(source), _clean_key_part(date_header or subject), "",
            ]),
            "source": source,
            "timestamp": timestamp,
            "user": account_quota["user"],
            "backup_set": "",
            "destination": "",
            "status": status,
            "data_size": "",
            "ip_address": "",
            "start_end": "",
            "job_id": "",
            "severity": "CRITICAL",
            "log_excerpt": "",
        }]

    subj_parsed = parse_subject(subject) or parse_missed_schedule_subject(subject)

    plain_chunks: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/html":
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    plain_chunks.append(unicodedata.normalize("NFC", strip_tags(html)))
            except Exception:
                continue

    blocks: list[dict[str, str]] = []
    for chunk in plain_chunks:
        blocks.extend(extract_records_from_text(chunk))

    if not blocks:
        # Fall back to subject-only info if the body couldn't be parsed
        return [_to_record({}, source, date_header or subject, subj_parsed)]

    extra_id = date_header or subject
    return [
        _to_record(fields, source, f"{extra_id}#{idx}", subj_parsed)
        for idx, fields in enumerate(blocks)
    ]


_LOG_LINE_START_RE = re.compile(
    r"^(\d+)\s+(info|error|warn|warning|debug)\s+(\S+\s+\S+)\s+(.*)$",
    re.IGNORECASE,
)
_LOG_INTERESTING_KEYS = [
    "error", "fail", "warn", "exception", "timeout", "denied", "retry",
    "could not", "unable", "corrupt",
]


def extract_log_excerpt(raw_text: str, max_chars: int = 2000) -> str:
    """Pull out the 'interesting' (non-routine) lines from the numbered
    "Backup Logs" table found in 1Backup/CoreTech PDF reports - the
    detailed retry/timeout/error lines that never appear anywhere else
    in the report (the header fields only carry a short overall status
    like "OK" or "Storage Quota Exceeded", not the underlying cause).

    PDF text extraction wraps a single logical log line across several
    physical text lines mid-sentence (e.g. a long exception message), so
    this reassembles each numbered entry (recognized by its "N info/
    error/warn TIMESTAMP ..." prefix) before filtering, rather than
    filtering line-by-line - otherwise a wrapped continuation line with
    no prefix of its own would be silently dropped even if the entry it
    belongs to is relevant.
    """
    m = re.search(r"Backup Logs\b(.*?)(?:\nBackup Files\b|\Z)", raw_text, re.DOTALL)
    if not m:
        return ""
    section = m.group(1)

    entries: list[str] = []
    current: Optional[str] = None
    for line in section.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        start_match = _LOG_LINE_START_RE.match(line.strip())
        if start_match:
            if current:
                entries.append(current)
            current = start_match.group(0)
        elif current is not None:
            current = current.rstrip() + " " + line.strip()
    if current:
        entries.append(current)

    interesting = [e for e in entries if any(k in e.lower() for k in _LOG_INTERESTING_KEYS)]
    text = "\n".join(re.sub(r"\s+", " ", e).strip() for e in interesting)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def parse_pdf_bytes(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse a PDF attachment's text into a list of report records."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []

    import io
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        lines: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))
    except Exception:
        # A misidentified or corrupt attachment shouldn't take down parsing
        # of the rest of the email - the HTML body (if any) still stands.
        return []

    full_text = unicodedata.normalize("NFC", "\n".join(lines))
    blocks = extract_records_from_text(full_text)
    log_excerpt = extract_log_excerpt(full_text)
    return [
        _to_record(fields, source, str(idx), None, log_excerpt=log_excerpt)
        for idx, fields in enumerate(blocks)
    ]


def _find_matching_record(records: list[dict[str, Any]], pdf_destination: str) -> Optional[dict[str, Any]]:
    """Find the HTML-body record this PDF-derived record corresponds to.

    The PDF's Destination field carries a longer form than the HTML
    body's (e.g. body "DC2-1Backup" vs PDF "DC2-1Backup (Predefined
    Destination)", body "1Backup" vs PDF "1Backup (1Backup)"), so an
    exact string match on destination would silently fail for every
    email that has both a body and a PDF - which is the common case.
    Falls back to a prefix check in either direction.
    """
    for r in records:
        d = r["destination"]
        if d == pdf_destination:
            return r
    for r in records:
        d = r["destination"]
        if d and (pdf_destination.startswith(d) or d.startswith(pdf_destination)):
            return r
    return None


def parse_email_message(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse an email, combining records found in its HTML body AND in any
    PDF attachments, de-duplicated by natural_key (HTML-body records win,
    since they are generally cleaner to parse) - EXCEPT for log_excerpt,
    which the HTML body never carries at all (only the PDF's "Backup
    Logs" table has it), so it's merged into the winning HTML-body record
    rather than lost whenever both a body and a matching PDF record exist
    for the same destination, which is the common case.

    PDF attachments are only opened and parsed for records whose severity
    (already known from the subject/HTML body at this point) is WARNING
    or CRITICAL. Extracting text from a PDF is far slower than parsing
    the HTML body or subject, and log_excerpt only exists to explain a
    problem - for the (typically large majority of) plain "OK" reports
    there is nothing worth extracting, so skipping them here is a large,
    safe speedup with no loss of anything the dashboard actually shows.
    """
    records = parse_eml_bytes(raw_bytes, source)

    if not any(r["severity"] in ("WARNING", "CRITICAL") for r in records):
        return records

    msg: Message = email.message_from_bytes(raw_bytes)
    for part in msg.walk():
        # Only treat this as a PDF if it genuinely has a filename ending
        # in .pdf, or a declared application/pdf type - NOT if it simply
        # lacks a filename (e.g. the HTML body part itself has none, and
        # defaulting that case to a made-up "attachment.pdf" name would
        # wrongly match the .pdf-suffix check below and try to parse the
        # HTML body's raw bytes as a PDF).
        real_filename = part.get_filename() or ""
        is_pdf = (
            part.get_content_type() == "application/pdf"
            or real_filename.lower().endswith(".pdf")
        )
        if is_pdf:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            filename = real_filename or "attachment.pdf"
            pdf_records = parse_pdf_bytes(payload, f"{source}::{filename}")
            for r in pdf_records:
                existing = _find_matching_record(records, r["destination"])
                if existing is not None:
                    if not existing.get("log_excerpt") and r.get("log_excerpt"):
                        existing["log_excerpt"] = r["log_excerpt"]
                elif r["severity"] in ("WARNING", "CRITICAL"):
                    # An unmatched PDF record with a genuine problem is a
                    # real destination the HTML body didn't mention at
                    # all, worth surfacing on its own. An unmatched OK/
                    # INFO one is, in practice, almost always the SAME
                    # destination the body already captured cleanly,
                    # just under a slightly different PDF-only spelling
                    # that _find_matching_record's prefix check didn't
                    # catch - appending it would just duplicate an
                    # already-correct row under an ugly "::file.pdf"
                    # source, with nothing new to show.
                    records.append(r)

    return records
