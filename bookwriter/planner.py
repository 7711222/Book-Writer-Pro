"""Planner stage: premise -> full story bible + chapter outline.

One model call (no caching needed — it runs once). Produces the stable spine that
every chapter then reuses via the prompt cache.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import Settings
from .costs import CostLedger
from .llm import LLM
from .models import Bible, Character, Location, Item, PlotThread, ChapterPlan
from .prompts import PLANNER_SYSTEM, PLAN_SCHEMA


def _rel_to_dict(rels: List[Dict[str, str]]) -> Dict[str, str]:
    return {r["with"]: r["relation"] for r in rels if "with" in r and "relation" in r}


def build_bible(d: Dict[str, Any]) -> Bible:
    """Map the planner's JSON into a Bible (converting relationship arrays to maps)."""
    chars: List[Character] = []
    for c in d.get("characters", []):
        rels = _rel_to_dict(c.pop("relationships", []) if isinstance(c.get("relationships"), list) else [])
        chars.append(Character(
            id=c.get("id", ""), name=c.get("name", ""), role=c.get("role", ""),
            traits=c.get("traits", []), appearance=c.get("appearance", ""),
            voice=c.get("voice", ""), arc=c.get("arc", ""),
            relationships=rels,
            first_chapter=None,
        ))
    bible = Bible(
        title=d.get("title", "Untitled"),
        premise=d.get("premise", ""),
        genre=d.get("genre", ""),
        tone=d.get("tone", ""),
        audience=d.get("audience", ""),
        themes=d.get("themes", []),
        pov=d.get("pov", "third-person limited"),
        tense=d.get("tense", "past"),
        style_guide=d.get("style_guide", ""),
        logline=d.get("logline", ""),
        target_chapters=d.get("target_chapters", len(d.get("outline", [])) or 12),
        act_structure=d.get("act_structure", ""),
        characters=chars,
        locations=[Location.from_dict(x) for x in d.get("locations", [])],
        items=[Item.from_dict(x) for x in d.get("items", [])],
        threads=[PlotThread.from_dict({**x, "status": "open"}) for x in d.get("threads", [])],
        outline=[ChapterPlan.from_dict(x) for x in d.get("outline", [])],
    )
    bible.outline.sort(key=lambda p: p.number)
    if not bible.outline:
        # complete_json returns {} for an empty/refused response; without this
        # guard the "plan" would persist and write_all would finish instantly
        # with an empty manuscript instead of surfacing the failure.
        raise ValueError("planner returned no chapters")
    # Renumber sequentially: the json_object provider path can't structurally
    # enforce the schema, so chapter numbers may be missing (coerced to 0) or
    # duplicated — which would make bible.plan(n) ambiguous and break the
    # writer's is-last check. Order (after the sort above) is authoritative.
    for i, p in enumerate(bible.outline, 1):
        p.number = i
    bible.target_chapters = len(bible.outline)
    return bible


def plan_book(
    llm: LLM, settings: Settings, ledger: CostLedger, *,
    premise: str, chapters: Optional[int] = None, words_per_chapter: int = 2000,
    title: Optional[str] = None, genre: Optional[str] = None,
    extra_guidance: str = "",
) -> Bible:
    target = chapters or 12
    user = [
        f"PREMISE:\n{premise}",
        f"\nTarget length: {target} chapters of roughly {words_per_chapter} words each.",
    ]
    if title:
        user.append(f"Working title: {title}")
    if genre:
        user.append(f"Genre: {genre}")
    if extra_guidance:
        user.append(f"\nAdditional guidance:\n{extra_guidance}")
    user.append(
        "\nDesign the bible and a complete outline of exactly "
        f"{target} chapters. Set word_target on each chapter (default {words_per_chapter})."
    )

    data = llm.complete_json(
        stage="plan",
        model=settings.profile.plan,
        system=PLANNER_SYSTEM,
        user="\n".join(user),
        schema=PLAN_SCHEMA,
        max_tokens=settings.max_tokens_plan,
        ledger=ledger,
        cached=None,
        use_cache=False,
    )
    bible = build_bible(data)
    # The requested per-chapter length is the user's explicit choice (the
    # Short/Medium/Long picker), so it is AUTHORITATIVE: force every chapter's
    # word_target to it rather than letting the planner model pick its own
    # (which made the length setting appear to do nothing). Callers that want the
    # model to vary lengths can edit word_target on individual chapters later.
    if words_per_chapter:
        for p in bible.outline:
            p.word_target = words_per_chapter
    return bible


# Schema for proposing additional chapters that CONTINUE an existing book.
_EXTEND_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "purpose", "tension", "beats", "forward_hook"],
    "properties": {
        "title": {"type": "string"},
        "act": {"type": "integer"},
        "purpose": {"type": "string"},
        "tension": {"type": "string"},
        "beats": {"type": "array", "items": {"type": "string"}},
        "forward_hook": {"type": "string"},
        "pov_character": {"type": "string"},
        "word_target": {"type": "integer"},
    },
}
EXTEND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outline"],
    "properties": {"outline": {"type": "array", "items": _EXTEND_ITEM}},
}

EXTEND_SYSTEM = """You are a master novelist continuing an existing book. Given the \
story so far (bible summary + rolling synopsis + open threads), design the NEXT \
chapters: each with a clear purpose that advances the arc, a central tension, \
ordered beats, and a forward hook. Honor the established characters, world, and \
voice; move open threads toward resolution. Output ONLY the structured outline."""


def extend_outline(
    llm: LLM, settings: Settings, ledger: CostLedger, graph, *,
    count: int = 3, words_per_chapter: int = 2000, guidance: str = "",
) -> List[ChapterPlan]:
    """Propose ``count`` new chapters that continue *graph*'s story. Returns
    ChapterPlan entries numbered after the current outline (not yet written)."""
    b = graph.bible
    start = max((p.number for p in b.outline), default=0) + 1
    cast = ", ".join(f"{c.name} ({c.role})" for c in b.characters[:12]) or "(none recorded)"
    open_threads = ", ".join(t.name for t in b.threads if t.status != "resolved") or "(none open)"
    user = [
        f"TITLE: {b.title}", f"GENRE: {b.genre} | TONE: {b.tone}",
        f"LOGLINE: {b.logline}" if b.logline else "",
        f"CAST: {cast}", f"OPEN THREADS: {open_threads}",
        "\nSTORY SO FAR (rolling synopsis):", graph.rolling_synopsis(),
        f"\nAUTHOR GUIDANCE: {guidance}" if guidance else "",
        f"\nPropose the next {count} chapters (they will be numbered "
        f"{start}–{start + count - 1}), each ~{words_per_chapter} words.",
    ]
    items: List[Dict[str, Any]] = []
    try:
        data = llm.complete_json(
            stage="plan", model=settings.profile.plan, system=EXTEND_SYSTEM,
            user="\n".join(p for p in user if p), schema=EXTEND_SCHEMA,
            max_tokens=settings.max_tokens_plan, ledger=ledger,
            cached=None, use_cache=False,
        )
        items = list(data.get("outline") or [])
    except Exception:  # noqa: BLE001 - fall back to blank planned chapters
        items = []

    def _as_int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    out: List[ChapterPlan] = []
    for i in range(count):
        it = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        out.append(ChapterPlan(
            number=start + i,
            title=str(it.get("title") or f"Chapter {start + i}")[:120],
            act=_as_int(it.get("act"), 3),
            purpose=str(it.get("purpose") or ""),
            pov_character=str(it.get("pov_character") or ""),
            beats=[str(x) for x in (it.get("beats") or []) if isinstance(x, str)],
            tension=str(it.get("tension") or ""),
            forward_hook=str(it.get("forward_hook") or ""),
            # `or words_per_chapter` re-applies the falsy backfill (a model-emitted
            # 0 must not leak "write 0 words"), matching plan_book's behavior.
            word_target=_as_int(it.get("word_target"), words_per_chapter) or words_per_chapter,
        ))
    return out
