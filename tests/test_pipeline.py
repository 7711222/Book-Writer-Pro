import json
import os
import tempfile
import unittest

from bookwriter.config import Settings
from bookwriter.mock import MockLLM
from bookwriter.pipeline import BookPipeline
from bookwriter.planner import build_bible


class _EmptyProseLLM(MockLLM):
    """Simulates a refusal / thinking-exhausted budget: no prose comes back."""

    def complete_text(self, **kw):
        super().complete_text(**kw)
        return ""


class _TruncatingLLM(MockLLM):
    """Prose comes back, but the response stopped at max_tokens."""

    def complete_text(self, **kw):
        out = super().complete_text(**kw)
        self.last_stop_reason = "max_tokens"
        return out


class _BadIssuesLLM(MockLLM):
    """The checker emits an off-spec payload: a bare string among the issues."""

    def complete_json(self, **kw):
        data = super().complete_json(**kw)
        if kw.get("stage") == "check":
            return {"issues": ["bare string", {"severity": "low", "description": "ok"}]}
        return data


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestPipeline(unittest.TestCase):
    def _settings(self, tmp, **kw):
        return Settings(project_dir=tmp, **kw).with_profile("balanced")

    def test_end_to_end_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="a test premise", chapters=3, words_per_chapter=200)
            self.assertEqual(len(pipe.graph.bible.outline), 3)
            pipe.write_all()

            # all chapters written and persisted
            self.assertEqual(len(pipe.graph.chapters), 3)
            self.assertTrue(os.path.exists(os.path.join(tmp, "manuscript.md")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "chapters", "01.md")))

            # cost was tracked and prose was produced
            self.assertGreater(pipe.ledger.total_cost(), 0)
            self.assertGreater(pipe.ledger.words_written, 0)

            # the extractor advanced the graph (hero gained knowledge)
            hero = pipe.graph.bible.character("hero")
            self.assertTrue(hero.knowledge)

            # rolling synopsis has one line per chapter
            self.assertEqual(len([s for s in pipe.graph.synopsis if s]), 3)

    def test_words_per_chapter_is_authoritative(self):
        # The Short/Medium/Long picker must control word_target on every chapter,
        # overriding whatever the planner model proposes.
        with tempfile.TemporaryDirectory() as tmp:
            short = BookPipeline(MockLLM(), self._settings(tmp)).plan(
                premise="x", chapters=3, words_per_chapter=900)
            self.assertTrue(all(p.word_target == 900 for p in short.outline))
        with tempfile.TemporaryDirectory() as tmp:
            long = BookPipeline(MockLLM(), self._settings(tmp)).plan(
                premise="x", chapters=3, words_per_chapter=3500)
            self.assertTrue(all(p.word_target == 3500 for p in long.outline))

    def test_prompt_cache_engages_after_first_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=150)
            pipe.write_all()
            t = pipe.ledger.totals()
            # bible prefix is identical across chapters -> cache reads accrue
            self.assertGreater(t["cache_read"], 0)
            self.assertGreater(t["cache_write"], 0)
            self.assertGreater(pipe.ledger.cache_savings(), 0)

    def test_no_cache_uses_slice_and_no_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._settings(tmp)
            s.use_cache = False
            pipe = BookPipeline(MockLLM(), s)
            pipe.plan(premise="t", chapters=2, words_per_chapter=120)
            pipe.write_all()
            self.assertEqual(pipe.ledger.totals()["cache_read"], 0)
            self.assertEqual(pipe.ledger.totals()["cache_write"], 0)

    def test_resume_skips_written_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=120)
            pipe.write_all()

            # fresh pipeline, reload from disk, write again -> nothing re-written
            pipe2 = BookPipeline(MockLLM(), self._settings(tmp))
            self.assertTrue(pipe2.load())
            pipe2.write_all(resume=True)
            write_entries = [u for u in pipe2.ledger.entries if u.stage == "write"]
            self.assertEqual(write_entries, [])
            # manuscript still assembled from loaded chapters
            self.assertTrue(os.path.exists(os.path.join(tmp, "manuscript.md")))

    def test_only_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=4, words_per_chapter=120)
            pipe.write_all(only=[2])
            writes = [u for u in pipe.ledger.entries if u.stage == "write"]
            self.assertEqual(len(writes), 1)
            self.assertIn(2, pipe.graph.chapters)

    def test_restart_rewrites_all(self):
        # --restart / resume=False must regenerate every chapter, written or not.
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=120)
            pipe.write_all()

            pipe2 = BookPipeline(MockLLM(), self._settings(tmp))
            self.assertTrue(pipe2.load())
            pipe2.write_all(resume=False)
            writes = [u for u in pipe2.ledger.entries if u.stage == "write"]
            self.assertEqual(len(writes), 3)

    def test_partial_resume_writes_only_missing_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=120)
            pipe.write_all(only=[1])
            fp1 = _read_json(os.path.join(tmp, "chapters", "01.json"))["fingerprint"]

            pipe2 = BookPipeline(MockLLM(), self._settings(tmp))
            self.assertTrue(pipe2.load())
            # If the skip logic were broken, ch1 would be rewritten under this
            # title and its fingerprint would change.
            pipe2.graph.bible.plan(1).title = "Rewritten Title"
            pipe2.write_all(resume=True)
            writes = [u for u in pipe2.ledger.entries if u.stage == "write"]
            self.assertEqual(len(writes), 2)          # chapters 2 and 3 only
            self.assertEqual(sorted(pipe2.graph.chapters), [1, 2, 3])
            self.assertEqual(
                _read_json(os.path.join(tmp, "chapters", "01.json"))["fingerprint"], fp1)

    def test_only_rewrites_written_chapter(self):
        # only=[N] deliberately rewrites even an already-written chapter — the
        # `not only` in the resume condition is intentional, not a bug.
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=120)
            pipe.write_all()
            fp2 = pipe.graph.chapters[2].fingerprint
            self.assertTrue(fp2)

            pipe.graph.bible.plan(2).title = "A Different Chapter Two"
            pipe.write_all(only=[2])
            writes = [u for u in pipe.ledger.entries if u.stage == "write"]
            self.assertEqual(len(writes), 4)          # 3 initial + 1 rewrite
            self.assertEqual(pipe.graph.chapters[2].title, "A Different Chapter Two")
            self.assertNotEqual(pipe.graph.chapters[2].fingerprint, fp2)

    def test_replan_clears_stale_chapters(self):
        # Re-planning the same project dir must purge the previous book's
        # chapters, or a later resume silently stitches old prose into the new
        # manuscript (the resume check is file-existence only).
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="the lighthouse keeper's secret", chapters=3,
                      words_per_chapter=120)
            pipe.write_all()
            self.assertTrue(os.path.exists(os.path.join(tmp, "chapters", "01.json")))

            pipe2 = BookPipeline(MockLLM(), self._settings(tmp))
            pipe2.plan(premise="a heist on the first martian colony", chapters=3,
                       words_per_chapter=120)
            for leftover in ("chapters/01.json", "chapters/01.md", "manuscript.md",
                             "cost.json"):
                self.assertFalse(os.path.exists(os.path.join(tmp, *leftover.split("/"))),
                                 leftover)

            # A fresh process resuming the new plan must WRITE, not skip.
            pipe3 = BookPipeline(MockLLM(), self._settings(tmp))
            self.assertTrue(pipe3.load())
            pipe3.write_all(resume=True)
            writes = [u for u in pipe3.ledger.entries if u.stage == "write"]
            self.assertEqual(len(writes), 3)

    def test_replan_shorter_outline_leaves_no_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=120)
            pipe.write_all()
            self.assertTrue(os.path.exists(os.path.join(tmp, "chapters", "03.json")))

            pipe2 = BookPipeline(MockLLM(), self._settings(tmp))
            pipe2.plan(premise="t", chapters=2, words_per_chapter=120)
            self.assertFalse(os.path.exists(os.path.join(tmp, "chapters", "03.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "chapters", "03.md")))

    def test_chapter_json_persists_fingerprint_and_synopsis(self):
        # record_chapter mutates the record AFTER the first save; the pipeline
        # must re-save so chapters/NN.json carries fingerprint + synopsis_line
        # (the API/MCP layers read them straight from disk after a restart).
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=2, words_per_chapter=120)
            pipe.write_all()
            for n in (1, 2):
                d = _read_json(os.path.join(tmp, "chapters", f"{n:02d}.json"))
                self.assertTrue(d.get("fingerprint"), f"chapter {n} fingerprint")
                self.assertTrue(d.get("synopsis_line"), f"chapter {n} synopsis")
                self.assertEqual(d["fingerprint"], pipe.graph.chapters[n].fingerprint)

    def test_resume_repairs_missing_delta(self):
        # A crash between saving the prose and saving the graph loses the
        # chapter's continuity delta; the resume-skip path must backfill it by
        # re-running the cheap extract stage.
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(MockLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=3, words_per_chapter=120)
            pipe.write_all()

            pipe2 = BookPipeline(MockLLM(), self._settings(tmp))
            self.assertTrue(pipe2.load())
            pipe2.graph.synopsis[1] = ""    # simulate the crash during ch2 extract
            pipe2.write_all(resume=True)
            writes = [u for u in pipe2.ledger.entries if u.stage == "write"]
            extracts = [u for u in pipe2.ledger.entries if u.stage == "extract"]
            self.assertEqual(writes, [])
            self.assertEqual(len(extracts), 1)          # only the repaired chapter
            self.assertTrue(pipe2.graph.synopsis[1].strip())
            # and the repair was persisted
            state = _read_json(os.path.join(tmp, "state.json"))
            self.assertTrue(state["synopsis"][1].strip())

    def test_empty_prose_raises(self):
        # A 0-word chapter must never be saved and marked written — that is a
        # permanent silent hole in the manuscript.
        with tempfile.TemporaryDirectory() as tmp:
            pipe = BookPipeline(_EmptyProseLLM(), self._settings(tmp))
            pipe.plan(premise="t", chapters=1, words_per_chapter=100)
            with self.assertRaises(ValueError):
                pipe.write_all()
            self.assertFalse(os.path.exists(os.path.join(tmp, "chapters", "01.json")))

    def test_truncated_write_adds_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            pipe = BookPipeline(_TruncatingLLM(), self._settings(tmp),
                                on_event=events.append)
            pipe.plan(premise="t", chapters=1, words_per_chapter=100)
            pipe.write_all()
            done = next(e for e in events if e["type"] == "chapter_done")
            self.assertTrue(any("truncated at max_tokens" in f for f in done["flags"]),
                            done["flags"])

    def test_checker_drops_non_dict_issues(self):
        # An off-spec (string) issue entry must be dropped, not crash the flags
        # formatting and get misreported as a failed extraction.
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            pipe = BookPipeline(_BadIssuesLLM(), self._settings(tmp),
                                on_event=events.append)
            pipe.plan(premise="t", chapters=1, words_per_chapter=100)
            pipe.write_all()
            done = next(e for e in events if e["type"] == "chapter_done")
            self.assertIn("[low] ok", done["flags"])
            self.assertFalse(any("state extraction skipped" in f for f in done["flags"]),
                             done["flags"])


class TestPlannerValidation(unittest.TestCase):
    def test_empty_outline_raises(self):
        # complete_json returns {} for an empty response; the plan must fail
        # loudly instead of persisting a 0-chapter book.
        with self.assertRaises(ValueError):
            build_bible({})

    def test_outline_renumbered_sequentially(self):
        # The json_object provider path can emit duplicate or missing chapter
        # numbers; order after sorting is authoritative.
        b = build_bible({"outline": [
            {"number": 2, "title": "dup a"},
            {"number": 2, "title": "dup b"},
            {"title": "no number"},          # coerced to 0, sorts first
        ], "target_chapters": 99})
        self.assertEqual([p.number for p in b.outline], [1, 2, 3])
        self.assertEqual(b.outline[0].title, "no number")
        self.assertEqual(b.target_chapters, 3)


if __name__ == "__main__":
    unittest.main()
