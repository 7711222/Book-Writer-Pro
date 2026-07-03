"""Writer stage: generate one chapter's prose.

The stable bible is passed as the cached prefix; only the small volatile brief
(rolling synopsis + previous-chapter tail + this chapter's beats) goes in the
user turn. The model still has full context but we pay ~0.1x for the big part.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from .config import Settings
from .costs import CostLedger
from .graph import StoryGraph
from .llm import LLM
from .models import ChapterPlan, ChapterRecord
from .prompts import WRITER_SYSTEM


def _word_count(text: str) -> int:
    return len(text.split())


def write_chapter(
    llm: LLM, settings: Settings, ledger: CostLedger, graph: StoryGraph,
    plan: ChapterPlan, on_delta: Optional[Callable[[str], None]] = None,
) -> ChapterRecord:
    is_first = plan.number == 1
    is_last = plan.number == graph.bible.target_chapters or plan.number == len(graph.bible.outline)

    # Stable, cacheable prefix: the frozen bible spine (never mutated mid-run).
    cached = graph.static_prefix() if settings.use_cache else None

    # Volatile suffix: only what changes per chapter.
    parts: List[str] = []
    if not settings.use_cache:
        # No caching: include the frozen spine inline so the model still has the
        # canonical bible (paid at full price).
        parts.append(graph.static_prefix())
        parts.append("")

    parts.append("## Story so far (rolling synopsis)")
    parts.append(graph.rolling_synopsis())

    # Current mutable continuity (status/knowledge/open threads) — kept out of the
    # cached prefix on purpose, since it changes every chapter.
    dyn = graph.dynamic_state(plan)
    if dyn:
        parts.append("\n## Continuity state right now")
        parts.append(dyn)

    tail = graph.prev_tail(plan.number, settings.prev_tail_words)
    if tail:
        parts.append("\n## End of the previous chapter (continue cleanly from here)")
        parts.append("…" + tail)

    parts.append("\n## Write this chapter now")
    parts.append(plan.brief())
    if is_first:
        parts.append("\nThis is the OPENING chapter: hook immediately, establish the protagonist "
                     "and the core stakes, and avoid throat-clearing.")
    if is_last:
        parts.append("\nThis is the FINAL chapter: resolve the open threads and deliver a "
                     "satisfying ending. Do not introduce a new cliffhanger.")
    target = plan.word_target or 2000
    lo, hi = int(target * 0.85), int(target * 1.15)
    parts.append(
        f"\nLENGTH (important): write a COMPLETE chapter of about {target} words "
        f"(aim for the {lo}–{hi} range). Develop the scenes fully to reach this "
        f"length — do NOT stop early, summarize, or pad with filler."
    )

    text = llm.complete_text(
        stage="write",
        model=settings.profile.write,
        system=WRITER_SYSTEM,
        user="\n".join(parts),
        # Scale the output budget to the requested length so a long chapter isn't
        # truncated (~1.6 tokens/word + headroom), but never below the configured
        # floor and capped to a sane ceiling.
        max_tokens=min(32000, max(settings.max_tokens_write, int(target * 2) + 1000)),
        ledger=ledger,
        cached=cached,
        use_cache=settings.use_cache,
        cache_ttl=settings.cache_ttl,
        on_delta=on_delta,
    ).strip()
    if not text:
        # A refusal or a thinking-consumed output budget yields empty prose.
        # Saving it would permanently mark the chapter written (the resume check
        # is file-existence only) — a silent hole in the manuscript. Fail loudly.
        raise ValueError(
            f"model returned no prose for chapter {plan.number} — likely a "
            "refusal or an exhausted output budget; retry or raise "
            "Settings.max_tokens_write"
        )

    rec = ChapterRecord(number=plan.number, title=plan.title, text=text)
    rec.word_count = _word_count(text)
    # Carry any existing rolling-synopsis line forward so a revise/regenerate that
    # doesn't re-extract can't blank it (which would punch a hole in the continuity
    # context fed to later chapters). Re-extraction, when it runs, overwrites this.
    prev = graph.chapters.get(plan.number)
    if prev is not None and prev.synopsis_line:
        rec.synopsis_line = prev.synopsis_line
    ledger.add_words(rec.word_count)
    return rec


def revise_chapter(
    llm: LLM, settings: Settings, ledger: CostLedger, graph: StoryGraph,
    plan: ChapterPlan, current_text: str, instructions: str = "",
    on_delta: Optional[Callable[[str], None]] = None,
) -> ChapterRecord:
    """Rewrite an EXISTING chapter per *instructions*, keeping it consistent with
    the bible + continuity. Used for both AI polish and targeted edits of imported
    or generated prose. Returns a fresh ChapterRecord with the revised text."""
    cached = graph.static_prefix() if settings.use_cache else None

    parts: List[str] = []
    if not settings.use_cache:
        parts.append(graph.static_prefix())
        parts.append("")
    parts.append("## Story so far (rolling synopsis)")
    parts.append(graph.rolling_synopsis())
    dyn = graph.dynamic_state(plan)
    if dyn:
        parts.append("\n## Continuity state right now")
        parts.append(dyn)
    parts.append("\n## The chapter to revise (current text)")
    parts.append(current_text or "(empty)")
    parts.append("\n## Revision instructions")
    parts.append(instructions.strip() or
                 "Polish the prose: tighten sentences, sharpen imagery, fix awkward "
                 "phrasing and repetition. Keep the plot, characters, POV, and "
                 "roughly the same length.")
    parts.append("\nReturn the COMPLETE revised chapter prose only — no notes, no "
                 "preamble, no headings.")

    text = llm.complete_text(
        stage="write",
        model=settings.profile.write,
        system=WRITER_SYSTEM,
        user="\n".join(parts),
        max_tokens=settings.max_tokens_write,
        ledger=ledger,
        cached=cached,
        use_cache=settings.use_cache,
        cache_ttl=settings.cache_ttl,
        on_delta=on_delta,
    ).strip()
    if not text:
        # Same guard as write_chapter: never let an empty revision silently
        # replace real prose.
        raise ValueError(
            f"model returned no prose for the revision of chapter {plan.number} "
            "— likely a refusal or an exhausted output budget; retry or raise "
            "Settings.max_tokens_write"
        )

    rec = ChapterRecord(number=plan.number, title=plan.title, text=text)
    rec.word_count = _word_count(text)
    # Carry any existing rolling-synopsis line forward so a revise/regenerate that
    # doesn't re-extract can't blank it (which would punch a hole in the continuity
    # context fed to later chapters). Re-extraction, when it runs, overwrites this.
    prev = graph.chapters.get(plan.number)
    if prev is not None and prev.synopsis_line:
        rec.synopsis_line = prev.synopsis_line
    ledger.add_words(rec.word_count)
    return rec
