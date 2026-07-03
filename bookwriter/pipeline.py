"""Orchestration: tie the stages together into a resumable book run.

    plan_book  ->  for each chapter:  write -> extract -> apply delta -> [check]
                                       (save after each so the run is resumable)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from .config import Settings
from .costs import CostLedger
from .graph import StoryGraph
from .llm import LLM
from .models import Bible
from .planner import plan_book
from .writer import write_chapter
from .extractor import extract_delta
from .checker import check_chapter
from .store import BookStore

logger = logging.getLogger(__name__)

Progress = Callable[[str], None]
EventSink = Callable[[Dict[str, Any]], None]


def _noop(_: str) -> None:
    pass


def _noop_event(_: Dict[str, Any]) -> None:
    pass


class BookPipeline:
    """Orchestrator. Beyond the human-readable ``progress`` string callback, it
    emits structured ``on_event`` dicts (plan_done, chapter_start, delta,
    chapter_done, manuscript_done); the plan_done / chapter_done / manuscript_done
    events each carry a ``cost`` snapshot field. This is the
    backbone the HTTP SSE stream and
    the MCP server both consume to drive live UIs and agents.
    """

    def __init__(self, llm: LLM, settings: Settings,
                 progress: Optional[Progress] = None,
                 on_event: Optional[EventSink] = None,
                 stream_prose: bool = False,
                 image_provider: Any = None):
        self.llm = llm
        self.settings = settings
        self.ledger = CostLedger()
        self.store = BookStore(settings.project_dir)
        self.graph: Optional[StoryGraph] = None
        self.progress: Progress = progress or _noop
        self.on_event: EventSink = on_event or _noop_event
        self.stream_prose = stream_prose
        # Optional image backend (see images.py). Only used when the book opted
        # into per-chapter illustrations via settings.chapter_images.
        self.image_provider = image_provider

    def _emit(self, **event: Any) -> None:
        self.on_event(event)

    def _maybe_make_image(self, plan) -> bool:
        """Generate + persist one chapter illustration. Best-effort: any failure
        is logged and the chapter is kept without an image (never blocks prose)."""
        if not self.settings.chapter_images or self.image_provider is None:
            return False
        assert self.graph is not None
        try:
            from .images import build_chapter_prompt
            prompt = build_chapter_prompt(self.graph.bible, plan)
            self.progress(f"Chapter {plan.number}: illustrating...")
            data, ext = self.image_provider.generate(prompt)
            self.store.save_image(plan.number, data, ext)
            return True
        except Exception as e:  # noqa: BLE001 - image is optional, keep writing
            self.progress(f"  [!] chapter image skipped: {e}")
            return False

    def _cost_snapshot(self) -> Dict[str, Any]:
        return {
            "total_cost": round(self.ledger.total_cost(), 6),
            "words": self.ledger.words_written,
            "by_stage": {k: round(v, 6) for k, v in self.ledger.by_stage().items()},
            "tokens": self.ledger.totals(),
            "cache_savings": round(self.ledger.cache_savings(), 6),
        }

    # ------------------------------------------------------------------
    def plan(self, *, premise: str, chapters: Optional[int] = None,
             words_per_chapter: int = 2000, title: Optional[str] = None,
             genre: Optional[str] = None, extra_guidance: str = "") -> Bible:
        self.progress("Planning book (bible + outline)...")
        bible = plan_book(
            self.llm, self.settings, self.ledger,
            premise=premise, chapters=chapters, words_per_chapter=words_per_chapter,
            title=title, genre=genre, extra_guidance=extra_guidance,
        )
        self.graph = StoryGraph(bible)
        if os.path.exists(self.store.book_path):
            # Re-planning an existing project: purge the previous outline's
            # chapter artifacts BEFORE saving the new plan — load_graph reloads
            # any chapters/NN.json whose number matches the new outline, and the
            # resume branch would then silently stitch the old book's prose into
            # the new manuscript. Fresh dirs have no book.json and are untouched.
            self.store.clear_chapters()
        self.store.save_graph(self.graph)
        self.progress(f"Planned '{bible.title}': {len(bible.outline)} chapters, "
                      f"{len(bible.characters)} characters.")
        self._emit(type="plan_done", title=bible.title, chapters=len(bible.outline),
                   characters=len(bible.characters), bible=bible.to_dict(),
                   cost=self._cost_snapshot())
        return bible

    # ------------------------------------------------------------------
    def load(self) -> bool:
        graph = self.store.load_graph()
        if graph is None:
            return False
        self.graph = graph
        return True

    # ------------------------------------------------------------------
    def _repair_missing_delta(self, plan, number: int) -> None:
        """Backfill the continuity delta for a written chapter whose rolling-
        synopsis slot is missing/empty (a crash during extract/check persisted
        the prose but not the graph update). Best-effort, mirroring the same
        degradation policy as the inline extract stage."""
        assert self.graph is not None
        slot = self.graph.synopsis[number - 1] if number <= len(self.graph.synopsis) else ""
        if isinstance(slot, str) and slot.strip():
            return
        rec = self.graph.chapters[number]
        self.progress(f"Chapter {number}: repairing missing continuity state...")
        try:
            delta = extract_delta(self.llm, self.settings, self.ledger, self.graph, plan, rec)
            self.graph.apply_delta(delta)
            self.graph.record_chapter(rec, delta.synopsis_line, self.settings.synopsis_line_chars)
            self.store.save_chapter(rec)
            self.store.save_graph(self.graph)
        except Exception as e:  # noqa: BLE001 - repair is best-effort, like extract itself
            logger.exception("continuity repair failed for chapter %s", number)
            self.progress(f"  [!] continuity repair failed: {e} (chapter kept, continuing)")

    # ------------------------------------------------------------------
    def write_all(self, *, resume: bool = True, only: Optional[List[int]] = None) -> CostLedger:
        if self.graph is None and not self.load():
            raise RuntimeError("No plan found. Run plan() first.")
        assert self.graph is not None
        targets = only or [p.number for p in self.graph.bible.outline]

        for number in targets:
            plan = self.graph.bible.plan(number)
            if plan is None:
                continue
            if resume and not only and self.store.has_chapter(number) \
                    and number in self.graph.chapters:
                # A crash between saving the prose and saving the graph (the
                # extract/check window) leaves the chapter written but its
                # continuity delta lost — an empty rolling-synopsis slot that
                # every later chapter would be written against. Repair it by
                # re-running the cheap extract stage before skipping.
                self._repair_missing_delta(plan, number)
                self.progress(f"Chapter {number}: already written - skipping.")
                continue

            self.progress(f"Chapter {number}: writing '{plan.title}'...")
            self._emit(type="chapter_start", number=number, title=plan.title,
                       act=plan.act, word_target=plan.word_target)
            delta_cb = None
            if self.stream_prose:
                delta_cb = lambda t, n=number: self._emit(type="delta", number=n, text=t)
            rec = write_chapter(self.llm, self.settings, self.ledger, self.graph, plan,
                                on_delta=delta_cb)
            # Capture the writer's stop reason NOW — the extract/check calls
            # below reuse the same client and would overwrite it. Backends that
            # don't track it (subscription CLIs) simply never flag truncation.
            truncated = getattr(self.llm, "last_stop_reason", None) == "max_tokens"
            # Persist the expensive prose IMMEDIATELY, before the cheap extract/
            # check stages run. A malformed-JSON failure in those mechanical
            # stages must never discard a chapter we already generated and paid
            # for — that would also break resumability at the exact failure point.
            self.store.save_chapter(rec)
            self.progress(f"Chapter {number}: {rec.word_count} words. Extracting state...")

            flags: List[str] = []
            if truncated:
                flags.append("[high] chapter truncated at max_tokens — raise "
                             "Settings.max_tokens_write or lower the chapter word target")
            synopsis = ""
            try:
                delta = extract_delta(self.llm, self.settings, self.ledger, self.graph, plan, rec)
                self.graph.apply_delta(delta)
                synopsis = delta.synopsis_line
                flags += list(delta.continuity_flags)
                if self.settings.run_continuity_check:
                    issues = check_chapter(self.llm, self.settings, self.ledger, self.graph, plan, rec)
                    flags += [f"[{i.get('severity', '?')}] {i.get('description', '')}" for i in issues]
            except Exception as e:  # noqa: BLE001 - extract/check are mechanical; degrade, don't abort
                logger.exception("state extraction/continuity failed for chapter %s", number)
                flags.append(f"[warning] state extraction skipped ({e}); chapter saved, continuity not updated")
                self.progress(f"  [!] state extraction failed: {e} (chapter kept, continuing)")

            if flags:
                self.progress(f"  [!] continuity flags: {len(flags)}")
                for fl in flags:
                    self.progress(f"    - {fl}")

            # Record the chapter in the graph (degraded synopsis is fine) so a
            # resumed run skips it, then persist graph + a cost snapshot per
            # chapter (so an interrupted run keeps accurate cost accounting).
            self.graph.record_chapter(rec, synopsis, self.settings.synopsis_line_chars)
            # record_chapter just computed the fingerprint and set the capped
            # synopsis line on the record — re-save (atomic, cheap) so
            # chapters/NN.json carries them; the API/MCP layers read both
            # straight from disk after a process restart.
            self.store.save_chapter(rec)
            self.store.save_graph(self.graph)
            self.store.save_cost(self.ledger.report(), _ledger_dict(self.ledger))

            has_image = self._maybe_make_image(plan)

            self._emit(type="chapter_done", number=number, title=rec.title,
                       words=rec.word_count, text=rec.text,
                       synopsis=rec.synopsis_line, flags=flags, image=has_image,
                       fingerprint=rec.fingerprint, cost=self._cost_snapshot())

        manuscript = self.store.assemble_manuscript(self.graph)
        report = self.ledger.report()
        self.store.save_cost(report, _ledger_dict(self.ledger))
        self.progress(f"Manuscript assembled ({len(manuscript.split())} words).")
        self._emit(type="manuscript_done", words=len(manuscript.split()),
                   cost=self._cost_snapshot())
        return self.ledger


def _ledger_dict(ledger: CostLedger) -> dict:
    return {
        "total_cost": round(ledger.total_cost(), 6),
        "words": ledger.words_written,
        "by_stage": {k: round(v, 6) for k, v in ledger.by_stage().items()},
        "by_model": {k: round(v, 6) for k, v in ledger.by_model().items()},
        "tokens": ledger.totals(),
        "cache_savings": round(ledger.cache_savings(), 6),
        "entries": [vars(u) for u in ledger.entries],
    }
