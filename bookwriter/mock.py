"""MockLLM — a deterministic, offline implementation of the LLM protocol.

Generates schema-valid bibles, prose, and deltas with no network or API key.
Used by the test suite and the ``--mock`` CLI flag for end-to-end smoke runs and
for demonstrating the cost-accounting / caching bookkeeping without spending
tokens. Token counts are *simulated* (proportional to text length) so the cost
report is representative, not exact.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Set

from .config import StageModel
from .costs import CostLedger, Usage

_WORD = "the quick story unfolds as our hero confronts another turn of fate and presses onward "


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class MockLLM:
    def __init__(self) -> None:
        self._seen_prefixes: Set[int] = set()  # simulate cross-call prompt cache
        # Mirrors the API backends' truncation telemetry: "end_turn" after every
        # call (mock output is never truncated). Tests override it to simulate
        # a max_tokens cut-off.
        self.last_stop_reason: Optional[str] = None

    # ------------------------------------------------------------------
    def complete_json(self, *, stage, model, system, user, schema, max_tokens,
                       ledger, cached=None, use_cache=True, cache_ttl="1h") -> Dict[str, Any]:
        props = schema.get("properties", {})
        if "outline" in props:
            data = self._plan(user)
        elif "issues" in props:
            data = {"issues": []}
        elif "blurb_variants" in props or "a_plus_modules" in props:
            data = self._marketing(user)
        elif "keywords" in props and "categories" in props:
            data = self._kdp(user)
        else:
            data = self._delta(user)
        self._record(ledger, stage, model, system, user, cached, use_cache, cache_ttl,
                     out_tokens=len(str(data)) // 4 + 20)
        self.last_stop_reason = "end_turn"
        return data

    # ------------------------------------------------------------------
    def complete_text(self, *, stage, model, system, user, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h",
                      on_delta=None) -> str:
        m = re.search(r"approximately (\d+) words", user)
        target = min(int(m.group(1)) if m else 300, 600)
        title = self._chapter_title(user)
        prose = self._mock_prose(title, target)
        if on_delta is not None:
            # Emit in word-ish chunks so the UI's live-typing path is exercised offline.
            # BOOKWRITER_MOCK_DELAY (seconds, default 0) paces the stream so the
            # web UI's live-writing animation is visible in demo mode; 0 keeps the
            # test suite instant.
            try:
                delay = float(os.environ.get("BOOKWRITER_MOCK_DELAY", "0") or 0)
            except ValueError:
                delay = 0.0
            words = prose.split(" ")
            for i in range(0, len(words), 6):
                on_delta(" ".join(words[i:i + 6]) + " ")
                if delay:
                    time.sleep(delay)
        self._record(ledger, stage, model, system, user, cached, use_cache, cache_ttl,
                     out_tokens=target * 4 // 3)
        self.last_stop_reason = "end_turn"
        return prose

    @staticmethod
    def _mock_prose(title: str, target: int) -> str:
        """More natural placeholder narrative than lorem-ipsum, for demos."""
        sentences = [
            "The morning came grey and slow over the rooftops.",
            "She had not slept, and the silence pressed against the windows like a held breath.",
            "Somewhere below, a door opened and did not close.",
            "He weighed the choice the way one weighs a stone before throwing it.",
            "Nothing about the room had changed, and that was the most frightening part.",
            "They walked without speaking, the way old friends sometimes must.",
            "A promise made in the dark has a different weight by daylight.",
            "The town remembered what the people had tried to forget.",
            "Every answer he found only sharpened the question beneath it.",
            "And so the day turned, indifferent to them both.",
        ]
        out: List[str] = [f"{title.strip().capitalize()}."]
        i = 0
        while len(" ".join(out).split()) < target:
            out.append(sentences[i % len(sentences)])
            i += 1
        return " ".join(out)

    # ------------------------------------------------------------------
    # Light parsing of the planner prompt so the offline demo reflects the
    # user's actual premise/genre/title instead of canned boilerplate.
    _STOP = {
        "the", "and", "with", "that", "this", "from", "into", "their", "there",
        "which", "while", "about", "after", "before", "every", "only", "still",
        "discovers", "must", "never", "what", "when", "they", "them", "have",
        "been", "where", "would", "could", "should", "other", "than", "then",
    }

    @classmethod
    def _parse(cls, user: str):
        def grab(pat, default=""):
            m = re.search(pat, user)
            return m.group(1).strip() if m else default
        pm = re.search(
            r"PREMISE:\s*\n?(.*?)(?:\n\s*(?:Target length|Working title|Genre|Additional)|\Z)",
            user, re.DOTALL)
        premise = (pm.group(1).strip() if pm and pm.group(1).strip()
                   else "A story waiting to be told.")
        genre = grab(r"Genre:\s*(.+)", "literary fiction")
        title = grab(r"Working title:\s*(.+)", "")
        return premise.strip(), genre.strip() or "literary fiction", title.strip()

    @classmethod
    def _keywords(cls, premise: str):
        words = [w for w in re.findall(r"[A-Za-z]{5,}", premise)
                 if w.lower() not in cls._STOP]
        seen, out = set(), []
        for w in words:
            lw = w.lower()
            if lw not in seen:
                seen.add(lw)
                out.append(w)
        return out

    def _plan(self, user: str) -> Dict[str, Any]:
        m = re.search(r"exactly (\d+) chapters", user)
        n = int(m.group(1)) if m else 3
        premise, genre, title = self._parse(user)
        kw = self._keywords(premise)
        focus = kw[0].capitalize() if kw else "Threshold"

        if not title:
            title = f"The {focus}"
        logline = premise if len(premise) <= 140 else premise[:139].rstrip() + "…"

        # A full ensemble so the continuity graph reads as a real, dense web of
        # relationships rather than a sparse star. The "hero"/"rival" ids are kept
        # for test compatibility; the rest fan out around them with cross-links so
        # the studio cast panel and the relationship map both look substantial.
        cast = [
            {"id": "hero", "name": "Wren Calloway", "role": "protagonist",
             "traits": ["watchful", "stubborn"], "arc": "denial to hard-won acceptance",
             "relationships": [{"with": "mentor", "relation": "former student of"},
                               {"with": "rival", "relation": "estranged from"},
                               {"with": "ally", "relation": "trusts, warily"},
                               {"with": "sibling", "relation": "protects"},
                               {"with": "keeper", "relation": "seeks answers from"}]},
            {"id": "ally", "name": "Tomas Reed", "role": "supporting",
             "traits": ["loyal", "wry"], "arc": "bystander to believer",
             "relationships": [{"with": "hero", "relation": "oldest friend"},
                               {"with": "sibling", "relation": "quietly in love with"},
                               {"with": "rival", "relation": "wary of"}]},
            {"id": "rival", "name": "Iris Halloran", "role": "antagonist",
             "traits": ["composed", "ruthless"], "arc": "certainty to doubt",
             "relationships": [{"with": "hero", "relation": "shared a past, now opposed"},
                               {"with": "keeper", "relation": "in league with"},
                               {"with": "mentor", "relation": "resents"}]},
            {"id": "mentor", "name": "Old Sabine", "role": "supporting",
             "traits": ["knowing", "guarded"], "arc": "keeper of the secret",
             "relationships": [{"with": "hero", "relation": "mentor and warning"},
                               {"with": "keeper", "relation": "old rival"}]},
            {"id": "sibling", "name": "Liam Calloway", "role": "supporting",
             "traits": ["impulsive", "tender"], "arc": "reckless to steadfast",
             "relationships": [{"with": "hero", "relation": "younger sibling"},
                               {"with": "ally", "relation": "trusts completely"},
                               {"with": "stranger", "relation": "drawn to"}]},
            {"id": "keeper", "name": "The Custodian", "role": "antagonist",
             "traits": ["patient", "implacable"], "arc": "shadow that lengthens",
             "relationships": [{"with": "rival", "relation": "true master of"},
                               {"with": "mentor", "relation": "betrayed long ago"}]},
            {"id": "stranger", "name": "Marigold Vey", "role": "supporting",
             "traits": ["enigmatic", "kind"], "arc": "outsider to linchpin",
             "relationships": [{"with": "hero", "relation": "unexpected ally"},
                               {"with": "sibling", "relation": "kindred spirit"}]},
        ]
        loc_a = f"the {focus.lower()}" if kw else "the old house"
        locations = [
            {"id": "home", "name": loc_a.title(), "description": "where it begins and is decided"},
            {"id": "edge", "name": "The Far Edge", "description": "the place no one returns from unchanged"},
            {"id": "market", "name": "The Lantern Market", "description": "where rumours and bargains change hands after dark"},
            {"id": "archive", "name": "The Old Archive", "description": "where the truth was buried in plain ink"},
        ]
        items = [{"id": "keepsake", "name": "The Keepsake",
                  "description": "a small thing that holds the whole story", "significance": "central"}]
        threads = [
            {"id": "central", "name": "The truth Wren is chasing",
             "description": "the question the book is really about"},
            {"id": "bond", "name": "Wren and Iris",
             "description": "an old bond strained to breaking"},
        ]

        titles = ["The First Sign", "What the Water Knew", "A Door Left Open",
                  "The Weight of It", "Old Promises", "The Turn", "Nothing Stays Buried",
                  "Closer to the Edge", "The Cost", "What Remained", "The Reckoning",
                  "And After"]
        # Rotate the supporting cast through the chapters so the demo outline
        # exercises the whole ensemble (and the per-chapter cast chips look full).
        scene_cast = [
            ["hero", "ally", "sibling"],
            ["hero", "rival", "keeper"],
            ["hero", "mentor", "stranger"],
            ["hero", "ally", "stranger"],
            ["hero", "sibling", "rival"],
            ["hero", "mentor", "keeper"],
        ]
        scene_loc = ["home", "market", "edge", "archive"]
        outline = []
        for i in range(1, n + 1):
            act = 1 if i <= max(1, n // 4) else (3 if i > n - max(1, n // 4) else 2)
            ct = titles[(i - 1) % len(titles)]
            cast_ids = scene_cast[(i - 1) % len(scene_cast)]
            if i == n:
                cast_ids = ["hero", "rival", "mentor", "keeper"]
            outline.append({
                "number": i,
                "title": ct,
                "act": act,
                "purpose": ("hook the reader and set the stakes" if i == 1
                            else "resolve the threads and land the ending" if i == n
                            else "raise the pressure and deepen the cost"),
                "pov_character": "hero",
                "location_ids": [scene_loc[(i - 1) % len(scene_loc)]],
                "character_ids": cast_ids,
                "beats": [f"Wren confronts the consequence of chapter {i - 1 or 'the opening'}",
                          "a choice narrows the path", "the hook into what comes next"],
                "tension": "what Wren is willing to lose",
                "forward_hook": "" if i == n else "a new pressure tightens",
                "word_target": 300,
            })

        return {
            "title": title,
            "logline": logline,
            "premise": premise,
            "genre": genre,
            "tone": "atmospheric, restrained",
            "audience": "adult",
            "themes": ["memory", "what we owe each other"],
            "pov": "third-person limited",
            "tense": "past",
            "style_guide": "Spare, sensory prose. Trust the reader. Short paragraphs; let silence carry weight.",
            "act_structure": "Act 1 hooks and sets the stakes; Act 2 escalates to a midpoint turn; Act 3 pays off every open thread.",
            "target_chapters": n,
            "characters": cast,
            "locations": locations,
            "items": items,
            "threads": threads,
            "outline": outline,
        }

    # ------------------------------------------------------------------
    # KDP listing fields (description / keywords / categories / reading age).
    # Reuses the same premise/genre extraction philosophy as _parse().
    _GENRE_CATEGORIES = {
        "mystery": ["Mystery, Thriller & Suspense > Mystery",
                    "Mystery, Thriller & Suspense > Crime Fiction",
                    "Literature & Fiction > Literary Fiction"],
        "thriller": ["Mystery, Thriller & Suspense > Thrillers & Suspense",
                     "Mystery, Thriller & Suspense > Crime Fiction",
                     "Literature & Fiction > Psychological Fiction"],
        "fantasy": ["Science Fiction & Fantasy > Fantasy > Epic",
                    "Science Fiction & Fantasy > Fantasy > Sword & Sorcery",
                    "Literature & Fiction > Mythology & Folk Tales"],
        "science fiction": ["Science Fiction & Fantasy > Science Fiction",
                            "Science Fiction & Fantasy > Science Fiction > Adventure",
                            "Literature & Fiction > Dystopian"],
        "sci-fi": ["Science Fiction & Fantasy > Science Fiction",
                   "Science Fiction & Fantasy > Science Fiction > Adventure",
                   "Literature & Fiction > Dystopian"],
        "romance": ["Romance > Contemporary",
                    "Romance > Romantic Suspense",
                    "Literature & Fiction > Women's Fiction"],
        "horror": ["Horror > Supernatural",
                   "Horror > Psychological",
                   "Literature & Fiction > Horror"],
        "historical": ["Literature & Fiction > Historical Fiction",
                       "Literature & Fiction > Literary Fiction",
                       "Romance > Historical"],
    }
    _GENRE_KEYWORDS = {
        "mystery": ["small town mystery", "amateur detective",
                    "twist ending suspense", "secrets and lies"],
        "thriller": ["psychological thriller", "page turner suspense",
                     "dark conspiracy", "edge of your seat"],
        "fantasy": ["epic fantasy adventure", "magic and prophecy",
                    "quest fantasy series", "hidden world fantasy"],
        "science fiction": ["space opera adventure", "dystopian future",
                            "first contact sci fi", "hard science fiction"],
        "romance": ["slow burn romance", "second chance love",
                    "emotional love story", "small town romance"],
        "horror": ["supernatural horror", "haunting and dread",
                   "creeping psychological horror", "small town nightmare"],
    }

    @classmethod
    def _kdp_parse(cls, user: str):
        """Extract premise/genre/title/audience from the KDP listing prompt."""
        def grab(pat, default=""):
            m = re.search(pat, user, re.IGNORECASE)
            return m.group(1).strip() if m else default
        pm = re.search(
            r"PREMISE:\s*\n?(.*?)(?:\n\s*(?:CHAPTER TITLES|Write the listing)|\Z)",
            user, re.DOTALL | re.IGNORECASE)
        premise = (pm.group(1).strip() if pm and pm.group(1).strip()
                   else "A story waiting to be told.")
        title = grab(r"BOOK TITLE:\s*(.+)", "")
        genre = grab(r"GENRE:\s*(.+)", "literary fiction") or "literary fiction"
        audience = grab(r"AUDIENCE:\s*(.+)", "adult") or "adult"
        return premise.strip(), genre.strip(), title.strip(), audience.strip()

    def _kdp(self, user: str) -> Dict[str, Any]:
        premise, genre, title, audience = self._kdp_parse(user)
        glow = genre.lower()

        # categories: first matching genre family, else literary defaults.
        categories = next(
            (v for k, v in self._GENRE_CATEGORIES.items() if k in glow),
            ["Literature & Fiction > Literary Fiction",
             "Literature & Fiction > Genre Fiction",
             "Literature & Fiction > Contemporary Fiction"],
        )[:3]

        # keywords: genre-family phrases + 1-2 premise-derived phrases. Never
        # the title or author; each <= 50 chars; deduped.
        base = next((v for k, v in self._GENRE_KEYWORDS.items() if k in glow),
                    ["literary fiction novel", "character driven story",
                     "book club fiction", "emotional family drama"])
        prem_words = self._keywords(premise)
        prem_kw = []
        if prem_words:
            prem_kw.append((prem_words[0] + " story").lower())
        if len(prem_words) > 1:
            prem_kw.append((prem_words[1] + " novel").lower())
        title_low = title.lower()
        keywords, seen = [], set()
        for k in base + prem_kw + ["new release fiction", "must read novel",
                                    "standalone novel"]:
            k = k.strip()[:50]
            lk = k.lower()
            if not k or lk in seen or (title_low and title_low in lk):
                continue
            seen.add(lk)
            keywords.append(k)
            if len(keywords) >= 7:
                break

        # description: punchy ~600-900 char back-cover blurb from the premise.
        hook = premise if len(premise) < 220 else premise[:217].rstrip() + "..."
        title_disp = title or "this novel"
        description = (
            f"<b>{title_disp}</b> is a {genre} you won't be able to put down.<br/><br/>"
            f"{hook}<br/><br/>"
            "As the pressure mounts, every choice carries a cost, and the line "
            "between who to trust and who to fear begins to blur. Secrets surface. "
            "Loyalties fracture. And by the time the truth arrives, nothing will "
            "ever be the same.<br/><br/>"
            f"Perfect for readers who love immersive, character-driven {genre} with "
            "a relentless pull and an ending that lingers. <i>One sitting won't be "
            "enough.</i>"
        )

        child = any(m in audience.lower()
                    for m in ("child", "kid", "middle grade", "young adult", "ya", "teen"))
        return {
            "subtitle": "",
            "description": description,
            "keywords": keywords,
            "categories": categories,
            "reading_age_min": "8" if child else "",
            "reading_age_max": "12" if child else "",
            "series_suggestion": "",
        }

    # ------------------------------------------------------------------
    # Marketing copy (blurb variants / A+ modules / author bio / taglines).
    def _marketing(self, user: str) -> Dict[str, Any]:
        def grab(pat, default=""):
            m = re.search(pat, user, re.IGNORECASE)
            return m.group(1).strip() if m else default
        pm = re.search(
            r"PREMISE:\s*\n?(.*?)(?:\n\s*(?:EXISTING DESCRIPTION|Write the marketing)|\Z)",
            user, re.DOTALL | re.IGNORECASE)
        premise = (pm.group(1).strip() if pm and pm.group(1).strip()
                   else "A story waiting to be told.")
        title = grab(r"BOOK TITLE:\s*(.+)", "") or "this novel"
        genre = grab(r"GENRE:\s*(.+)", "literary fiction") or "literary fiction"
        author = grab(r"AUTHOR:\s*(.+)", "") or "the author"

        hook = premise if len(premise) < 200 else premise[:197].rstrip() + "..."
        kw = self._keywords(premise)
        focus = kw[0] if kw else "what was lost"

        blurb_variants = [
            (f"<b>{title}</b> — {hook}<br/><br/>A {genre} about {focus} and the cost "
             "of the truth. Once you start, you won't want to stop."),
            (f"They thought it was over. It was only beginning.<br/><br/>{hook}<br/><br/>"
             f"A {genre} for readers who like their stories to leave a mark."),
        ]
        a_plus_modules = [
            {"headline": "A story that grips from page one",
             "body": f"{hook} A {genre} built on momentum and stakes that matter."},
            {"headline": "For readers who love being kept up all night",
             "body": f"Atmospheric, character-driven, and impossible to put down — "
                     f"{title} earns every page."},
        ]
        author_bio = (
            f"{author} writes {genre} that lingers long after the last page. "
            "When not writing, the author can be found chasing the next story.")
        taglines = [
            "Some truths refuse to stay buried.",
            f"A {genre} you'll finish in one sitting.",
            f"{title}: every choice has a cost.",
        ]
        return {
            "blurb_variants": blurb_variants,
            "a_plus_modules": a_plus_modules,
            "author_bio": author_bio,
            "taglines": taglines,
        }

    def _delta(self, user: str) -> Dict[str, Any]:
        m = re.search(r"CHAPTER (\d+)", user)
        n = int(m.group(1)) if m else 1
        beats = [
            "Wren follows the first sign and refuses to look away.",
            "An old promise resurfaces, and with it a cost Wren hadn't counted.",
            "Tomas takes a side; the ground shifts under both of them.",
            "Iris makes her move, and what Wren believed gives way.",
            "Sabine reveals a piece of the truth — and withholds the rest.",
            "The bond between Wren and Iris bends to its breaking point.",
            "A choice is made that cannot be unmade.",
            "What remained is gathered up; the reckoning lands.",
        ]
        rel = [{"with": "rival", "relation": "wary, after the confrontation"}] if n % 2 == 0 else []
        return {
            "synopsis_line": beats[(n - 1) % len(beats)],
            "timeline_summary": f"the {_ordinal(n)} night",
            "time_marker": "that night",
            "character_updates": [
                {"id": "hero", "knowledge_added": [f"the meaning of the keepsake (part {n})"],
                 "relationship_updates": rel},
            ],
            "new_characters": [],
            "new_locations": [],
            "new_items": [],
            "threads_opened": [],
            "threads_resolved": [],
            "continuity_flags": [],
        }

    @staticmethod
    def _chapter_title(user: str) -> str:
        m = re.search(r"CHAPTER \d+: ([^\n(]+)", user)
        return m.group(1).strip() if m else "the chapter"

    # ------------------------------------------------------------------
    def _record(self, ledger: CostLedger, stage: str, model: StageModel, system: str,
                user: str, cached: Optional[str], use_cache: bool, cache_ttl: str,
                out_tokens: int) -> None:
        sys_toks = len(system) // 4
        user_toks = len(user) // 4
        cache_create = 0
        cache_read = 0
        input_toks = sys_toks + user_toks
        if cached:
            ctoks = len(cached) // 4
            key = hash(cached)
            if use_cache and key in self._seen_prefixes:
                cache_read = ctoks
            elif use_cache:
                cache_create = ctoks
                self._seen_prefixes.add(key)
            else:
                input_toks += ctoks
        ledger.add(Usage(
            model=model.model, stage=stage,
            input_tokens=input_toks, output_tokens=out_tokens,
            cache_creation_tokens=cache_create, cache_read_tokens=cache_read,
            cache_ttl=cache_ttl,
        ))
