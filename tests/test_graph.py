import unittest

from bookwriter.graph import StoryGraph, StateDelta
from bookwriter.models import (
    Bible, Character, Item, Location, PlotThread, ChapterPlan, ChapterRecord,
)


def _bible():
    return Bible(
        title="T",
        characters=[Character(id="hero", name="Hero", role="protagonist")],
        locations=[Location(id="home", name="Home")],
        threads=[PlotThread(id="q", name="Quest", status="open")],
        outline=[ChapterPlan(number=1, title="One", character_ids=["hero"],
                             location_ids=["home"])],
        target_chapters=1,
    )


class TestGraph(unittest.TestCase):
    def test_apply_delta_updates_state(self):
        g = StoryGraph(_bible())
        delta = StateDelta.from_dict(1, {
            "synopsis_line": "things happen",
            "character_updates": [{"id": "hero", "status": "wounded",
                                   "knowledge_added": ["the secret"],
                                   "relationship_updates": {"villain": "nemesis"}}],
            "new_characters": [{"id": "villain", "name": "Villain", "role": "antagonist"}],
            "threads_opened": [{"id": "betrayal", "name": "Betrayal"}],
            "threads_resolved": ["q"],
            "timeline_summary": "day one",
        })
        g.apply_delta(delta)
        hero = g.bible.character("hero")
        self.assertEqual(hero.status, "wounded")
        self.assertIn("the secret", hero.knowledge)
        self.assertEqual(hero.relationships["villain"], "nemesis")
        self.assertIsNotNone(g.bible.character("villain"))
        self.assertEqual(g.bible.character("villain").first_chapter, 1)
        q = next(t for t in g.bible.threads if t.id == "q")
        self.assertEqual(q.status, "resolved")
        self.assertTrue(any(t.id == "betrayal" for t in g.bible.threads))
        self.assertEqual(len(g.timeline), 1)

    def test_relevant_slice_is_subset(self):
        b = _bible()
        b.characters.append(Character(id="extra", name="Extra"))
        g = StoryGraph(b)
        sl = g.relevant_slice(b.plan(1))
        self.assertIn("Hero", sl)
        self.assertNotIn("Extra", sl)      # not in this chapter
        self.assertIn("Quest", sl)         # open threads always shown

    def test_rolling_synopsis_capped_and_ordered(self):
        g = StoryGraph(_bible())
        rec = ChapterRecord(number=1, title="One", text="word " * 50)
        long_line = "x" * 500
        g.record_chapter(rec, long_line, settings_cap=240)
        self.assertLessEqual(len(g.synopsis[0]), 240)
        self.assertTrue(g.synopsis[0].endswith("…"))

    def test_prev_tail(self):
        g = StoryGraph(_bible())
        g.chapters[1] = ChapterRecord(number=1, title="a", text="one two three four five")
        tail = g.prev_tail(2, words=2)
        self.assertEqual(tail, "four five")
        self.assertEqual(g.prev_tail(1, words=2), "")  # no chapter 0

    def test_from_dict_coerces_off_spec_scalars(self):
        # The json_object provider path can emit numbers/nulls/lists for the
        # string scalars; a truthy non-string used to crash record_chapter's
        # .strip() (outside the pipeline's extract try/except).
        d = StateDelta.from_dict(1, {"synopsis_line": 42, "timeline_summary": None,
                                     "time_marker": ["dawn"], "continuity_flags": "oops"})
        self.assertEqual(d.synopsis_line, "")
        self.assertEqual(d.timeline_summary, "")
        self.assertEqual(d.time_marker, "")
        self.assertEqual(d.continuity_flags, [])
        # record_chapter survives the coerced line
        g = StoryGraph(_bible())
        rec = ChapterRecord(number=1, title="One", text="t")
        g.record_chapter(rec, d.synopsis_line, settings_cap=240)
        self.assertEqual(g.synopsis, [""])

    def test_relevant_slice_includes_items(self):
        b = _bible()
        b.items.append(Item(id="keepsake", name="The Keepsake",
                            description="a small thing"))
        g = StoryGraph(b)
        sl = g.relevant_slice(b.plan(1))
        self.assertIn("The Keepsake", sl)
        self.assertIn("## Items", sl)

    def test_dynamic_state_shows_midbook_discoveries(self):
        # Entities discovered after planning are absent from the frozen prefix
        # AND from plan.character_ids (the outline predates the discovery), so
        # dynamic_state must surface them or the writer never learns they exist.
        g = StoryGraph(_bible())
        g.apply_delta(StateDelta.from_dict(3, {
            "new_characters": [{"id": "drifter", "name": "The Drifter"}],
            "new_items": [{"id": "coin", "name": "The Coin"}],
        }))
        dyn = g.dynamic_state(g.bible.plan(1))   # plan lists only "hero"
        self.assertIn("The Drifter", dyn)
        self.assertIn("The Coin", dyn)
        self.assertIn("Items discovered mid-book", dyn)


if __name__ == "__main__":
    unittest.main()
