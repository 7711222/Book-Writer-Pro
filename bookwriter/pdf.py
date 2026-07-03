"""PDF exports for the finished book — interior, front cover, back cover, full.

Unlike the stdlib EPUB/DOCX builders, real PDF layout (flowing text + pagination
+ raster cover art) is done with **reportlab**, an *optional* dependency:

    pip install -e ".[pdf]"     # reportlab + pillow

If reportlab isn't installed, every entry point raises ``PdfUnavailable`` with a
clear install hint and the rest of the app keeps working (the Publish screen's
PDF buttons surface the message instead of crashing).

Four artifacts, all sized to the book trim (default 6x9in):
  * ``interior_pdf``     — title page + chapters (no cover), flowing prose
  * ``front_cover_pdf``  — the AI cover art + title/author typography (or a clean
                           typographic cover when there's no art)
  * ``back_cover_pdf``   — blurb + author bio + imprint + barcode placeholder
  * ``full_pdf``         — front cover + interior + back cover in one file
"""
from __future__ import annotations

import io
from typing import Any, List, Optional, Tuple

from .kdp import _strip_tags  # reuse the HTML-strip used elsewhere


class PdfUnavailable(RuntimeError):
    """Raised when the optional reportlab dependency isn't installed."""


_INSTALL_HINT = (
    "PDF export needs the optional 'reportlab' package. Install it with: "
    'pip install -e ".[pdf]"   (or: pip install reportlab pillow pypdf)'
)


def pdf_available() -> bool:
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def _require():
    try:
        import reportlab  # noqa: F401
    except ImportError as e:  # pragma: no cover - environment dependent
        raise PdfUnavailable(_INSTALL_HINT) from e


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _paragraphs(text: str) -> List[str]:
    """Split prose into paragraphs on blank lines (mirrors kdp/print_export)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in text.split("\n\n")]
    paras = [b for b in blocks if b]
    if len(paras) <= 1:
        paras = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not paras:
        paras = [text.strip()] if text.strip() else []
    return [" ".join(p.split("\n")) for p in paras]


def _img_reader(art_bytes: bytes):
    from reportlab.lib.utils import ImageReader
    return ImageReader(io.BytesIO(art_bytes))


def _draw_cover_fill(c, art_bytes: bytes, w: float, h: float) -> None:
    """Draw art to COVER (fill) the w x h page, cropping the overflow, centered."""
    img = _img_reader(art_bytes)
    iw, ih = img.getSize()
    if not iw or not ih:
        return
    scale = max(w / iw, h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(img, (w - dw) / 2.0, (h - dh) / 2.0, width=dw, height=dh,
                preserveAspectRatio=False, mask="auto")


def _scrim(c, w: float, h: float) -> None:
    """A subtle dark vignette top+bottom so overlaid text stays legible on art."""
    from reportlab.lib.colors import Color
    band = h * 0.30
    steps = 24
    for i in range(steps):
        a = 0.62 * (1 - i / steps)
        c.setFillColor(Color(0, 0, 0, alpha=a))
        c.rect(0, h - band + i * (band / steps), w, band / steps + 1, stroke=0, fill=1)
        c.rect(0, band - (i + 1) * (band / steps), w, band / steps + 1, stroke=0, fill=1)


def _wrap_centered(c, text: str, font: str, size: int, w: float, max_lines: int) -> List[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words, lines, cur = (text or "").split(), [], ""
    limit = w * 0.86
    for word in words:
        trial = f"{cur} {word}".strip()
        if cur and stringWidth(trial, font, size) > limit:
            lines.append(cur)
            cur = word
            if len(lines) >= max_lines:
                break
        else:
            cur = trial
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------- #
# Interior
# --------------------------------------------------------------------------- #
def interior_pdf(graph, meta, *, trim: Tuple[float, float] = (6.0, 9.0)) -> bytes:
    """Title page + copyright + each chapter (heading + flowing prose). No cover."""
    _require()
    from reportlab.lib.pagesizes import inch
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    )
    from xml.sax.saxutils import escape as esc

    pagesize = (trim[0] * inch, trim[1] * inch)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=pagesize, title=meta.full_title(), author=meta.author_full(),
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
    )
    ss = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=ss["Normal"], fontName="Times-Roman",
                          fontSize=11.5, leading=16.5, firstLineIndent=16, spaceAfter=2)
    body0 = ParagraphStyle("Body0", parent=body, firstLineIndent=0)
    h_title = ParagraphStyle("BTitle", parent=ss["Title"], fontName="Times-Bold",
                             fontSize=30, leading=36, alignment=TA_CENTER)
    h_sub = ParagraphStyle("BSub", parent=ss["Normal"], fontName="Times-Italic",
                           fontSize=16, leading=22, alignment=TA_CENTER)
    h_chap = ParagraphStyle("Chap", parent=ss["Heading1"], fontName="Times-Bold",
                            fontSize=20, leading=26, alignment=TA_CENTER, spaceAfter=22)
    centered = ParagraphStyle("Cen", parent=ss["Normal"], fontName="Times-Roman",
                              fontSize=11, leading=16, alignment=TA_CENTER)

    story: List[Any] = [Spacer(1, trim[1] * inch * 0.28), Paragraph(esc(meta.title), h_title)]
    if meta.subtitle:
        story += [Spacer(1, 10), Paragraph(esc(meta.subtitle), h_sub)]
    story += [Spacer(1, 28), Paragraph(esc(meta.author_full()), h_sub)]

    # copyright page
    story.append(PageBreak())
    story += [Spacer(1, trim[1] * inch * 0.30),
              Paragraph(esc(meta.full_title()), centered),
              Paragraph(f"Copyright &#169; {esc(meta.author_full())}", centered),
              Paragraph("All rights reserved.", centered)]

    for n in sorted(graph.chapters):
        rec = graph.chapters[n]
        story.append(PageBreak())
        story.append(Paragraph(esc(rec.title), h_chap))
        for i, para in enumerate(_paragraphs(rec.text)):
            story.append(Paragraph(esc(para), body0 if i == 0 else body))

    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Covers
# --------------------------------------------------------------------------- #
def front_cover_pdf(meta, *, art_bytes: Optional[bytes] = None, ext: str = "png",
                    trim: Tuple[float, float] = (6.0, 9.0)) -> bytes:
    """Single-page front cover: AI art + title/author overlay, or a clean
    typographic cover when there's no artwork."""
    _require()
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import inch
    from reportlab.lib.colors import HexColor, white

    W, H = trim[0] * inch, trim[1] * inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))

    if art_bytes:
        _draw_cover_fill(c, art_bytes, W, H)
        _scrim(c, W, H)
    else:
        c.setFillColor(HexColor("#15171c"))
        c.rect(0, 0, W, H, stroke=0, fill=1)
        c.setStrokeColor(HexColor("#8a1c2b"))
        c.setLineWidth(4)
        c.rect(W * 0.05, H * 0.04, W * 0.90, H * 0.92, stroke=1, fill=0)

    # Title (wrapped, near the top), subtitle, author (near the foot).
    c.setFillColor(white)
    title = (meta.title or "").upper()
    tfont, tsize = "Times-Bold", 34
    lines = _wrap_centered(c, title, tfont, tsize, W, 4)
    y = H * 0.86
    c.setFont(tfont, tsize)
    for ln in lines:
        c.drawCentredString(W / 2, y, ln)
        y -= tsize + 8
    if meta.subtitle:
        c.setFont("Times-Italic", 16)
        c.drawCentredString(W / 2, y - 6, meta.subtitle)
    c.setFont("Times-Roman", 20)
    c.drawCentredString(W / 2, H * 0.07, meta.author_full())

    c.showPage()
    c.save()
    return buf.getvalue()


def back_cover_pdf(graph, meta, *, art_bytes: Optional[bytes] = None, ext: str = "png",
                   trim: Tuple[float, float] = (6.0, 9.0)) -> bytes:
    """Single-page back cover: blurb + author bio + imprint + barcode placeholder."""
    _require()
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import inch
    from reportlab.lib.colors import HexColor, white, Color

    W, H = trim[0] * inch, trim[1] * inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))

    if art_bytes:
        _draw_cover_fill(c, art_bytes, W, H)
        c.setFillColor(Color(0.05, 0.055, 0.07, alpha=0.82))
        c.rect(0, 0, W, H, stroke=0, fill=1)
    else:
        c.setFillColor(HexColor("#15171c"))
        c.rect(0, 0, W, H, stroke=0, fill=1)

    margin = 0.6 * inch
    blurb = _strip_tags(meta.back_cover_blurb or meta.description
                        or getattr(graph.bible, "logline", "")
                        or getattr(graph.bible, "premise", ""))
    c.setFillColor(white)
    y = H - margin - 6
    c.setFont("Times-Roman", 12.5)
    for ln in _wrap_centered(c, blurb, "Times-Roman", 12.5, W - margin, 9999)[:24]:
        c.drawString(margin, y, ln)
        y -= 19

    if meta.author_bio:
        y -= 26
        c.setFillColor(HexColor("#b9b4ab"))
        c.setFont("Times-Italic", 12)
        c.drawString(margin, y, "About the author")
        y -= 20
        c.setFillColor(white)
        c.setFont("Times-Roman", 11.5)
        for ln in _wrap_centered(c, _strip_tags(meta.author_bio), "Times-Roman", 11.5, W - margin, 9999)[:8]:
            c.drawString(margin, y, ln)
            y -= 17

    c.setFillColor(HexColor("#a39e96"))
    c.setFont("Times-Roman", 10)
    c.drawString(margin, margin, f"{meta.author_full()} · Independently published")
    # barcode placeholder
    c.setFillColor(white)
    c.rect(W - margin - 1.6 * inch, margin - 0.1 * inch, 1.6 * inch, 0.9 * inch, stroke=0, fill=1)
    c.setFillColor(HexColor("#15171c"))
    c.setFont("Helvetica", 8)
    c.drawCentredString(W - margin - 0.8 * inch, margin + 0.32 * inch, "ISBN / barcode")

    c.showPage()
    c.save()
    return buf.getvalue()


def full_pdf(graph, meta, *, art_bytes: Optional[bytes] = None, ext: str = "png",
             trim: Tuple[float, float] = (6.0, 9.0)) -> bytes:
    """Front cover + interior + back cover, concatenated into one PDF.

    Prefers pypdf for a clean page-merge; falls back to a single re-rendered
    document if pypdf isn't installed."""
    _require()
    front = front_cover_pdf(meta, art_bytes=art_bytes, ext=ext, trim=trim)
    interior = interior_pdf(graph, meta, trim=trim)
    back = back_cover_pdf(graph, meta, art_bytes=art_bytes, ext=ext, trim=trim)

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _full_pdf_single(graph, meta, art_bytes=art_bytes, ext=ext, trim=trim)

    writer = PdfWriter()
    for part in (front, interior, back):
        for page in PdfReader(io.BytesIO(part)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _full_pdf_single(graph, meta, *, art_bytes=None, ext="png",
                     trim: Tuple[float, float] = (6.0, 9.0)) -> bytes:
    """Single-document fallback (no pypdf): covers as Platypus flowables around
    the interior so the whole book is one PDF."""
    from reportlab.lib.pagesizes import inch
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    PageBreak, KeepInFrame)
    from xml.sax.saxutils import escape as esc

    W, H = trim[0] * inch, trim[1] * inch
    buf = io.BytesIO()

    def _cover_canvas(c, _doc):
        # drawn on the FIRST page; back cover drawn via onLaterPages is complex,
        # so the fallback only rasters the front cover and renders the back cover
        # text as flowables. (pypdf path is the high-fidelity one.)
        if art_bytes:
            _draw_cover_fill(c, art_bytes, W, H)
            _scrim(c, W, H)
        else:
            # No art: paint the same dark ground front_cover_pdf uses so the
            # white cover text is visible (white-on-white otherwise).
            c.setFillColor(HexColor("#15171c"))
            c.rect(0, 0, W, H, stroke=0, fill=1)
            c.setStrokeColor(HexColor("#8a1c2b"))
            c.setLineWidth(4)
            c.rect(W * 0.05, H * 0.04, W * 0.90, H * 0.92, stroke=1, fill=0)

    doc = SimpleDocTemplate(buf, pagesize=(W, H), leftMargin=0.75 * inch,
                            rightMargin=0.75 * inch, topMargin=0.9 * inch,
                            bottomMargin=0.9 * inch, title=meta.full_title())
    ss = getSampleStyleSheet()
    centered = ParagraphStyle("Cen", parent=ss["Normal"], alignment=TA_CENTER,
                              fontName="Times-Bold", fontSize=30, leading=36,
                              textColor="white")
    cover_sub = ParagraphStyle("CovSub", parent=ss["Normal"], alignment=TA_CENTER,
                               fontName="Times-Italic", fontSize=16, leading=22,
                               textColor="white")
    cover_author = ParagraphStyle("CovAuth", parent=ss["Normal"], alignment=TA_CENTER,
                                  fontName="Times-Roman", fontSize=18, leading=24,
                                  textColor="white")
    body = ParagraphStyle("Body", parent=ss["Normal"], fontName="Times-Roman",
                          fontSize=11.5, leading=16.5, firstLineIndent=16)
    chap = ParagraphStyle("Chap", parent=ss["Heading1"], fontName="Times-Bold",
                          fontSize=20, leading=26, alignment=TA_CENTER, spaceAfter=22)

    cover_block: List[Any] = [Paragraph(esc(meta.title.upper()), centered)]
    if meta.subtitle:
        cover_block += [Spacer(1, 8), Paragraph(esc(meta.subtitle), cover_sub)]
    cover_block += [Spacer(1, 14), Paragraph(esc(meta.author_full()), cover_author)]
    # KeepInFrame shrinks a long title/subtitle to fit page 1 — overflowing
    # would push white cover text onto the white second page.
    top_space = H * 0.60
    avail_h = max(1.0, (H - 1.8 * inch) - top_space)
    story: List[Any] = [Spacer(1, top_space),
                        KeepInFrame(W - 1.5 * inch, avail_h, cover_block,
                                    mode="shrink"),
                        PageBreak()]
    for n in sorted(graph.chapters):
        rec = graph.chapters[n]
        story.append(Paragraph(esc(rec.title), chap))
        for para in _paragraphs(rec.text):
            story.append(Paragraph(esc(para), body))
        story.append(PageBreak())
    blurb = _strip_tags(meta.back_cover_blurb or meta.description)
    story.append(Paragraph(esc(blurb), body))
    doc.build(story, onFirstPage=_cover_canvas)
    return buf.getvalue()


# Part names accepted by the export endpoint.
PDF_PARTS = ("interior", "front-cover", "back-cover", "full")


def build_pdf(part: str, graph, meta, *, art_bytes: Optional[bytes] = None,
              ext: str = "png", trim: Tuple[float, float] = (6.0, 9.0)) -> bytes:
    """Dispatch to the right PDF builder for ``part``."""
    part = (part or "full").lower()
    if part == "interior":
        return interior_pdf(graph, meta, trim=trim)
    if part == "front-cover":
        return front_cover_pdf(meta, art_bytes=art_bytes, ext=ext, trim=trim)
    if part == "back-cover":
        return back_cover_pdf(graph, meta, art_bytes=art_bytes, ext=ext, trim=trim)
    if part == "full":
        return full_pdf(graph, meta, art_bytes=art_bytes, ext=ext, trim=trim)
    raise ValueError(f"unknown PDF part {part!r}; choose from {PDF_PARTS}")
