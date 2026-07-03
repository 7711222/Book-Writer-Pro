"""Print interior (.docx) + print-cover spec/SVG — pure stdlib, no third-party deps.

This is the paperback companion to the EPUB path in ``kdp.py``. Where ``build_epub``
produces the Kindle ebook, this module produces the *print* artifacts an author needs
to publish a paperback through Amazon KDP / KDP Print:

  1. ``build_docx`` — a valid Office Open XML (.docx) manuscript interior. A .docx is
     just a ZIP of well-formed XML parts; we hand-write the minimum required parts
     ([Content_Types].xml, _rels/.rels, word/document.xml, word/styles.xml,
     word/_rels/document.xml.rels). Trim size drives the page geometry (sectPr pgSz in
     twips); each chapter starts on a fresh page; chapter prose is never dropped.
  2. ``print_spec`` — the cover *math*: page-count estimate, spine width (KDP's
     documented per-page constants), full bleed cover dimensions, and the recommended
     pixel canvas at 300 DPI. All JSON-able.
  3. ``build_print_cover_svg`` — a full-wrap cover SVG sized to that spec, with BACK
     (blurb + imprint), SPINE (rotated title/author) and FRONT zones plus faint
     bleed/trim guides. A starting point, not a finished cover.

Everything here is stdlib only: ``zipfile`` + manual, escaped XML. Numbers are clearly
labelled APPROXIMATE — KDP's exact printing math changes; confirm in the KDP UI.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as _xml_escape

# ---------------------------------------------------------------------------
# Geometry constants
# ---------------------------------------------------------------------------
TWIPS_PER_INCH = 1440           # Word measures in twentieths of a point (1in = 1440)
DEFAULT_DPI = 300               # KDP print covers are specified at 300 DPI
BLEED_IN = 0.125                # KDP bleed on every outside edge
MARGIN_IN = 1.0                 # interior page margins

# KDP-documented APPROXIMATE spine-width-per-page constants (inches/page), B&W:
#   white paper  ~ 0.002252 in/page
#   cream paper  ~ 0.0025   in/page
# These are Amazon's published figures and may change; confirm in the KDP cover
# calculator. Spine width = page_count * the per-page constant for the paper.
SPINE_IN_PER_PAGE_WHITE = 0.002252
SPINE_IN_PER_PAGE_CREAM = 0.0025

WORDS_PER_PAGE_6x9 = 300        # rough trade-paperback density at 6x9
MIN_PAGE_COUNT = 24             # KDP's paperback page-count floor

# KDP's minimum INSIDE (gutter) margin grows with page count (no-bleed interiors):
#   24-150 pages: 0.375"    151-300: 0.5"    301-500: 0.625"
#   501-700: 0.75"          700+:    0.875"
# We emit it as sectPr w:gutter (extra inside space) with mirrorMargins so it
# rides the binding edge on facing pages.
_GUTTER_TIERS = ((150, 0.375), (300, 0.5), (500, 0.625), (700, 0.75))
GUTTER_MAX_IN = 0.875


def _gutter_in(page_count: int) -> float:
    """KDP inside-margin (gutter) minimum for an estimated page count."""
    for max_pages, gutter in _GUTTER_TIERS:
        if page_count <= max_pages:
            return gutter
    return GUTTER_MAX_IN


# ---------------------------------------------------------------------------
# Shared text helpers (mirror kdp.py's paragraph splitting so prose is preserved)
# ---------------------------------------------------------------------------
# XML 1.0 forbids most C0 control characters; a pasted \x0b or \x0c (Word soft
# breaks) would make Word reject the .docx. Translate them away before escaping:
# \x0b/\x0c become spaces, the rest (except \t \n \r) are dropped, as are the
# ￾/￿ noncharacters (also outside XML 1.0's Char range).
_XML_CTRL_TRANSLATE = str.maketrans({
    **{c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)},
    0x0B: " ", 0x0C: " ", 0x7F: None, 0xFFFE: None, 0xFFFF: None,
})


def _esc(text: Any) -> str:
    return _xml_escape(
        str(text if text is not None else "").translate(_XML_CTRL_TRANSLATE)
    )


def _paragraphs(text: str) -> List[str]:
    """Split prose into paragraphs on blank lines; never lose content."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in text.split("\n\n")]
    paras = [b for b in blocks if b]
    if len(paras) <= 1:
        paras = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not paras:
        paras = [text.strip()] if text.strip() else []
    return [" ".join(p.split("\n")) for p in paras]


def _book_word_count(graph) -> int:
    total = 0
    for n in graph.chapters:
        rec = graph.chapters[n]
        wc = getattr(rec, "word_count", 0) or 0
        if not wc and getattr(rec, "text", ""):
            wc = len(rec.text.split())
        total += wc
    return total


def _estimate_page_count(graph) -> int:
    """Shared page-count estimate (words / trade density, KDP 24-page floor)."""
    words = _book_word_count(graph)
    return max(MIN_PAGE_COUNT,
               int(round(words / WORDS_PER_PAGE_6x9)) or MIN_PAGE_COUNT)


# ===========================================================================
# 1. DOCX builder
# ===========================================================================
def _p(text: str, *, style: Optional[str] = None, page_break_before: bool = False,
       align: Optional[str] = None) -> str:
    """One <w:p> paragraph. Optional paragraph style, page break, alignment."""
    ppr_bits: List[str] = []
    if style:
        ppr_bits.append(f'<w:pStyle w:val="{style}"/>')
    if page_break_before:
        ppr_bits.append('<w:pageBreakBefore/>')
    if align:
        ppr_bits.append(f'<w:jc w:val="{align}"/>')
    ppr = f'<w:pPr>{"".join(ppr_bits)}</w:pPr>' if ppr_bits else ""
    run = f'<w:r><w:t xml:space="preserve">{_esc(text)}</w:t></w:r>' if text else ""
    return f'<w:p>{ppr}{run}</w:p>'


def _CONTENT_TYPES() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/footer1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
        '<Override PartName="/word/settings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '</Types>'
    )


def _ROOT_RELS() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )


def _DOC_RELS() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" '
        'Target="footer1.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" '
        'Target="settings.xml"/>'
        '</Relationships>'
    )


def _FOOTER() -> str:
    """word/footer1.xml — a centered PAGE-number field on every page."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>1</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        '</w:p></w:ftr>'
    )


def _SETTINGS() -> str:
    """word/settings.xml — mirror margins so the gutter rides the inside edge."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:mirrorMargins/>'
        '</w:settings>'
    )


def _STYLES() -> str:
    """Minimal style sheet: Normal + Title + Subtitle + Heading1 (chapter title)."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:sz w:val="24"/>'
        '</w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:pPr><w:spacing w:line="360" w:lineRule="auto"/>'
        '<w:ind w:firstLine="360"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title">'
        '<w:name w:val="Title"/><w:pPr><w:jc w:val="center"/>'
        '<w:spacing w:before="2400" w:after="240"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="72"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Subtitle">'
        '<w:name w:val="Subtitle"/><w:pPr><w:jc w:val="center"/>'
        '<w:spacing w:after="240"/></w:pPr>'
        '<w:rPr><w:i/><w:sz w:val="40"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:jc w:val="center"/><w:spacing w:before="1200" w:after="480"/>'
        '<w:ind w:firstLine="0"/><w:outlineLvl w:val="0"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>'
        '</w:styles>'
    )


def build_docx(graph, meta, *, trim=(6.0, 9.0)) -> bytes:
    """Build a valid .docx print interior (returned as zip bytes). Stdlib only.

    Layout: title page (title/subtitle/author) -> copyright page -> each chapter on
    a new page (page break before the chapter heading), with a Heading1 chapter title
    and prose split into paragraphs on blank lines. Page size is the trim in twips via
    a section's pgSz; margins are 1 inch plus a KDP-sized inside gutter (mirrored
    via settings.xml so it rides the binding edge), and a centered page-number
    footer prints on every page. Chapter prose is never dropped.
    """
    trim_w, trim_h = float(trim[0]), float(trim[1])
    pg_w = int(round(trim_w * TWIPS_PER_INCH))
    pg_h = int(round(trim_h * TWIPS_PER_INCH))
    margin = int(round(MARGIN_IN * TWIPS_PER_INCH))
    # Inside (gutter) margin sized from the estimated page count, per KDP's table.
    gutter = int(round(_gutter_in(_estimate_page_count(graph)) * TWIPS_PER_INCH))

    sect_pr = (
        '<w:sectPr>'
        '<w:footerReference w:type="default" r:id="rId2"/>'
        f'<w:pgSz w:w="{pg_w}" w:h="{pg_h}"/>'
        f'<w:pgMar w:top="{margin}" w:right="{margin}" w:bottom="{margin}" '
        f'w:left="{margin}" w:header="720" w:footer="720" w:gutter="{gutter}"/>'
        '</w:sectPr>'
    )

    body: List[str] = []

    # --- title page ---------------------------------------------------------
    body.append(_p(meta.title, style="Title"))
    if getattr(meta, "subtitle", ""):
        body.append(_p(meta.subtitle, style="Subtitle"))
    body.append(_p(meta.author_full(), style="Subtitle"))
    for extra in meta.contributor_names():
        body.append(_p(extra, align="center"))

    # --- copyright page (new page) -----------------------------------------
    body.append(_p(f"{meta.full_title()}", page_break_before=True, align="center"))
    body.append(_p(f"Copyright © {meta.author_full()}", align="center"))
    body.append(_p("All rights reserved.", align="center"))
    rights = ("The author holds the necessary publishing rights."
              if getattr(meta, "publishing_rights", "owned") == "owned"
              else "This is a public domain work.")
    body.append(_p(rights, align="center"))
    if getattr(meta, "language", ""):
        body.append(_p(f"Language: {meta.language}", align="center"))

    # --- chapters (each starts on a new page) ------------------------------
    chapters = [graph.chapters[n] for n in sorted(graph.chapters)]
    for rec in chapters:
        # Heading1 carries pageBreakBefore so every chapter opens a fresh page.
        body.append(_p(rec.title, style="Heading1", page_break_before=True))
        for para in _paragraphs(rec.text):
            body.append(_p(para, style="Normal"))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:body>'
        + "".join(body)
        + sect_pr
        + '</w:body></w:document>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES())
        zf.writestr("_rels/.rels", _ROOT_RELS())
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS())
        zf.writestr("word/styles.xml", _STYLES())
        zf.writestr("word/footer1.xml", _FOOTER())
        zf.writestr("word/settings.xml", _SETTINGS())
    return buf.getvalue()


# ===========================================================================
# 2. Print spec (cover math)
# ===========================================================================
def print_spec(graph, meta, *, trim=(6.0, 9.0), paper="white") -> Dict[str, Any]:
    """Compute paperback cover geometry. Returns a JSON-able dict.

    page_count_estimate = max(MIN_PAGE_COUNT, round(words / WORDS_PER_PAGE_6x9))
    spine_width_in      = page_count * (white ~0.002252 | cream ~0.0025) in/page
    full_cover_width_in = 2*bleed + 2*trim_w + spine_width_in
    full_cover_height_in = trim_h + 2*bleed
    Plus recommended px at 300 DPI for each dimension, and a notes list.
    """
    trim_w, trim_h = float(trim[0]), float(trim[1])
    paper = (paper or "white").lower()
    per_page = SPINE_IN_PER_PAGE_CREAM if paper == "cream" else SPINE_IN_PER_PAGE_WHITE

    words = _book_word_count(graph)
    page_count = _estimate_page_count(graph)

    spine_width_in = round(page_count * per_page, 4)
    full_w = round(BLEED_IN * 2 + trim_w * 2 + spine_width_in, 4)
    full_h = round(trim_h + BLEED_IN * 2, 4)

    def px(inches: float) -> int:
        return int(round(inches * DEFAULT_DPI))

    notes = [
        f"Estimates only — confirm exact values in KDP's cover calculator.",
        f"Page count estimated at ~{WORDS_PER_PAGE_6x9} words/page for {trim_w}x{trim_h}; "
        f"KDP enforces a {MIN_PAGE_COUNT}-page minimum for paperbacks.",
        f"Spine width uses the APPROXIMATE B&W {paper}-paper constant "
        f"{per_page} in/page; spines under ~100 pages may be too thin "
        f"for spine text.",
        f"Full-wrap cover = back + spine + front + {BLEED_IN}\" bleed on every edge.",
        f"Canvas px computed at {DEFAULT_DPI} DPI (KDP's required cover resolution).",
    ]

    return {
        "trim_w": trim_w,
        "trim_h": trim_h,
        "paper": paper,
        "word_count": words,
        "page_count_estimate": page_count,
        "bleed": BLEED_IN,
        "spine_in_per_page": per_page,
        "spine_width_in": spine_width_in,
        "full_cover_width_in": full_w,
        "full_cover_height_in": full_h,
        "dpi": DEFAULT_DPI,
        "full_cover_width_px": px(full_w),
        "full_cover_height_px": px(full_h),
        "trim_w_px": px(trim_w),
        "trim_h_px": px(trim_h),
        "bleed_px": px(BLEED_IN),
        "spine_width_px": px(spine_width_in),
        "notes": notes,
    }


# ===========================================================================
# 3. Full-wrap print cover SVG
# ===========================================================================
def _wrap_text(text: str, max_chars: int) -> List[str]:
    """Greedy word-wrap into lines no longer than ~max_chars (for SVG <tspan>)."""
    words = (text or "").split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def _strip_html(text: str) -> str:
    """Crude tag strip so an HTML blurb renders as plain back-cover text."""
    out, depth = [], 0
    for ch in text or "":
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    # collapse <br>-driven whitespace
    return " ".join("".join(out).split())


def build_print_cover_svg(graph, meta, spec, *, front_cover_svg=None) -> str:
    """A full-wrap print cover SVG sized to ``spec`` (300 DPI px).

    Three zones laid out left-to-right: BACK (blurb + imprint), SPINE (rotated
    title + author), FRONT (the supplied front cover SVG embedded, else a simple
    title front). Faint bleed/trim guide rectangles are drawn as a layout aid.
    A starting point for a print cover — replace/refine before publishing.
    """
    W = int(spec["full_cover_width_px"])
    H = int(spec["full_cover_height_px"])
    bleed = int(spec["bleed_px"])
    trim_w = int(spec["trim_w_px"])
    spine_w = int(spec["spine_width_px"])

    # Zone x-origins (within the bleed canvas).
    back_x = 0
    spine_x = bleed + trim_w
    front_x = bleed + trim_w + spine_w

    title = _esc(meta.title)
    author = _esc(meta.author_full())

    parts: List[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    )
    # background
    parts.append(f'<rect width="{W}" height="{H}" fill="#15171c"/>')

    # ---- BACK zone: blurb + imprint ---------------------------------------
    blurb = _strip_html(getattr(meta, "description", "") or "")
    if not blurb:
        b = graph.bible
        blurb = _strip_html(getattr(b, "logline", "") or getattr(b, "premise", ""))
    back_inner_x = back_x + bleed + 60
    blurb_lines = _wrap_text(blurb, 52)[:22]
    parts.append(
        f'<text x="{back_inner_x}" y="{bleed + 220}" font-family="Georgia, serif" '
        f'font-size="34" fill="#e6e2da">'
    )
    y = 0
    for i, ln in enumerate(blurb_lines):
        dy = 0 if i == 0 else 52
        parts.append(f'<tspan x="{back_inner_x}" dy="{dy}">{_esc(ln)}</tspan>')
    parts.append('</text>')
    # imprint at the foot of the back cover
    parts.append(
        f'<text x="{back_x + bleed + trim_w // 2}" y="{H - bleed - 80}" '
        f'font-family="Georgia, serif" font-size="26" fill="#8a8782" '
        f'text-anchor="middle">{author} · Independently published</text>'
    )

    # ---- SPINE zone: rotated title + author -------------------------------
    if spine_w >= 36:  # only draw spine text if there's room (~0.12in / 36px)
        spine_cx = spine_x + spine_w // 2
        spine_cy = H // 2
        spine_font = max(18, min(spine_w - 12, 40))
        parts.append(
            f'<g transform="translate({spine_cx},{spine_cy}) rotate(90)">'
            f'<text x="0" y="-6" font-family="Georgia, serif" font-size="{spine_font}" '
            f'font-weight="bold" fill="#f4f1ea" text-anchor="middle">{title}</text>'
            f'<text x="0" y="{spine_font + 4}" font-family="Georgia, serif" '
            f'font-size="{max(14, spine_font - 8)}" fill="#c9c4bd" '
            f'text-anchor="middle">{author}</text>'
            f'</g>'
        )

    # ---- FRONT zone -------------------------------------------------------
    front_w = trim_w + bleed  # front spans to the right outer bleed edge
    if front_cover_svg:
        parts.append(
            f'<svg x="{front_x}" y="0" width="{front_w}" height="{H}" '
            f'viewBox="0 0 1600 2560" preserveAspectRatio="xMidYMid slice">'
        )
        parts.append(_embed_inner_svg(front_cover_svg))
        parts.append('</svg>')
    else:
        fcx = front_x + front_w // 2
        parts.append(
            f'<text x="{fcx}" y="{H // 2 - 40}" font-family="Georgia, serif" '
            f'font-size="96" font-weight="bold" fill="#f4f1ea" '
            f'text-anchor="middle">{title}</text>'
        )
        if getattr(meta, "subtitle", ""):
            parts.append(
                f'<text x="{fcx}" y="{H // 2 + 60}" font-family="Georgia, serif" '
                f'font-size="48" fill="#c9c4bd" text-anchor="middle">'
                f'{_esc(meta.subtitle)}</text>'
            )
        parts.append(
            f'<text x="{fcx}" y="{H - bleed - 160}" font-family="Georgia, serif" '
            f'font-size="56" fill="#d8d3ca" text-anchor="middle">{author}</text>'
        )

    # ---- guides: bleed (outer), trim (inner), spine fold lines ------------
    parts.append(
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" fill="none" '
        f'stroke="#e23" stroke-width="1" stroke-dasharray="12 8" opacity="0.45"/>'
    )
    parts.append(
        f'<rect x="{bleed}" y="{bleed}" width="{W - 2 * bleed}" height="{H - 2 * bleed}" '
        f'fill="none" stroke="#3cf" stroke-width="1" stroke-dasharray="6 6" opacity="0.35"/>'
    )
    for fx in (spine_x, spine_x + spine_w):
        parts.append(
            f'<line x1="{fx}" y1="{bleed}" x2="{fx}" y2="{H - bleed}" '
            f'stroke="#9c9" stroke-width="1" stroke-dasharray="4 6" opacity="0.4"/>'
        )

    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def _embed_inner_svg(svg: str) -> str:
    """Strip XML/doctype prolog and the outer <svg ...> wrapper from a front cover
    SVG so its inner content can be nested inside the wrap cover's <svg> viewport."""
    s = svg.strip()
    # drop <?xml ...?> and <!DOCTYPE ...>
    while s.startswith("<?") or s.startswith("<!"):
        end = s.find(">")
        if end == -1:
            break
        s = s[end + 1:].lstrip()
    low = s.lower()
    open_start = low.find("<svg")
    if open_start != -1:
        open_end = s.find(">", open_start)
        close = low.rfind("</svg>")
        if open_end != -1 and close != -1 and close > open_end:
            return s[open_end + 1:close].strip()
    return s
