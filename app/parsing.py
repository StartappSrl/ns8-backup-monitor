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
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
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
}

CRITICAL_KEYS = [
    # English
    "failed", "error", "could not", "cannot", "unable", "corrupt",
    "denied", "timeout", "timed out", "disk full", "not enough space",
    "authentication",
    # Italiano
    "fallito", "fallita", "errore", "impossibile", "corrotto", "negato",
    "scaduto", "spazio esaurito", "autenticazione",
]
WARNING_KEYS = [
    # English
    "skipped", "still running", "warning", "quota", "partial", "retry",
    "delayed", "exceeded",
    # Italiano
    "saltato", "saltata", "ancora in corso", "attenzione", "parziale",
    "ritardo", "superata", "superato",
]
OK_KEYS = [
    # English
    "success", "completed successfully", "no errors",
    # Italiano
    "riuscito", "riuscita", "completato con successo", "nessun errore",
]


def classify_severity(status_text: str) -> str:
    s = (status_text or "").lower().strip()
    if any(k in s for k in CRITICAL_KEYS):
        return "CRITICAL"
    if any(k in s for k in WARNING_KEYS):
        return "WARNING"
    # "OK" is often followed by parenthetical detail, e.g.
    # "OK (no files backed up)" / "OK (nessun file sottoposto a backup)" -
    # match it as a prefix rather than requiring an exact "ok" status.
    if s.startswith("ok"):
        return "OK"
    if any(k in s for k in OK_KEYS):
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


def normalize_timestamp(raw: str) -> str:
    """Normalize the two date formats seen in these reports into a single
    sortable ISO 8601 string (YYYY-MM-DDTHH:MM:SS), so the dashboard can
    reliably sort/compare dates regardless of which field they came from:

      - "17/09/2026 23:10:22 CEST"  (from the "Start - End" field)
      - "2026-09-17-23-00-00"       (from the "Backup Job" field, used as
                                      a fallback when Start - End is absent)

    Anything not matching either pattern is returned unchanged.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})", raw)
    if m:
        day, month, year, hh, mm, ss = m.groups()
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
    module works standalone on whatever msg.get('Subject') returns."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def parse_eml_bytes(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse a raw .eml message (as bytes) into a list of report records."""
    msg: Message = email.message_from_bytes(raw_bytes)
    subject = decode_mime_header(msg.get("Subject", "") or "")
    date_header = msg.get("Date", "") or ""
    subj_parsed = parse_subject(subject)

    plain_chunks: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/html":
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    plain_chunks.append(strip_tags(html))
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

    full_text = "\n".join(lines)
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
    for the same destination, which is the common case."""
    records = parse_eml_bytes(raw_bytes, source)

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
                if existing is None:
                    records.append(r)
                elif not existing.get("log_excerpt") and r.get("log_excerpt"):
                    existing["log_excerpt"] = r["log_excerpt"]

    return records
