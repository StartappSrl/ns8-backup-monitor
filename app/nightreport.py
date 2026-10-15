"""PDF "report notturno": one overnight backup window, ready to print or send.

Pure presentation: build_night_pdf() takes the dict produced by
db.night_summary() and returns the PDF bytes, so it can be exercised
without a database or a web request.

Two things are deliberate:

* Every dynamic string (client names, statuses, ...) is reduced to what the
  built-in Helvetica font can draw and XML-escaped before it reaches a
  reportlab Paragraph. Characters outside WinAnsi (emoji, arrows, check
  marks) would otherwise print as solid black boxes, and an unescaped
  "<img src=...>" in a client name would be interpreted as markup.
* Page numbers ("Pagina X di Y") need the final page count, hence the
  two-pass canvas below.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

INK = colors.HexColor("#1B222C")
MUTED = colors.HexColor("#5B6472")
FAINT = colors.HexColor("#8A93A1")
LINE = colors.HexColor("#DDE2E9")
PANEL = colors.HexColor("#F8F9FB")
BAND = colors.HexColor("#1B222C")
CRIT, CRIT_BG = colors.HexColor("#C43D3D"), colors.HexColor("#FBEAEA")
WARN, WARN_BG = colors.HexColor("#A9700F"), colors.HexColor("#FBF1DE")
OK, OK_BG = colors.HexColor("#237A50"), colors.HexColor("#E6F5EC")

_SEV_LABEL = {"CRITICAL": "Critico", "WARNING": "Attenzione", "OK": "OK", "INFO": "OK"}
_SEV_COLOR = {"CRITICAL": CRIT, "WARNING": WARN, "OK": OK, "INFO": OK}
_SEV_RANK = {"CRITICAL": 3, "WARNING": 2, "OK": 1, "INFO": 0}

_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_CELL_CHARS = 200

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
CONTENT_W = PAGE_W - 2 * MARGIN


def _t(value, limit: int = MAX_CELL_CHARS) -> str:
    """Make a dynamic value safe to put in a Paragraph: printable in the
    built-in font, bounded in length, and XML-escaped."""
    s = _CTRL_RE.sub(" ", str(value if value is not None else ""))
    s = s.encode("cp1252", "replace").decode("cp1252")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "\u2026"
    return escape(s)


def _day(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (iso or "")


def _day_short(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}/{m.group(2)}" if m else (iso or "")


def _when(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", iso or "")
    return f"{m.group(3)}/{m.group(2)} {m.group(4)}:{m.group(5)}" if m else (iso or "")


def _styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Helvetica", textColor=INK, leading=11.5, fontSize=8.5, alignment=TA_LEFT)
    return {
        "cell": ParagraphStyle("cell", **base),
        "cell_muted": ParagraphStyle("cell_muted", **{**base, "textColor": MUTED}),
        "head": ParagraphStyle("head", **{**base, "fontName": "Helvetica-Bold", "fontSize": 7.5,
                                          "textColor": MUTED, "leading": 9}),
        "h2": ParagraphStyle("h2", **{**base, "fontName": "Helvetica-Bold", "fontSize": 11.5,
                                      "leading": 14, "spaceBefore": 14, "spaceAfter": 6}),
        "note": ParagraphStyle("note", **{**base, "textColor": MUTED, "fontSize": 8, "leading": 10.5}),
        "kpi_n": ParagraphStyle("kpi_n", **{**base, "fontName": "Helvetica-Bold", "fontSize": 22, "leading": 24}),
        "kpi_l": ParagraphStyle("kpi_l", **{**base, "textColor": MUTED, "fontSize": 8, "leading": 10}),
        "band_small": ParagraphStyle("band_small", **{**base, "textColor": colors.HexColor("#AEB6C2"),
                                                      "fontSize": 8, "leading": 10}),
        "band_title": ParagraphStyle("band_title", **{**base, "textColor": colors.white,
                                                      "fontName": "Helvetica-Bold", "fontSize": 20, "leading": 24}),
        "band_right": ParagraphStyle("band_right", **{**base, "textColor": colors.HexColor("#AEB6C2"),
                                                      "fontSize": 8, "leading": 11, "alignment": TA_RIGHT}),
        "verdict": ParagraphStyle("verdict", **{**base, "fontSize": 9.5, "leading": 13,
                                                "fontName": "Helvetica-Bold"}),
    }


class _NumberedCanvas(canvas.Canvas):
    """Draws "Pagina X di Y" on every page once the total is known."""

    def __init__(self, *args, footer_left: str = "", header_title: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._footer_left = footer_left
        self._header_title = header_title
        self._saved_states: list[dict] = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for i, state in enumerate(self._saved_states):
            self.__dict__.update(state)
            self._decorate(i + 1, total)
            super().showPage()
        super().save()

    def _decorate(self, number: int, total: int):
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(MARGIN, 12 * mm, PAGE_W - MARGIN, 12 * mm)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(FAINT)
        self.drawString(MARGIN, 8 * mm, self._footer_left)
        self.drawRightString(PAGE_W - MARGIN, 8 * mm, f"Pagina {number} di {total}")
        if number > 1:
            self.drawString(MARGIN, PAGE_H - 10 * mm, self._header_title)
            self.setStrokeColor(LINE)
            self.line(MARGIN, PAGE_H - 12 * mm, PAGE_W - MARGIN, PAGE_H - 12 * mm)
        self.restoreState()


def _table(rows: list[list], widths: list[float], repeat: int = 1, extra: list | None = None) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=repeat, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def build_night_pdf(summary: dict, sender: str = "", generated_at: datetime | None = None,
                    version: str = "") -> bytes:
    st = _styles()
    generated_at = generated_at or datetime.now()
    bs = summary.get("by_severity", {})
    n_crit, n_warn = bs.get("CRITICAL", 0), bs.get("WARNING", 0)
    n_ok = bs.get("OK", 0) + bs.get("INFO", 0)
    missing = summary.get("missing", [])
    problems = summary.get("problems", [])
    crit = [p for p in problems if p["severity"] == "CRITICAL"]
    warn = [p for p in problems if p["severity"] == "WARNING"]
    title = f"Notte del {_day(summary['date'])}"
    scope = f"Mittente: {_t(sender, 80)}" if sender else "Tutti i mittenti"

    story: list = []

    def heading(markup: str) -> None:
        # Not keepWithNext: that would glue the heading to the ENTIRE table and push a
        # table that fits on one page wholesale to the next, leaving a big hole.
        story.append(CondPageBreak(38 * mm))
        story.append(Paragraph(markup, st["h2"]))

    # -- title band ---------------------------------------------------------
    left = [
        Paragraph("BACKUP MONITOR · REPORT NOTTURNO", st["band_small"]),
        Spacer(1, 3),
        Paragraph(title, st["band_title"]),
        Spacer(1, 3),
        Paragraph(f"Finestra: {_when(summary['start'])} &ndash; {_when(summary['end'])} · {scope}",
                  st["band_small"]),
    ]
    right = [Paragraph(f"Generato il {generated_at:%d/%m/%Y %H:%M}", st["band_right"])]
    if version:
        right.append(Paragraph(f"v{_t(version, 30)}", st["band_right"]))
    band = Table([[left, right]], colWidths=[CONTENT_W * 0.72, CONTENT_W * 0.28])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    story += [band, Spacer(1, 10)]

    # -- KPI row ------------------------------------------------------------
    def kpi(number, label, color):
        return [Paragraph(f'<font color="{color.hexval().replace("0x", "#")}">{number}</font>', st["kpi_n"]),
                Paragraph(label, st["kpi_l"])]
    kpis = [
        (summary.get("total_clients", 0), "Clienti con report", INK),
        (n_ok, "OK", OK), (n_warn, "Attenzione", WARN), (n_crit, "Critici", CRIT),
        (len(missing) if not summary.get("insufficient_history") else "n/d", "Mancanti", CRIT),
    ]
    w = CONTENT_W / len(kpis)
    kt = Table([[kpi(n, l, c) for n, l, c in kpis]], colWidths=[w] * len(kpis))
    ks = [("BOX", (0, 0), (-1, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (-1, -1), colors.white),
          ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 10),
          ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]
    for i, (_n, _l, c) in enumerate(kpis):
        ks.append(("LINEABOVE", (i, 0), (i, 0), 2.2, c))
        if i:
            ks.append(("LINEBEFORE", (i, 0), (i, 0), 0.5, LINE))
    kt.setStyle(TableStyle(ks))
    story += [kt, Spacer(1, 8)]

    # -- verdict ------------------------------------------------------------
    n_missing = len(missing)
    if n_crit or n_missing:
        bits = []
        if n_crit:
            bits.append(f"{n_crit} client{'e' if n_crit == 1 else 'i'} in stato critico")
        if n_missing:
            bits.append(f"{n_missing} client{'e' if n_missing == 1 else 'i'} mancant{'e' if n_missing == 1 else 'i'}")
        if n_warn:
            bits.append(f"{n_warn} in attenzione")
        verdict, vcol, vbg = "Anomalie rilevate: " + ", ".join(bits) + ".", CRIT, CRIT_BG
    elif n_warn:
        verdict, vcol, vbg = f"Nessun cliente critico o mancante; {n_warn} in attenzione.", WARN, WARN_BG
    elif summary.get("total_clients", 0) == 0:
        verdict, vcol, vbg = "Nessun report ricevuto nella finestra considerata.", WARN, WARN_BG
    else:
        verdict, vcol, vbg = "Nessuna anomalia rilevata nella notte.", OK, OK_BG
    if summary.get("insufficient_history"):
        verdict += " I clienti mancanti non sono valutabili: storico insufficiente."
    vp = Paragraph(f'<font color="{vcol.hexval().replace("0x", "#")}">{escape(verdict)}</font>', st["verdict"])
    vt = Table([[vp]], colWidths=[CONTENT_W])
    vt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), vbg), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story.append(vt)

    # -- problems -----------------------------------------------------------
    def problem_section(label, items, color):
        if not items:
            return
        heading(f'<font color="{color.hexval().replace("0x", "#")}">{label}</font> '
                f'<font color="#8A93A1" size="9">({len(items)})</font>')
        head = [Paragraph(h, st["head"]) for h in ("CLIENTE", "BACKUP SET", "DESTINAZIONE", "STATO", "ORA")]
        rows = [head]
        for p in items:
            rows.append([
                Paragraph(f"<b>{_t(p['user'], 60)}</b>", st["cell"]),
                Paragraph(_t(p["backup_set"], 60), st["cell"]),
                Paragraph(_t(p["destination"], 60) or "&mdash;", st["cell_muted"]),
                Paragraph(_t(p["status"]) or "&mdash;", st["cell"]),
                Paragraph(_when(p.get("timestamp", "")), st["cell_muted"]),
            ])
        story.append(_table(rows, [34 * mm, 34 * mm, 30 * mm, 60 * mm, 20 * mm],
                            extra=[("LINEBEFORE", (0, 1), (0, -1), 2.2, color)]))

    problem_section("Critici", crit, CRIT)
    problem_section("Attenzione", warn, WARN)

    # -- missing ------------------------------------------------------------
    heading(f'<font color="#C43D3D">Clienti mancanti</font> '
            f'<font color="#8A93A1" size="9">({"n/d" if summary.get("insufficient_history") else len(missing)})</font>')
    if summary.get("insufficient_history"):
        story.append(Paragraph("Storico insufficiente: servono almeno 3 notti confrontabili con dati per "
                               "stabilire quali clienti ci si aspetta.", st["note"]))
    elif not missing:
        story.append(Paragraph("Nessun cliente mancante: tutti quelli attesi hanno inviato almeno un report.",
                               st["note"]))
    else:
        head = [Paragraph(h, st["head"]) for h in ("CLIENTE", "PRESENTE NELLE NOTTI PRECEDENTI", "ULTIMA NOTTE VISTA")]
        rows = [head] + [[
            Paragraph(f"<b>{_t(m['user'], 60)}</b>", st["cell"]),
            Paragraph(f"{m['seen_in']} su {m['of']}", st["cell"]),
            Paragraph(_day_short(m["last_night"]), st["cell_muted"]),
        ] for m in missing]
        story.append(_table(rows, [78 * mm, 62 * mm, 38 * mm],
                            extra=[("LINEBEFORE", (0, 1), (0, -1), 2.2, CRIT)]))
    if not summary.get("insufficient_history"):
        basis = ("stesso tipo di giorno (feriale o weekend)" if summary.get("comparison_basis") == "same_day_type"
                 else "tutte le notti precedenti con dati")
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"Un cliente &egrave; considerato atteso se compare in pi&ugrave; della met&agrave; delle "
            f"{summary.get('considered_nights', 0)} notti confrontate ({basis}).", st["note"]))

    # -- all clients --------------------------------------------------------
    clients = sorted(summary.get("clients", []),
                     key=lambda c: (-_SEV_RANK.get(c["severity"], 0), c["user"].lower()))
    heading(f'Elenco clienti <font color="#8A93A1" size="9">({len(clients)})</font>')
    if not clients:
        story.append(Paragraph("Nessun cliente ha inviato report in questa finestra.", st["note"]))
    else:
        head = [Paragraph(h, st["head"]) for h in ("CLIENTE", "ESITO PEGGIORE", "REPORT")]
        rows = [head]
        for c in clients:
            col = _SEV_COLOR.get(c["severity"], OK).hexval().replace("0x", "#")
            rows.append([
                Paragraph(_t(c["user"], 70), st["cell"]),
                Paragraph(f'<font color="{col}"><b>{_SEV_LABEL.get(c["severity"], "OK")}</b></font>', st["cell"]),
                Paragraph(str(c["reports"]), st["cell_muted"]),
            ])
        story.append(_table(rows, [100 * mm, 40 * mm, 38 * mm]))

    buf = io.BytesIO()
    footer = f"Backup Monitor{f' v{_t(version, 30)}' if version else ''} \u00b7 {title}"
    footer = footer.replace("&amp;", "&")
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=16 * mm, bottomMargin=17 * mm,
        title=f"Report backup - {title}", author="Backup Monitor", subject=f"Finestra {_when(summary['start'])} - {_when(summary['end'])}",
    )
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(
        *a, footer_left=footer.encode("cp1252", "replace").decode("cp1252"),
        header_title=title, **k))
    return buf.getvalue()
