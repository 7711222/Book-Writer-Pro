"""Amazon KDP packaging — turn a finished StoryGraph into an upload-ready kit.

This module is the bridge between the generated manuscript (the continuity graph
+ chapters) and Amazon Kindle Direct Publishing's "page 1" book-details form. It
does three things:

  1. Generate the *marketing* fields the author shouldn't have to write by hand
     (description / keywords / categories / reading age / series suggestion) via
     the same LLM protocol the rest of the pipeline uses — then ENFORCE KDP's
     hard limits in Python so a chatty model can't produce an invalid listing.
  2. Build a valid, stdlib-only EPUB 3 from the chapters (no third-party deps:
     just ``zipfile`` + manual, escaped XML). Chapter prose is never dropped.
  3. Write a copy-paste kit (metadata.json, manuscript.epub, cover.svg — plus a
     raster cover.<ext> when AI art exists, since KDP rejects SVG uploads —
     kdp-listing.txt, CHECKLIST.md) the author pastes field-by-field into KDP.

User-set identity fields (author name, publishing rights, explicit-content flag)
are NOT generated — they are captured from the caller and carried verbatim.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as _xml_escape, quoteattr as _xml_quoteattr


# ---------------------------------------------------------------------------
# KDP hard limits (Amazon-enforced; we mirror them so a model can't break them)
# ---------------------------------------------------------------------------
MAX_DESCRIPTION_CHARS = 4000
MAX_KEYWORDS = 7
MAX_KEYWORD_CHARS = 50
MAX_CATEGORIES = 3
MAX_CONTRIBUTORS = 9
STORYTELLER_KEYWORD = "StorytellerUK2026"

# Marketing-generator caps (our own product limits, enforced in Python)
MAX_BLURB_VARIANTS = 3
MAX_APLUS_MODULES = 3
MAX_TAGLINES = 5
MAX_BLURB_CHARS = 4000


# ---------------------------------------------------------------------------
# 1. Schema for the LLM-generated fields ONLY
# ---------------------------------------------------------------------------
KDP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "subtitle", "description", "keywords", "categories",
        "reading_age_min", "reading_age_max", "series_suggestion",
        "back_cover_blurb", "author_bio", "edition", "series_part",
    ],
    "properties": {
        "subtitle": {
            "type": "string",
            "description": "Optional marketing subtitle (no colon); empty string if none.",
        },
        "back_cover_blurb": {
            "type": "string",
            "description": (
                "A shorter, punchy BACK-COVER blurb (plain text, ~60-110 words, no "
                "HTML) — the hook a browser reads on the physical back cover."
            ),
        },
        "author_bio": {
            "type": "string",
            "description": (
                "A short third-person author bio (1-3 sentences) for the back cover "
                "/ Author Central. Invent a tasteful, generic bio if unknown."
            ),
        },
        "edition": {
            "type": "string",
            "description": "Edition number, normally \"1\" for a first edition.",
        },
        "series_part": {
            "type": "string",
            "description": "Series part number (e.g. \"1\") if part of a series; else empty.",
        },
        "description": {
            "type": "string",
            "description": (
                "Punchy back-cover marketing copy under 4000 characters. Light "
                "HTML allowed (<b>,<i>,<br>,<ul>,<li>,<h4>). Hook, stakes, "
                "promise — not a synopsis dump."
            ),
        },
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Up to 7 short search phrases customers actually type. Each <=50 "
                "chars. No title, no author name, no 'bestseller/free/on sale'."
            ),
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 3 Kindle/BISAC-style fiction categories for this genre.",
        },
        "reading_age_min": {
            "type": "string",
            "description": "Minimum reading age (children's/YA only); empty for adult.",
        },
        "reading_age_max": {
            "type": "string",
            "description": "Maximum reading age (children's/YA only); empty for adult.",
        },
        "series_suggestion": {
            "type": "string",
            "description": "Suggested series name if this reads like book 1 of a series; else empty.",
        },
    },
}


# Marketing copy the LLM writes (blurb variants / A+ modules / bio / taglines).
MARKETING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["blurb_variants", "a_plus_modules", "author_bio", "taglines"],
    "properties": {
        "blurb_variants": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "2-3 alternative back-cover / product-description blurbs, each a "
                "punchy hook (NOT a synopsis), under 4000 characters, no spoilers."
            ),
        },
        "a_plus_modules": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["headline", "body"],
                "properties": {
                    "headline": {"type": "string",
                                 "description": "Short A+ Content module headline."},
                    "body": {"type": "string",
                             "description": "1-3 sentence A+ module body copy."},
                },
            },
            "description": "Up to 3 Amazon A+ Content modules (headline + body).",
        },
        "author_bio": {
            "type": "string",
            "description": "A short third-person author bio for the Author Central page.",
        },
        "taglines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 one-line taglines for ads / social posts.",
        },
    },
}


# ---------------------------------------------------------------------------
# 2. The full page-1 metadata dataclass
# ---------------------------------------------------------------------------
@dataclass
class KdpMetadata:
    """Every Amazon KDP 'page 1' book-details field, in one place.

    Combines user-set identity fields (title/author/rights/...) with the
    LLM-generated marketing fields (description/keywords/categories/...). All
    KDP limits are enforced before/at construction by ``generate_kdp_metadata``;
    ``to_dict``/``from_dict`` round-trip the whole thing for metadata.json.
    """
    title: str
    author_first: str
    author_last: str
    language: str = "English"
    subtitle: str = ""
    series: str = ""
    series_part: str = ""
    edition: str = ""
    contributors: List[Dict[str, str]] = field(default_factory=list)  # [{first,last}]
    description: str = ""
    publishing_rights: str = "owned"     # 'owned' | 'public_domain'
    sexually_explicit: bool = False
    reading_age_min: str = ""
    reading_age_max: str = ""
    primary_marketplace: str = "Amazon.com"
    categories: List[str] = field(default_factory=list)   # <= 3
    keywords: List[str] = field(default_factory=list)      # <= 7
    author_bio: str = ""             # back cover / Author Central
    back_cover_blurb: str = ""       # short plain-text back-cover hook

    # ---- convenience -------------------------------------------------------
    def author_full(self) -> str:
        return " ".join(p for p in (self.author_first, self.author_last) if p).strip()

    def contributor_names(self) -> List[str]:
        out = []
        for c in self.contributors:
            name = " ".join(p for p in (c.get("first", ""), c.get("last", "")) if p).strip()
            if name:
                out.append(name)
        return out

    def full_title(self) -> str:
        """Title as KDP displays it: 'Title: Subtitle' (auto-inserted colon)."""
        if self.subtitle:
            return f"{self.title}: {self.subtitle}"
        return self.title

    def all_creators(self) -> List[str]:
        """Primary author followed by contributors, in entered order."""
        return [self.author_full()] + self.contributor_names()

    # ---- serialization -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "KdpMetadata":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# 3. Generate + enforce the metadata
# ---------------------------------------------------------------------------
def _split_title(raw: str):
    """Split a 'Title : Subtitle' / 'Title: Subtitle' string into parts.

    Returns (title, subtitle). If there is no separator, subtitle is "".
    Prefers the spaced ' : ' form, then a plain ':'.
    """
    raw = (raw or "").strip()
    for sep in (" : ", ": ", " :", ":"):
        if sep in raw:
            head, _, tail = raw.partition(sep)
            return head.strip(), tail.strip()
    return raw, ""


def _strip_tags(text: str) -> str:
    """Crude HTML-tag strip so an HTML description renders as plain back-cover text."""
    out, depth = [], 0
    for ch in text or "":
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _truncate_words(text: str, limit: int) -> str:
    """Truncate to <= limit chars on a word boundary where possible."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:        # only honor the boundary if it isn't too early
        cut = cut[:sp]
    return cut.rstrip()


def _clean_keywords(raw: List[str], *, forbidden: List[str]) -> List[str]:
    """Strip, dedupe, length-cap, and drop title/author-leaking keywords."""
    forbid = [f.lower() for f in forbidden if f and f.strip()]
    out: List[str] = []
    seen = set()
    for kw in raw or []:
        if not isinstance(kw, str):
            continue
        k = kw.strip()
        if not k:
            continue
        k = k[:MAX_KEYWORD_CHARS].strip()
        low = k.lower()
        if low in seen:
            continue
        if any(f in low for f in forbid):
            continue
        seen.add(low)
        out.append(k)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def _is_adult(audience: str) -> bool:
    a = (audience or "").lower()
    child_markers = ("child", "kid", "middle grade", "middle-grade", "young adult", "ya", "teen")
    return not any(m in a for m in child_markers)


def generate_kdp_metadata(
    llm, settings, ledger, graph, *,
    author_first: str,
    author_last: str,
    language: str = "English",
    subtitle: Optional[str] = None,
    series: str = "",
    edition: str = "",
    contributors: Optional[List[Dict[str, str]]] = None,
) -> KdpMetadata:
    """Generate marketing fields via the LLM and assemble a validated KdpMetadata.

    Identity fields (author/rights/series/edition/contributors) come from the
    caller. The model only supplies description/keywords/categories/reading age/
    series suggestion; every KDP limit is then re-enforced here in Python — we do
    not trust the model to respect them.
    """
    b = graph.bible

    # Title / subtitle: split bible.title if it carries a subtitle and the caller
    # didn't pass one explicitly.
    title, split_sub = _split_title(b.title)
    if subtitle is None:
        subtitle = split_sub
    subtitle = (subtitle or "").strip()

    chapter_titles = [f"{p.number}. {p.title}" for p in b.outline]

    system = (
        "You are a senior Amazon KDP listing copywriter and metadata strategist. "
        "Write a punchy, back-cover marketing DESCRIPTION (a hook + stakes + "
        "promise, NOT a plot synopsis) under 4000 characters; light HTML is "
        "allowed (<b>,<i>,<br>,<ul>,<li>,<h4>). Choose up to 7 KEYWORDS that are "
        "short search phrases real customers type — never repeat the book title "
        "or author name, never use other books' titles, and never use "
        "'bestseller', 'free', 'on sale', or subjective/inaccurate claims. Pick "
        "up to 3 Kindle-store CATEGORIES appropriate to the genre. Provide a "
        "reading age ONLY for children's or YA books (leave blank for adult). "
        "Suggest a series name only if this clearly reads like book one of a "
        "series. Also write a shorter plain-text BACK-COVER blurb (~60-110 words), "
        "a short third-person AUTHOR BIO, an EDITION number (\"1\" for a first "
        "edition), and a SERIES PART number if it's part of a series. Return "
        "strictly the requested JSON schema."
    )
    user = (
        f"BOOK TITLE: {title}\n"
        f"GENRE: {b.genre}\n"
        f"TONE: {b.tone}\n"
        f"AUDIENCE: {b.audience}\n"
        f"THEMES: {', '.join(b.themes)}\n"
        f"LOGLINE: {b.logline}\n\n"
        f"PREMISE:\n{b.premise}\n\n"
        f"CHAPTER TITLES:\n" + "\n".join(chapter_titles) + "\n\n"
        "Write the listing fields for this book."
    )

    data = llm.complete_json(
        stage="kdp",
        model=settings.profile.write,
        system=system,
        user=user,
        schema=KDP_SCHEMA,
        max_tokens=4000,
        ledger=ledger,
    )

    # Model may suggest a subtitle if we don't have one.
    if not subtitle:
        subtitle = (data.get("subtitle") or "").strip()

    # --- ENFORCE limits (never trust the model) -------------------------
    description = _truncate_words(str(data.get("description") or "").strip(),
                                  MAX_DESCRIPTION_CHARS)

    forbidden = [title, subtitle, author_first, author_last,
                 f"{author_first} {author_last}".strip()]
    keywords = _clean_keywords(data.get("keywords") or [], forbidden=forbidden)

    categories = []
    seen_cat = set()
    for c in (data.get("categories") or []):
        if not isinstance(c, str):
            continue
        cc = c.strip()
        if cc and cc.lower() not in seen_cat:
            seen_cat.add(cc.lower())
            categories.append(cc)
        if len(categories) >= MAX_CATEGORIES:
            break

    # Reading age: blank for adult audiences regardless of what the model said.
    if _is_adult(b.audience):
        reading_age_min = ""
        reading_age_max = ""
    else:
        reading_age_min = str(data.get("reading_age_min") or "").strip()
        reading_age_max = str(data.get("reading_age_max") or "").strip()

    # Series: caller wins; otherwise accept the model's suggestion.
    if not series:
        series = (data.get("series_suggestion") or "").strip()

    contribs = []
    for c in (contributors or [])[:MAX_CONTRIBUTORS]:
        contribs.append({"first": str(c.get("first", "")).strip(),
                         "last": str(c.get("last", "")).strip()})

    # Back-cover blurb (plain text) — fall back to a trimmed description.
    back_cover_blurb = _strip_tags(str(data.get("back_cover_blurb") or "").strip())
    if not back_cover_blurb:
        back_cover_blurb = _truncate_words(_strip_tags(description), 700)
    author_bio = str(data.get("author_bio") or "").strip()

    # Edition / series part: caller wins; else accept the model's suggestion.
    if not edition:
        edition = (str(data.get("edition") or "")).strip()
    series_part = (str(data.get("series_part") or "")).strip() if series else ""

    return KdpMetadata(
        title=title,
        subtitle=subtitle,
        author_first=author_first,
        author_last=author_last,
        language=language or "English",
        series=series,
        series_part=series_part,
        edition=edition,
        contributors=contribs,
        description=description,
        categories=categories,
        keywords=keywords,
        reading_age_min=reading_age_min,
        reading_age_max=reading_age_max,
        author_bio=author_bio,
        back_cover_blurb=back_cover_blurb,
    )


# ---------------------------------------------------------------------------
# 3b. Marketing copy generator (blurb variants / A+ modules / bio / taglines)
# ---------------------------------------------------------------------------
def generate_marketing(llm, settings, ledger, graph, meta: "KdpMetadata") -> Dict[str, Any]:
    """Generate ad/listing marketing copy via the LLM and enforce our caps.

    Produces alternative blurbs, Amazon A+ Content modules, an author bio, and
    short taglines — the copy an author needs beyond the single KDP description.
    Caps (<=3 blurbs, <=3 A+ modules, <=5 taglines, each blurb <=4000 chars) are
    re-enforced in Python; we never trust the model to respect them.
    """
    b = graph.bible
    system = (
        "You are an elite book-marketing copywriter. Write punchy, HONEST,"
        " spoiler-free copy that sells the EXPERIENCE of the book — hooks, stakes,"
        " emotional promise — never a plot synopsis and never hype words like"
        " 'bestseller' or 'award-winning'. Give 2-3 alternative blurbs in"
        " different angles, up to 3 Amazon A+ Content modules (a short headline"
        " plus 1-3 sentences each), a short third-person author bio, and up to 5"
        " one-line taglines for ads. Return strictly the requested JSON schema."
    )
    user = (
        f"BOOK TITLE: {meta.full_title()}\n"
        f"AUTHOR: {meta.author_full()}\n"
        f"GENRE: {b.genre}\n"
        f"TONE: {b.tone}\n"
        f"THEMES: {', '.join(b.themes)}\n"
        f"LOGLINE: {b.logline}\n\n"
        f"PREMISE:\n{b.premise}\n\n"
        f"EXISTING DESCRIPTION:\n{meta.description}\n\n"
        "Write the marketing copy for this book."
    )

    data = llm.complete_json(
        stage="kdp",
        model=settings.profile.write,
        system=system,
        user=user,
        schema=MARKETING_SCHEMA,
        max_tokens=3000,
        ledger=ledger,
    )

    # --- ENFORCE caps (never trust the model) ------------------------------
    blurbs: List[str] = []
    for v in (data.get("blurb_variants") or []):
        if isinstance(v, str) and v.strip():
            blurbs.append(_truncate_words(v.strip(), MAX_BLURB_CHARS))
        if len(blurbs) >= MAX_BLURB_VARIANTS:
            break

    modules: List[Dict[str, str]] = []
    for m in (data.get("a_plus_modules") or []):
        if not isinstance(m, dict):
            continue
        headline = str(m.get("headline", "") or "").strip()
        body = str(m.get("body", "") or "").strip()
        if headline or body:
            modules.append({"headline": headline, "body": body})
        if len(modules) >= MAX_APLUS_MODULES:
            break

    taglines: List[str] = []
    seen = set()
    for t in (data.get("taglines") or []):
        if not isinstance(t, str):
            continue
        tt = t.strip()
        if tt and tt.lower() not in seen:
            seen.add(tt.lower())
            taglines.append(tt)
        if len(taglines) >= MAX_TAGLINES:
            break

    author_bio = str(data.get("author_bio", "") or "").strip()

    return {
        "blurb_variants": blurbs,
        "a_plus_modules": modules,
        "author_bio": author_bio,
        "taglines": taglines,
    }


# ---------------------------------------------------------------------------
# 4. EPUB 3 builder (pure stdlib)
# ---------------------------------------------------------------------------
# XML 1.0 forbids most C0 control characters; a pasted \x0b or \x0c (Word soft
# breaks) would make every consumer reject the file. Translate them away before
# escaping: \x0b/\x0c become spaces, the rest (except \t \n \r) are dropped,
# as are the ￾/￿ noncharacters (also outside XML 1.0's Char range).
_XML_CTRL_TRANSLATE = str.maketrans({
    **{c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)},
    0x0B: " ", 0x0C: " ", 0x7F: None, 0xFFFE: None, 0xFFFF: None,
})


def _esc(text: str) -> str:
    """XML-escape text for element content / attribute-safe usage.

    Also strips XML-1.0-illegal control characters so pasted/imported prose
    (e.g. Word's \\x0b soft line breaks) can't corrupt the EPUB/DOCX.
    """
    return _xml_escape(
        str(text if text is not None else "").translate(_XML_CTRL_TRANSLATE)
    )


def _paragraphs(text: str) -> List[str]:
    """Split prose into paragraphs on blank lines; never lose content.

    Blocks separated by one or more blank lines become <p>; within a block,
    single newlines are collapsed to spaces. Falls back to one paragraph per
    non-empty line if there are no blank-line separators.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in text.split("\n\n")]
    paras = [b for b in blocks if b]
    if len(paras) <= 1:
        paras = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not paras:
        paras = [text.strip()] if text.strip() else []
    return [" ".join(p.split("\n")) for p in paras]


def _book_id(graph, meta: "KdpMetadata") -> str:
    """Stable, unique identifier derived from chapter fingerprints + title."""
    h = hashlib.sha256()
    h.update((meta.full_title() + "|" + meta.author_full()).encode("utf-8"))
    for n in sorted(graph.chapters):
        rec = graph.chapters[n]
        fp = getattr(rec, "fingerprint", "") or ""
        if not fp:
            fp = hashlib.sha256((rec.text or "").encode("utf-8")).hexdigest()[:16]
        h.update(f"|{n}:{fp}".encode("utf-8"))
    digest = h.hexdigest()
    # urn:uuid-ish formatting from the hash (deterministic, not a real v4 UUID)
    d = digest
    return f"urn:uuid:{d[0:8]}-{d[8:12]}-{d[12:16]}-{d[16:20]}-{d[20:32]}"


def _fallback_cover_svg(meta: "KdpMetadata") -> str:
    """A clean title-style fallback cover (1600x2560, KDP-friendly ratio)."""
    title = _esc(meta.title)
    subtitle = _esc(meta.subtitle)
    author = _esc(meta.author_full())
    sub_block = (
        f'<text x="800" y="1180" font-family="Georgia, serif" font-size="64" '
        f'fill="#c9c4bd" text-anchor="middle">{subtitle}</text>'
        if meta.subtitle else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="2560" '
        'viewBox="0 0 1600 2560" preserveAspectRatio="xMidYMid meet">\n'
        '  <rect width="1600" height="2560" fill="#15171c"/>\n'
        '  <rect x="80" y="80" width="1440" height="2400" fill="none" '
        'stroke="#8a1c2b" stroke-width="6"/>\n'
        f'  <text x="800" y="1040" font-family="Georgia, serif" font-size="120" '
        f'font-weight="bold" fill="#f4f1ea" text-anchor="middle">{title}</text>\n'
        f'  {sub_block}\n'
        f'  <text x="800" y="2360" font-family="Georgia, serif" font-size="72" '
        f'fill="#d8d3ca" text-anchor="middle">{author}</text>\n'
        '</svg>\n'
    )


def _cover_wrap(text: str, max_chars: int, max_lines: int) -> List[str]:
    """Greedy word-wrap into <= max_lines lines of ~max_chars chars (for SVG)."""
    words = (text or "").split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
        else:
            cur = f"{cur} {w}".strip()
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def _art_data_uri(art_bytes: bytes, ext: str) -> str:
    media = _IMG_MEDIA.get((ext or "png").lower().lstrip("."), "image/png")
    return f"data:{media};base64,{base64.b64encode(art_bytes).decode('ascii')}"


def compose_cover_svg(meta: "KdpMetadata", art_bytes: bytes, ext: str = "png") -> str:
    """Front cover = AI artwork (embedded raster) + crisp title/author typography.

    1600x2560 (KDP-friendly ~1:1.6). A dark top+bottom scrim keeps the overlaid
    text legible over any artwork. This SVG is what flows into the EPUB / print
    cover; the PDF cover redraws the same composition natively (see pdf.py)."""
    uri = _art_data_uri(art_bytes, ext)
    title_lines = _cover_wrap(meta.title.upper(), 16, 3)
    title_fs = 150 if len(title_lines) <= 2 else 120
    t_spans = "".join(
        f'<tspan x="800" dy="{0 if i == 0 else title_fs + 12}">{_esc(ln)}</tspan>'
        for i, ln in enumerate(title_lines)
    )
    sub = (
        f'<text x="800" y="{360 + len(title_lines) * (title_fs + 12)}" '
        f'font-family="Georgia, serif" font-size="56" fill="#f0ece4" '
        f'text-anchor="middle" opacity="0.92">{_esc(meta.subtitle)}</text>'
        if meta.subtitle else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="1600" height="2560" viewBox="0 0 1600 2560" '
        'preserveAspectRatio="xMidYMid meet">\n'
        '  <defs>\n'
        '    <linearGradient id="scrim" x1="0" y1="0" x2="0" y2="1">\n'
        '      <stop offset="0" stop-color="#000" stop-opacity="0.62"/>\n'
        '      <stop offset="0.28" stop-color="#000" stop-opacity="0.05"/>\n'
        '      <stop offset="0.72" stop-color="#000" stop-opacity="0.05"/>\n'
        '      <stop offset="1" stop-color="#000" stop-opacity="0.72"/>\n'
        '    </linearGradient>\n'
        '  </defs>\n'
        f'  <image x="0" y="0" width="1600" height="2560" xlink:href="{uri}" '
        'preserveAspectRatio="xMidYMid slice"/>\n'
        '  <rect width="1600" height="2560" fill="url(#scrim)"/>\n'
        f'  <text x="800" y="300" font-family="Georgia, serif" font-size="{title_fs}" '
        f'font-weight="bold" fill="#ffffff" text-anchor="middle" '
        f'style="paint-order:stroke;stroke:#000;stroke-width:6px;stroke-opacity:0.35">{t_spans}</text>\n'
        f'  {sub}\n'
        f'  <text x="800" y="2400" font-family="Georgia, serif" font-size="68" '
        f'fill="#ffffff" text-anchor="middle" '
        f'style="paint-order:stroke;stroke:#000;stroke-width:5px;stroke-opacity:0.35">'
        f'{_esc(meta.author_full())}</text>\n'
        '</svg>\n'
    )


def back_cover_svg(graph, meta: "KdpMetadata", *, art_bytes: Optional[bytes] = None,
                   ext: str = "png") -> str:
    """A 1600x2560 back cover: blurb + author bio + imprint + barcode placeholder.

    Uses the front-cover artwork as a darkened background when supplied (so the
    front and back match), else a solid dark ground."""
    if art_bytes:
        bg = (
            f'  <image x="0" y="0" width="1600" height="2560" '
            f'xlink:href="{_art_data_uri(art_bytes, ext)}" '
            'preserveAspectRatio="xMidYMid slice"/>\n'
            '  <rect width="1600" height="2560" fill="#0c0e12" opacity="0.82"/>\n'
        )
    else:
        bg = '  <rect width="1600" height="2560" fill="#15171c"/>\n'

    blurb = _strip_tags(meta.back_cover_blurb or meta.description
                        or getattr(graph.bible, "logline", "")
                        or getattr(graph.bible, "premise", ""))
    blurb_lines = _cover_wrap(blurb, 46, 18)
    blurb_spans = "".join(
        f'<tspan x="160" dy="{0 if i == 0 else 60}">{_esc(ln)}</tspan>'
        for i, ln in enumerate(blurb_lines)
    )

    bio_block = ""
    if meta.author_bio:
        bio_lines = _cover_wrap(_strip_tags(meta.author_bio), 50, 8)
        bio_spans = "".join(
            f'<tspan x="160" dy="{0 if i == 0 else 46}">{_esc(ln)}</tspan>'
            for i, ln in enumerate(bio_lines)
        )
        bio_y = 2560 - 620
        bio_block = (
            f'  <text x="160" y="{bio_y - 56}" font-family="Georgia, serif" '
            f'font-size="34" font-style="italic" fill="#b9b4ab">About the author</text>\n'
            f'  <text x="160" y="{bio_y}" font-family="Georgia, serif" font-size="36" '
            f'fill="#e6e2da">{bio_spans}</text>\n'
        )

    creators = _esc(meta.author_full())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="1600" height="2560" viewBox="0 0 1600 2560">\n'
        + bg
        + f'  <text x="160" y="280" font-family="Georgia, serif" font-size="44" '
        f'fill="#e6e2da">{blurb_spans}</text>\n'
        + bio_block
        + f'  <text x="160" y="2400" font-family="Georgia, serif" font-size="30" '
        f'fill="#a39e96">{creators} · Independently published</text>\n'
        # ISBN / barcode placeholder (KDP prints the real one)
        '  <rect x="1120" y="2300" width="320" height="170" rx="8" fill="#ffffff"/>\n'
        '  <text x="1280" y="2400" font-family="Helvetica, Arial, sans-serif" '
        'font-size="26" fill="#15171c" text-anchor="middle">ISBN / barcode</text>\n'
        '</svg>\n'
    )


# KDP-supported language names -> BCP-47 codes for dc:language / xml:lang.
# Kindle uses the OPF language for dictionary/typography/store classification,
# so a non-English book must not ship tagged as 'en'.
_LANGUAGE_CODES = {
    "english": "en", "spanish": "es", "german": "de", "french": "fr",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "japanese": "ja",
    "hindi": "hi", "afrikaans": "af", "arabic": "ar", "basque": "eu",
    "catalan": "ca", "chinese": "zh", "danish": "da", "finnish": "fi",
    "galician": "gl", "gujarati": "gu", "icelandic": "is", "irish": "ga",
    "malayalam": "ml", "marathi": "mr", "norwegian": "no", "polish": "pl",
    "romanian": "ro", "swedish": "sv", "tamil": "ta", "welsh": "cy",
}


def _lang_code(language: str) -> str:
    """Map a KDP language NAME (e.g. 'Spanish') to a BCP-47 code ('es').

    A value that already looks like a bare 2-3 letter code is passed through
    lowercased; anything unrecognized falls back to 'en'.
    """
    name = (language or "English").strip().lower()
    if name in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[name]
    if 2 <= len(name) <= 3 and name.isalpha():
        return name
    return "en"


def _xhtml(title: str, body: str, lang: str = "en") -> str:
    lang = _esc(lang or "en")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
        f'<head><meta charset="utf-8"/><title>{_esc(title)}</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f'<body>\n{body}\n</body>\n</html>\n'
    )


_CSS = (
    "body { font-family: Georgia, serif; line-height: 1.5; margin: 1em; }\n"
    "h1 { text-align: center; font-size: 2em; margin: 2em 0 0.3em; }\n"
    "h2 { font-size: 1.5em; margin: 1.5em 0 1em; }\n"
    "p { text-indent: 1.4em; margin: 0 0 0.2em; }\n"
    "p.first { text-indent: 0; }\n"
    ".title-author { text-align: center; font-size: 1.2em; margin-top: 1em; }\n"
    ".cover img, .cover svg { width: 100%; height: auto; }\n"
)


_IMG_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "webp": "image/webp", "gif": "image/gif"}


def build_epub(graph, meta: "KdpMetadata", *, cover_svg: Optional[str] = None,
               images: Optional[Dict[int, "tuple"]] = None,
               art_bytes: Optional[bytes] = None, art_ext: str = "png",
               modified: Optional[str] = None) -> bytes:
    """Build a valid EPUB 3 (returned as zip bytes). Stdlib only.

    Layout:
      mimetype (STORED, first)             application/epub+zip
      META-INF/container.xml
      OEBPS/content.opf                    package metadata + manifest + spine
      OEBPS/nav.xhtml                      EPUB3 nav (chapter TOC)
      OEBPS/toc.ncx                        NCX for back-compat
      OEBPS/style.css
      OEBPS/cover.jpg|png + cover.xhtml    raster cover when art is supplied, or
      OEBPS/cover.svg + cover.xhtml        SVG cover (given or generated)
      OEBPS/title.xhtml                    title page
      OEBPS/chap-NN.xhtml                  one per chapter (escaped, <p> split)

    When ``art_bytes`` is given, the cover artwork is embedded directly as the
    ``cover-image`` raster (smaller file — no base64 inflation, lower delivery
    fee); the SVG cover is used only when there is no art. ``modified``
    overrides the dcterms:modified timestamp (defaults to the build time) so
    tests stay deterministic. Chapter prose is never dropped.
    """
    import io

    b = graph.bible
    cover = cover_svg if cover_svg else _fallback_cover_svg(meta)
    book_id = _book_id(graph, meta)
    lang_code = _lang_code(meta.language)
    if not modified:
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    art_ext = (art_ext or "png").lower().lstrip(".")
    if art_ext not in _IMG_MEDIA:
        art_ext = "png"

    chapters = [graph.chapters[n] for n in sorted(graph.chapters)]
    images = images or {}
    image_assets: List[Dict[str, Any]] = []  # {file, data, media, id}

    # --- chapter XHTML ------------------------------------------------------
    chapter_files: List[Dict[str, str]] = []
    for i, rec in enumerate(chapters, 1):
        paras = _paragraphs(rec.text)
        p_html = []
        for j, p in enumerate(paras):
            cls = ' class="first"' if j == 0 else ""
            p_html.append(f'<p{cls}>{_esc(p)}</p>')
        # Optional chapter illustration, embedded right under the title.
        img_html = ""
        if rec.number in images:
            data, ext = images[rec.number]
            ext = (ext or "png").lower().lstrip(".")
            if ext not in _IMG_MEDIA:
                ext = "png"
            iname = f"img-{rec.number:02d}.{ext}"
            image_assets.append({"file": iname, "data": data,
                                 "media": _IMG_MEDIA[ext], "id": f"img{rec.number:02d}"})
            img_html = (
                f'<figure class="chapter-illus" style="margin:1em 0;text-align:center">'
                f'<img src="{iname}" alt="" style="max-width:100%;height:auto"/></figure>\n'
            )
        body = (
            f'<section epub:type="chapter">\n'
            f'<h2>{_esc(rec.title)}</h2>\n' + img_html + "\n".join(p_html) + "\n</section>"
        )
        fname = f"chap-{i:02d}.xhtml"
        chapter_files.append({
            "id": f"chap{i:02d}",
            "file": fname,
            "title": rec.title,
            "number": rec.number,
            "xhtml": _xhtml(rec.title, body, lang_code),
        })

    # --- title page ---------------------------------------------------------
    title_body_parts = [f'<h1>{_esc(meta.title)}</h1>']
    if meta.subtitle:
        title_body_parts.append(f'<p class="title-author"><em>{_esc(meta.subtitle)}</em></p>')
    title_body_parts.append(
        f'<p class="title-author">{_esc(meta.author_full())}</p>'
    )
    for extra in meta.contributor_names():
        title_body_parts.append(f'<p class="title-author">{_esc(extra)}</p>')
    title_xhtml = _xhtml(meta.title, "\n".join(title_body_parts), lang_code)

    # --- cover page ---------------------------------------------------------
    # With art the cover-image is the raster itself (KDP-friendly, no base64
    # inflation); the SVG cover is the no-art typographic fallback.
    cover_file = f"cover.{art_ext}" if art_bytes else "cover.svg"
    cover_body = (
        '<section epub:type="cover" class="cover">\n'
        f'<img src="{cover_file}" alt="Cover"/>\n'
        '</section>'
    )
    cover_xhtml = _xhtml("Cover", cover_body, lang_code)

    # --- nav.xhtml ----------------------------------------------------------
    nav_items = "\n".join(
        f'      <li><a href="{cf["file"]}">{_esc(cf["title"])}</a></li>'
        for cf in chapter_files
    )
    nav_body = (
        '<nav epub:type="toc" id="toc">\n'
        '  <h1>Table of Contents</h1>\n'
        '  <ol>\n'
        f'      <li><a href="title.xhtml">Title Page</a></li>\n'
        f'{nav_items}\n'
        '  </ol>\n'
        '</nav>'
    )
    nav_xhtml = _xhtml("Table of Contents", nav_body, lang_code)

    # --- toc.ncx ------------------------------------------------------------
    nav_points = []
    play = 1
    nav_points.append(
        f'    <navPoint id="nav-title" playOrder="{play}">'
        f'<navLabel><text>Title Page</text></navLabel>'
        f'<content src="title.xhtml"/></navPoint>'
    )
    for cf in chapter_files:
        play += 1
        nav_points.append(
            f'    <navPoint id="nav-{cf["id"]}" playOrder="{play}">'
            f'<navLabel><text>{_esc(cf["title"])}</text></navLabel>'
            f'<content src="{cf["file"]}"/></navPoint>'
        )
    toc_ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head><meta name="dtb:uid" content={_xml_quoteattr(book_id)}/></head>\n'
        f'  <docTitle><text>{_esc(meta.full_title())}</text></docTitle>\n'
        '  <navMap>\n' + "\n".join(nav_points) + '\n  </navMap>\n'
        '</ncx>\n'
    )

    # --- content.opf --------------------------------------------------------
    creators = []
    for idx, name in enumerate(meta.all_creators()):
        cid = "creator" if idx == 0 else f"creator{idx}"
        creators.append(
            f'    <dc:creator id="{cid}">{_esc(name)}</dc:creator>'
        )

    if art_bytes:
        cover_image_item = (
            f'    <item id="cover-image" href="{cover_file}" '
            f'media-type="{_IMG_MEDIA[art_ext]}" properties="cover-image"/>'
        )
        cover_page_item = (
            '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
        )
    else:
        cover_image_item = (
            '    <item id="cover-image" href="cover.svg" '
            'media-type="image/svg+xml" properties="cover-image"/>'
        )
        cover_page_item = (
            '    <item id="cover" href="cover.xhtml" '
            'media-type="application/xhtml+xml" properties="svg"/>'
        )

    manifest_items = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="css" href="style.css" media-type="text/css"/>',
        cover_image_item,
        cover_page_item,
        '    <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
    ]
    for cf in chapter_files:
        manifest_items.append(
            f'    <item id="{cf["id"]}" href="{cf["file"]}" '
            f'media-type="application/xhtml+xml"/>'
        )
    for ia in image_assets:
        manifest_items.append(
            f'    <item id="{ia["id"]}" href="{ia["file"]}" media-type="{ia["media"]}"/>'
        )

    spine_items = ['    <itemref idref="cover"/>', '    <itemref idref="title"/>']
    spine_items += [f'    <itemref idref="{cf["id"]}"/>' for cf in chapter_files]

    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="book-id" xml:lang="{_esc(lang_code)}">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f'    <dc:identifier id="book-id">{_esc(book_id)}</dc:identifier>\n'
        f'    <dc:title>{_esc(meta.full_title())}</dc:title>\n'
        + "\n".join(creators) + "\n"
        f'    <dc:language>{_esc(lang_code)}</dc:language>\n'
        f'    <meta property="dcterms:modified">{_esc(modified)}</meta>\n'
        '    <meta name="cover" content="cover-image"/>\n'
        '  </metadata>\n'
        '  <manifest>\n' + "\n".join(manifest_items) + '\n  </manifest>\n'
        '  <spine toc="ncx">\n' + "\n".join(spine_items) + '\n  </spine>\n'
        '</package>\n'
    )

    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        '  </rootfiles>\n'
        '</container>\n'
    )

    # --- assemble the zip ---------------------------------------------------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # mimetype MUST be first and STORED (uncompressed, no extra fields).
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, "application/epub+zip")

        def add(name: str, data: str):
            zf.writestr(name, data.encode("utf-8"), zipfile.ZIP_DEFLATED)

        add("META-INF/container.xml", container)
        add("OEBPS/content.opf", opf)
        add("OEBPS/nav.xhtml", nav_xhtml)
        add("OEBPS/toc.ncx", toc_ncx)
        add("OEBPS/style.css", _CSS)
        if art_bytes:
            zf.writestr(f"OEBPS/{cover_file}", art_bytes, zipfile.ZIP_DEFLATED)
        else:
            add("OEBPS/cover.svg", cover)
        add("OEBPS/cover.xhtml", cover_xhtml)
        add("OEBPS/title.xhtml", title_xhtml)
        for cf in chapter_files:
            add(f"OEBPS/{cf['file']}", cf["xhtml"])
        for ia in image_assets:
            zf.writestr(f"OEBPS/{ia['file']}", ia["data"], zipfile.ZIP_DEFLATED)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# 5. The copy-paste kit
# ---------------------------------------------------------------------------
def _listing_text(meta: "KdpMetadata") -> str:
    """A human copy-paste block — every KDP page-1 field, labeled as KDP shows."""
    L: List[str] = []
    L.append("=== AMAZON KDP — BOOK DETAILS (page 1) — copy field by field ===\n")
    L.append(f"Language:\n  {meta.language}\n")
    L.append(f"Book Title:\n  {meta.title}\n")
    L.append(f"Subtitle:\n  {meta.subtitle or '(leave blank)'}\n")
    series_line = meta.series or "(leave blank)"
    part_line = meta.series_part or "(leave blank)"
    L.append(f"Series:\n  {series_line}\n  Series part number: {part_line}\n")
    L.append(f"Edition Number:\n  {meta.edition or '(leave blank)'}\n")
    L.append(
        "Primary Author:\n"
        f"  First name: {meta.author_first}\n"
        f"  Last name:  {meta.author_last}\n"
    )
    if meta.contributors:
        L.append("Contributors:")
        for i, c in enumerate(meta.contributors, 1):
            L.append(f"  {i}. First: {c.get('first','')}  Last: {c.get('last','')}")
        L.append("")
    else:
        L.append("Contributors:\n  (none)\n")
    L.append("Description (paste into the Description box; light HTML allowed):")
    L.append(meta.description)
    L.append(f"  [length: {len(meta.description)}/{MAX_DESCRIPTION_CHARS} chars]\n")
    rights = ("I own the copyright and I hold the necessary publishing rights"
              if meta.publishing_rights == "owned"
              else "This is a public domain work")
    L.append(f"Publishing Rights:\n  {rights}\n")
    L.append("Primary Audience — Sexually Explicit Images or Title:\n"
             f"  {'Yes' if meta.sexually_explicit else 'No'}\n")
    if meta.reading_age_min or meta.reading_age_max:
        L.append("Reading age:\n"
                 f"  Minimum: {meta.reading_age_min or '(blank)'}\n"
                 f"  Maximum: {meta.reading_age_max or '(blank)'}\n")
    else:
        L.append("Reading age:\n  (leave blank — adult title)\n")
    L.append(f"Primary marketplace:\n  {meta.primary_marketplace}\n")
    L.append("Categories (choose 1-3):")
    if meta.categories:
        for i, c in enumerate(meta.categories, 1):
            L.append(f"  {i}. {c}")
    else:
        L.append("  (none generated — pick 1-3 in KDP)")
    L.append("")
    L.append("Keywords (1-7, each <= 50 chars):")
    if meta.keywords:
        for i, k in enumerate(meta.keywords, 1):
            L.append(f"  {i}. {k}")
    else:
        L.append("  (none generated)")
    L.append("")
    L.append("Optional contest keyword (Kindle Storyteller UK 2026):")
    L.append(f"  Add '{STORYTELLER_KEYWORD}' as a keyword to enter the contest.")
    L.append("")
    return "\n".join(L)


def _checklist_md(meta: "KdpMetadata", *, cover_image: str = "") -> str:
    used = len(meta.keywords)
    free_slots = MAX_KEYWORDS - used
    storyteller_tip = (
        f"You have {free_slots} free keyword slot(s) — you can add "
        f"`{STORYTELLER_KEYWORD}` to enter the Kindle Storyteller UK 2026 contest."
        if free_slots > 0 else
        f"All 7 keyword slots are used. To enter the Kindle Storyteller UK 2026 "
        f"contest, swap one for `{STORYTELLER_KEYWORD}`."
    )
    # KDP does NOT accept SVG covers (eBook covers must be JPEG/TIFF), so point
    # the author at the raster when the kit has one.
    if cover_image:
        is_jpeg = cover_image.lower().endswith((".jpg", ".jpeg"))
        cover_step = (
            f"**Cover** — upload `{cover_image}`"
            + ("" if is_jpeg else
               " (convert it to a JPG first — KDP eBook covers must be JPEG or TIFF)")
            + ". KDP does NOT accept SVG."
        )
        cover_files = (
            f"- `{cover_image}` — the raster cover to upload\n"
            "- `cover.svg` — editable cover source (KDP does not accept SVG uploads)"
        )
    else:
        cover_step = (
            "**Cover** — KDP does NOT accept SVG: export `cover.svg` to a "
            "high-res JPG (the web UI's cover export, or any SVG converter) "
            "and upload that"
        )
        cover_files = "- `cover.svg` — editable cover source (convert to JPG before uploading)"
    return f"""# KDP Upload Checklist — {meta.full_title()}

This kit was generated for Amazon Kindle Direct Publishing (KDP). Work through it
top to bottom. Open https://kdp.amazon.com and click **Create** > **Kindle eBook**.

## Page 1 — Kindle eBook Details
Paste from `kdp-listing.txt`, field by field:

- [ ] **Language** — {meta.language}
- [ ] **Book Title** — paste the title (do NOT include the subtitle here)
- [ ] **Subtitle** — paste separately; KDP auto-inserts the colon. *Cannot be changed after publish.*
- [ ] **Series** / **Series part number** — only if part of a series
- [ ] **Edition Number** — optional; *cannot be changed after publish*
- [ ] **Author** — First name / Last name (pen name OK)
- [ ] **Contributors** — up to 9, in entered order
- [ ] **Description** — paste the marketing copy (max 4000 chars; light HTML OK)
- [ ] **Publishing Rights** — {"I own the copyright..." if meta.publishing_rights == "owned" else "Public domain work"}
- [ ] **Primary Audience / Sexually Explicit** — {"Yes" if meta.sexually_explicit else "No"}
- [ ] **Reading age** — {"set min/max" if (meta.reading_age_min or meta.reading_age_max) else "leave blank (adult title)"}
- [ ] **Primary marketplace** — {meta.primary_marketplace}
- [ ] **Categories** — choose up to 3 (suggestions in the listing file)
- [ ] **Keywords** — up to 7, each <= 50 chars (suggestions in the listing file)

## Page 2 — Kindle eBook Content
- [ ] **Manuscript** — upload `manuscript.epub`
- [ ] {cover_step}
- [ ] **ISBN** — not required for Kindle eBooks. KDP can assign a **free ISBN**, or you may leave it blank.
- [ ] Use the **Kindle Previewer** to confirm the EPUB renders correctly.

## Page 3 — Kindle eBook Pricing
- [ ] **Territories** — select worldwide rights (or specific territories you hold).
- [ ] **Royalty & Pricing** — pick the 35% or 70% plan and set your list price.
- [ ] Review and **Publish** (review can take up to 72 hours).

## Tip — Kindle Storyteller UK 2026
{storyteller_tip}

---
Files in this kit:
- `metadata.json` — machine-readable copy of every field
- `manuscript.epub` — the upload-ready ebook
{cover_files}
- `kdp-listing.txt` — copy-paste block of all page-1 fields
"""


def _find_cover_art(out_dir: str):
    """Load AI cover art saved next to the kit (cover-art.<ext>), if any."""
    if not os.path.isdir(out_dir):
        return None, None
    for name in sorted(os.listdir(out_dir)):
        if name.startswith("cover-art."):
            ext = name.rsplit(".", 1)[-1].lower()
            if ext not in _IMG_MEDIA:
                continue  # unsupported extension; keep looking
            try:
                with open(os.path.join(out_dir, name), "rb") as f:
                    return f.read(), ext
            except OSError:
                continue
    return None, None


def build_kdp_kit(graph, meta: "KdpMetadata", out_dir: str, *,
                  cover_svg: Optional[str] = None,
                  trim=(6.0, 9.0), paper: str = "white",
                  marketing: Optional[Dict[str, Any]] = None,
                  images: Optional[Dict[int, "tuple"]] = None,
                  art_bytes: Optional[bytes] = None,
                  art_ext: str = "png") -> Dict[str, Any]:
    """Write the full KDP kit to ``out_dir`` and return the paths + metadata.

    Produces the ebook kit (metadata.json, manuscript.epub, cover.svg,
    kdp-listing.txt, CHECKLIST.md) PLUS the print kit under ``print/``
    (interior.docx, print-spec.json, print-cover.svg) and, when a pre-generated
    ``marketing`` dict is supplied, marketing.json. The cover embedded in the EPUB
    and written to cover.svg is the one you pass (or a generated fallback).

    When cover ART is available — passed via ``art_bytes``/``art_ext`` or found
    on disk as ``out_dir/cover-art.<ext>`` (where generate-cover saves it) — a
    raster ``cover.<ext>`` is written to the kit (KDP does not accept SVG
    uploads) and embedded directly in the EPUB as the cover-image.

    ``marketing`` is accepted as data (not generated here) so this stays usable in
    pure-export contexts with no LLM/ledger. New kwargs are optional — existing
    callers are unaffected.
    """
    # Local import keeps core import-light (no print path pulled in unless used).
    from . import print_export as _pe

    os.makedirs(out_dir, exist_ok=True)
    print_dir = os.path.join(out_dir, "print")
    os.makedirs(print_dir, exist_ok=True)
    cover = cover_svg if cover_svg else _fallback_cover_svg(meta)
    if art_bytes is None:
        art_bytes, found_ext = _find_cover_art(out_dir)
        if art_bytes is not None:
            art_ext = found_ext
    art_ext = (art_ext or "png").lower().lstrip(".")
    if art_ext not in _IMG_MEDIA:
        art_ext = "png"

    paths = {
        "metadata": os.path.join(out_dir, "metadata.json"),
        "epub": os.path.join(out_dir, "manuscript.epub"),
        "cover": os.path.join(out_dir, "cover.svg"),
        "listing": os.path.join(out_dir, "kdp-listing.txt"),
        "checklist": os.path.join(out_dir, "CHECKLIST.md"),
        "docx": os.path.join(print_dir, "interior.docx"),
        "print_spec": os.path.join(print_dir, "print-spec.json"),
        "print_cover": os.path.join(print_dir, "print-cover.svg"),
    }
    cover_image_name = ""
    if art_bytes:
        cover_image_name = f"cover.{art_ext}"
        paths["cover_image"] = os.path.join(out_dir, cover_image_name)

    meta_dict = meta.to_dict()
    with open(paths["metadata"], "w", encoding="utf-8") as f:
        json.dump(meta_dict, f, indent=2, ensure_ascii=False)
    with open(paths["epub"], "wb") as f:
        f.write(build_epub(graph, meta, cover_svg=cover, images=images,
                           art_bytes=art_bytes, art_ext=art_ext))
    with open(paths["cover"], "w", encoding="utf-8") as f:
        f.write(cover)
    if art_bytes:
        with open(paths["cover_image"], "wb") as f:
            f.write(art_bytes)
    with open(paths["listing"], "w", encoding="utf-8") as f:
        f.write(_listing_text(meta))
    with open(paths["checklist"], "w", encoding="utf-8") as f:
        f.write(_checklist_md(meta, cover_image=cover_image_name))

    # --- print kit ---------------------------------------------------------
    spec = _pe.print_spec(graph, meta, trim=trim, paper=paper)
    with open(paths["docx"], "wb") as f:
        f.write(_pe.build_docx(graph, meta, trim=trim))
    with open(paths["print_spec"], "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
    with open(paths["print_cover"], "w", encoding="utf-8") as f:
        f.write(_pe.build_print_cover_svg(graph, meta, spec, front_cover_svg=cover))

    result: Dict[str, Any] = {"paths": paths, "metadata": meta_dict,
                              "print_spec": spec}

    # --- optional marketing copy ------------------------------------------
    if marketing is not None:
        paths["marketing"] = os.path.join(out_dir, "marketing.json")
        with open(paths["marketing"], "w", encoding="utf-8") as f:
            json.dump(marketing, f, indent=2, ensure_ascii=False)
        result["marketing"] = marketing

    return result
