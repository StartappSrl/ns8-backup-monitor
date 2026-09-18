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
    "failed", "error", "could not", "cannot", "unable", "corrupt",
    "denied", "timeout", "timed out", "disk full", "not enough space",
    "authentication",
]
WARNING_KEYS = [
    "skipped", "still running", "warning", "quota", "partial", "retry",
    "delayed",
]
OK_KEYS = ["success", "completed successfully", "no errors"]


def classify_severity(status_text: str) -> str:
    s = (status_text or "").lower()
    if any(k in s for k in CRITICAL_KEYS):
        return "CRITICAL"
    if any(k in s for k in WARNING_KEYS):
        return "WARNING"
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
    r"Backup Report\s*\[(.*?)\]\s*>\s*(.*?)\s*>\s*(.*?)\s*>\s*Job\s*(.*)",
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


def _to_record(fields: dict[str, str], source: str, extra_id: str,
               subj_parsed: Optional[dict[str, str]]) -> dict[str, Any]:
    status = fields.get("Job Status") or (subj_parsed or {}).get("status", "") or ""
    start_end = fields.get("Start - End", "")
    timestamp = start_end.split(" - ")[0].strip() if start_end else fields.get("Backup Job", "")
    return {
        "natural_key": "|".join([
            source, extra_id, fields.get("Destination", ""),
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
    }


def parse_eml_bytes(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse a raw .eml message (as bytes) into a list of report records."""
    msg: Message = email.message_from_bytes(raw_bytes)
    subject = msg.get("Subject", "") or ""
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


def parse_pdf_bytes(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse a PDF attachment's text into a list of report records."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []

    import io
    reader = PdfReader(io.BytesIO(raw_bytes))
    lines: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        lines.extend(text.split("\n"))

    blocks = extract_records_from_text("\n".join(lines))
    return [_to_record(fields, source, str(idx), None) for idx, fields in enumerate(blocks)]


def parse_email_message(raw_bytes: bytes, source: str) -> list[dict[str, Any]]:
    """Parse an email, combining records found in its HTML body AND in any
    PDF attachments, de-duplicated by natural_key (HTML-body records win,
    since they are generally cleaner to parse)."""
    records = parse_eml_bytes(raw_bytes, source)

    msg: Message = email.message_from_bytes(raw_bytes)
    for part in msg.walk():
        if part.get_content_type() == "application/pdf":
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            filename = part.get_filename() or "attachment.pdf"
            pdf_records = parse_pdf_bytes(payload, f"{source}::{filename}")
            existing_keys = {r["destination"] for r in records}
            for r in pdf_records:
                if r["destination"] not in existing_keys:
                    records.append(r)
                    existing_keys.add(r["destination"])

    return records
