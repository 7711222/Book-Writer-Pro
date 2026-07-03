"""Continuity checker stage (optional, cheap model).

A second cheap pass that flags concrete contradictions against the established
state. Distinct from the extractor's incidental ``continuity_flags`` — this one
is dedicated to error-finding and is configurable on/off.
"""
from __future__ import annotations

from typing import Dict, List

from .config import Settings
from .costs import CostLedger
from .graph import StoryGraph
from .llm import LLM
from .models import ChapterPlan, ChapterRecord
from .prompts import CHECKER_SYSTEM, CHECK_SCHEMA


def check_chapter(
    llm: LLM, settings: Settings, ledger: CostLedger, graph: StoryGraph,
    plan: ChapterPlan, rec: ChapterRecord,
) -> List[Dict[str, str]]:
    context = graph.relevant_slice(plan)
    user = (
        f"ESTABLISHED STATE:\n{context}\n\n"
        f"INTENDED BEATS: {'; '.join(plan.beats) if plan.beats else '(none)'}\n\n"
        f"CHAPTER {plan.number} TEXT:\n{rec.text}"
    )
    data = llm.complete_json(
        stage="check",
        model=settings.profile.check,
        system=CHECKER_SYSTEM,
        user=user,
        schema=CHECK_SCHEMA,
        max_tokens=4000,
        ledger=ledger,
        cached=None,
        use_cache=False,
    )
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        return []
    # The json_object provider path can't structurally enforce the schema —
    # drop non-dict entries so the pipeline's severity/description reads
    # never crash (which would be misreported as a failed extraction).
    return [i for i in issues if isinstance(i, dict)]
