"""The continuity knowledge graph.

This is the project's analogue of Understand-Anything's committed JSON knowledge
graph: a single shared source of truth that every stage reads from and the
extractor incrementally updates. It is the mechanism that keeps characters and
plot consistent across chapters *without* re-feeding prior prose to the model.

Responsibilities:
  * hold the Bible (stable spine) + per-chapter records (evolving state)
  * render the *stable cacheable prefix* (full bible) for the writer
  * render the *relevant slice* (only entities in a given chapter) — used when
    caching is off, and always for the cheap extractor/checker stages
  * apply structured state deltas produced by the extractor (incremental update)
  * maintain the bounded rolling synopsis
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from .models import (
    Bible, Character, Location, Item, PlotThread, TimelineEvent,
    ChapterPlan, ChapterRecord,
)


@dataclass
class StateDelta:
    """Structured changes extracted from a freshly-written chapter.

    The extractor emits this; the graph applies it. Keeping updates structured
    (rather than re-summarizing the whole world each time) is what makes the
    continuity update incremental and cheap.
    """
    chapter: int
    synopsis_line: str = ""
    timeline_summary: str = ""
    time_marker: str = ""
    character_updates: List[Dict[str, Any]] = field(default_factory=list)
    # each: {id, status?, knowledge_added: [..], relationship_updates: {id: rel}, note?}
    new_characters: List[Dict[str, Any]] = field(default_factory=list)
    new_locations: List[Dict[str, Any]] = field(default_factory=list)
    new_items: List[Dict[str, Any]] = field(default_factory=list)
    threads_opened: List[Dict[str, Any]] = field(default_factory=list)   # {id?, name, description}
    threads_resolved: List[str] = field(default_factory=list)            # thread ids
    continuity_flags: List[str] = field(default_factory=list)            # noticed risks

    @classmethod
    def from_dict(cls, chapter: int, d: Dict[str, Any]) -> "StateDelta":
        # Coerce off-spec model output at the boundary: the dict-list fields keep
        # only dict entries, the string-list fields keep only strings, and the
        # string scalars drop non-strings (a model-emitted number would otherwise
        # crash record_chapter's .strip()), so apply_delta never meets a bare
        # string where it expects an object or vice versa.
        def _dicts(v):
            return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

        def _strs(v):
            return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

        def _str(v):
            return v if isinstance(v, str) else ""

        return cls(
            chapter=chapter,
            synopsis_line=_str(d.get("synopsis_line")),
            timeline_summary=_str(d.get("timeline_summary")),
            time_marker=_str(d.get("time_marker")),
            character_updates=_dicts(d.get("character_updates")),
            new_characters=_dicts(d.get("new_characters")),
            new_locations=_dicts(d.get("new_locations")),
            new_items=_dicts(d.get("new_items")),
            threads_opened=_dicts(d.get("threads_opened")),
            threads_resolved=_strs(d.get("threads_resolved")),
            continuity_flags=_strs(d.get("continuity_flags")),
        )


class StoryGraph:
    """Holds the bible + evolving state and renders prompt context."""

    def __init__(self, bible: Optional[Bible] = None):
        self.bible: Bible = bible or Bible()
        self.timeline: List[TimelineEvent] = []
        self.chapters: Dict[int, ChapterRecord] = {}
        self.synopsis: List[str] = []  # one bounded line per written chapter, in order

    # ------------------------------------------------------------------
    # Rendering — STABLE prefix (cache this once per book)
    # ------------------------------------------------------------------
    def static_prefix(self) -> str:
        """The FROZEN spine of the book: identities, world, style, and the full
        outline as planned. It deliberately excludes everything the extractor
        mutates (status, accumulated knowledge, relationships, discovered
        entities, thread status) so its bytes never change during a run — which
        is what lets it stay cached and be read at ~0.1x for the whole book.

        'Planned' entities are those introduced at planning time (first_chapter /
        opened_chapter is None); entities discovered mid-book live in the
        volatile dynamic-state block instead.
        """
        b = self.bible
        out: List[str] = []
        out.append("# STORY BIBLE (canonical, fixed for the whole book)")
        out.append(f"Title: {b.title}")
        if b.logline:
            out.append(f"Logline: {b.logline}")
        out.append(f"Genre: {b.genre} | Tone: {b.tone} | Audience: {b.audience}")
        out.append(f"POV: {b.pov} | Tense: {b.tense}")
        if b.themes:
            out.append("Themes: " + ", ".join(b.themes))
        if b.premise:
            out.append("\n## Premise\n" + b.premise)
        if b.act_structure:
            out.append("\n## Act structure\n" + b.act_structure)
        if b.style_guide:
            out.append("\n## Style guide\n" + b.style_guide)

        planned_chars = [c for c in b.characters if c.first_chapter is None]
        if planned_chars:
            out.append("\n## Characters (identity)")
            out.extend(c.static_card() for c in planned_chars)
        planned_locs = [l for l in b.locations if l.first_chapter is None]
        if planned_locs:
            out.append("\n## Locations")
            out.extend(l.card() for l in planned_locs)
        planned_items = [i for i in b.items if i.first_chapter is None]
        if planned_items:
            out.append("\n## Items")
            out.extend(i.card() for i in planned_items)
        planned_threads = [t for t in b.threads if t.opened_chapter is None]
        if planned_threads:
            out.append("\n## Plot threads (definitions)")
            for t in planned_threads:
                s = f"[{t.id}] {t.name}"
                if t.description:
                    s += f" — {t.description}"
                out.append(s)
        if b.outline:
            out.append("\n## Full chapter outline")
            for p in b.outline:
                hook = f" -> {p.forward_hook}" if p.forward_hook else ""
                out.append(f"  {p.number}. {p.title} (Act {p.act}) — {p.purpose}{hook}")
        return "\n".join(out)

    def dynamic_state(self, plan: ChapterPlan) -> str:
        """Volatile continuity for THIS chapter — goes after the cache breakpoint.

        Carries everything the static prefix omits but the writer needs right
        now: the current mutable state of the characters in the scene, any
        entities discovered after planning, and which threads are still open.
        Small and changing, so it is paid at full price — but it's a fraction of
        the frozen spine that rides the cache.
        """
        b = self.bible
        ids = set(plan.character_ids) | ({plan.pov_character} if plan.pov_character else set())
        out: List[str] = []

        dyn_lines = []
        for c in b.characters:
            if c.first_chapter is not None:
                # Discovered mid-book -> not in the frozen prefix; give full
                # identity even when absent from the planned cast (the outline
                # predates the discovery, so plan.character_ids can't list them).
                dyn_lines.append(c.card())
            elif c.id in ids:
                d = c.dynamic_card()
                if d:
                    dyn_lines.append(d)
        if dyn_lines:
            out.append("## Current character state")
            out.extend(dyn_lines)

        new_locs = [l for l in b.locations if l.first_chapter is not None
                    and l.id in set(plan.location_ids)]
        if new_locs:
            out.append("\n## Locations established mid-book")
            out.extend(l.card() for l in new_locs)

        new_items = [i for i in b.items if i.first_chapter is not None]
        if new_items:
            out.append("\n## Items discovered mid-book")
            out.extend(i.card() for i in new_items)

        open_threads = [t for t in b.threads if t.status == "open"]
        if open_threads:
            out.append("\n## Still-open plot threads (keep tracking / pay off)")
            out.extend(f"[{t.id}] {t.name}" for t in open_threads)
        return "\n".join(out)

    # ------------------------------------------------------------------
    # Rendering — RELEVANT slice (small; used when not caching / for extractor)
    # ------------------------------------------------------------------
    def relevant_slice(self, plan: ChapterPlan) -> str:
        """Only the entities this chapter touches, plus open threads.

        Mirrors Understand-Anything's 'pre-resolved maps passed to analyzers':
        give the model exactly what it needs to stay consistent, nothing more.
        """
        b = self.bible
        ids = set(plan.character_ids) | ({plan.pov_character} if plan.pov_character else set())
        out: List[str] = []
        chars = [c for c in b.characters if c.id in ids]
        if chars:
            out.append("## Characters in this chapter")
            out.extend(c.card() for c in chars)
        locs = [l for l in b.locations if l.id in set(plan.location_ids)]
        if locs:
            out.append("\n## Locations in this chapter")
            out.extend(l.card() for l in locs)
        if b.items:
            # All items, always: the cards are one-liners, and the extractor is
            # told to report "anything not already in the bible" — without this
            # it re-reports the same item every chapter under variant id slugs.
            out.append("\n## Items")
            out.extend(i.card() for i in b.items)
        open_threads = [t for t in b.threads if t.status == "open"]
        if open_threads:
            out.append("\n## Open plot threads")
            out.extend(t.card() for t in open_threads)
        return "\n".join(out)

    def rolling_synopsis(self) -> str:
        if not self.synopsis:
            return "(This is the opening chapter — no prior story.)"
        return "\n".join(f"Ch {i + 1}: {s}" for i, s in enumerate(self.synopsis))

    def prev_tail(self, number: int, words: int) -> str:
        # words<=0 must mean "no tail" — toks[-0:] is the WHOLE list (slice quirk),
        # which would silently inline the entire previous chapter (huge token cost).
        if words <= 0:
            return ""
        prev = self.chapters.get(number - 1)
        if not prev or not prev.text:
            return ""
        toks = prev.text.split()
        return " ".join(toks[-words:])

    # ------------------------------------------------------------------
    # Incremental update — apply a structured delta from the extractor
    # ------------------------------------------------------------------
    def apply_delta(self, delta: StateDelta) -> None:
        b = self.bible

        for nc in delta.new_characters:
            if isinstance(nc, dict) and "id" in nc and not b.character(nc["id"]):
                c = Character.from_dict(nc)
                c.first_chapter = c.first_chapter or delta.chapter
                b.characters.append(c)
        for nl in delta.new_locations:
            if isinstance(nl, dict) and "id" in nl and not b.location(nl["id"]):
                loc = Location.from_dict(nl)
                loc.first_chapter = loc.first_chapter or delta.chapter
                b.locations.append(loc)
        for ni in delta.new_items:
            if isinstance(ni, dict) and "id" in ni and not any(it.id == ni["id"] for it in b.items):
                item = Item.from_dict(ni)
                item.first_chapter = item.first_chapter or delta.chapter
                b.items.append(item)

        for upd in delta.character_updates:
            if not isinstance(upd, dict):
                continue
            c = b.character(upd.get("id", ""))
            if not c:
                continue
            if upd.get("status"):
                c.status = upd["status"]
            # Guard off-spec model output: tolerate an explicit null AND a
            # non-list/non-dict value (a bare string would otherwise be iterated
            # char-by-char into the knowledge list).
            knowledge_added = upd.get("knowledge_added") or []
            if isinstance(knowledge_added, list):
                for fact in knowledge_added:
                    # Keep only string facts — a stray dict/number would later crash
                    # "; ".join(c.knowledge) when the character card is rendered.
                    if isinstance(fact, str) and fact and fact not in c.knowledge:
                        c.knowledge.append(fact)
            rel_updates = upd.get("relationship_updates") or {}
            if isinstance(rel_updates, dict):
                for oid, rel in rel_updates.items():
                    if isinstance(oid, str) and isinstance(rel, str):
                        c.relationships[oid] = rel

        for to in delta.threads_opened:
            if not isinstance(to, dict):
                continue
            # Coerce off-spec model output to strings before slugging/storing — a
            # non-string name would otherwise crash _slug (iterates the value).
            tid_raw = to.get("id")
            name = to.get("name")
            name = name if isinstance(name, str) else ""
            desc = to.get("description")
            desc = desc if isinstance(desc, str) else ""
            tid = (tid_raw if isinstance(tid_raw, str) and tid_raw else
                   _slug(name or f"thread{len(b.threads) + 1}"))
            if not any(t.id == tid for t in b.threads):
                b.threads.append(PlotThread(
                    id=tid, name=name or tid,
                    description=desc,
                    status="open", opened_chapter=delta.chapter,
                ))
        for tid in delta.threads_resolved:
            t = next((t for t in b.threads if t.id == tid), None)
            if t:
                t.status = "resolved"
                t.resolved_chapter = delta.chapter

        if delta.timeline_summary:
            self.timeline.append(TimelineEvent(
                chapter=delta.chapter,
                summary=delta.timeline_summary,
                time_marker=delta.time_marker,
            ))

    def record_chapter(self, rec: ChapterRecord, synopsis_line: str, settings_cap: int) -> None:
        rec.compute_fingerprint()
        self.chapters[rec.number] = rec
        line = (synopsis_line or rec.synopsis_line or "").strip()
        if len(line) > settings_cap:
            line = line[: settings_cap - 1].rstrip() + "…"
        rec.synopsis_line = line
        # Synopsis is a 1-based-by-chapter list. A number < 1 (degenerate plan from
        # a non-conforming model) would index self.synopsis[-1] and clobber the last
        # chapter — guard it (the record itself is still stored above). Also reject
        # an implausibly large number so a single off-spec value (e.g. 100000) can't
        # pad the list to that size and blow up state.json.
        ceiling = max(len(self.bible.outline), len(self.synopsis)) + 1
        if rec.number < 1 or rec.number > ceiling:
            return
        while len(self.synopsis) < rec.number:
            self.synopsis.append("")
        self.synopsis[rec.number - 1] = line

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def state_to_dict(self) -> Dict[str, Any]:
        """The evolving state (not the bible — that is saved separately)."""
        return {
            "timeline": [e.to_dict() for e in self.timeline],
            "synopsis": list(self.synopsis),
        }

    def load_state(self, d: Dict[str, Any]) -> None:
        # Filter Nones: TimelineEvent.from_dict returns None for a non-dict entry
        # (tolerant loader), so a malformed saved-state timeline can't smuggle a
        # None into self.timeline and crash a later e.to_dict().
        raw = d.get("timeline", [])
        raw = raw if isinstance(raw, list) else []
        self.timeline = [e for e in (TimelineEvent.from_dict(x) for x in raw) if e is not None]
        syn = d.get("synopsis", [])
        self.synopsis = list(syn) if isinstance(syn, list) else []


def _slug(name) -> str:
    if not isinstance(name, str):
        name = str(name)
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")[:32] or "x"
