"""Configuration: model tiers, pricing, and quality profiles.

The central knob for the project's goal (minimum token cost per book) is the
*quality profile*, which assigns a model tier to each pipeline stage. Prose is
the expensive, quality-sensitive stage; extraction and continuity checking are
mechanical and run on the cheapest capable model.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Pricing — USD per 1M tokens. Cached snapshot (2026-06); override in Settings
# or refresh from the Models API if Anthropic changes pricing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    input: float          # $/1M uncached input tokens
    output: float         # $/1M output tokens
    cache_write_5m: float  # $/1M tokens written to the 5-minute cache (1.25x input)
    cache_write_1h: float  # $/1M tokens written to the 1-hour cache (2x input)
    cache_read: float      # $/1M tokens read from cache (~0.1x input)

    @classmethod
    def from_base(cls, inp: float, out: float) -> "ModelPrice":
        return cls(
            input=inp,
            output=out,
            cache_write_5m=inp * 1.25,
            cache_write_1h=inp * 2.0,
            cache_read=inp * 0.1,
        )


MODEL_PRICES: Dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice.from_base(5.0, 25.0),
    "claude-opus-4-7": ModelPrice.from_base(5.0, 25.0),
    "claude-sonnet-4-6": ModelPrice.from_base(3.0, 15.0),
    "claude-haiku-4-5": ModelPrice.from_base(1.0, 5.0),
    "claude-fable-5": ModelPrice.from_base(10.0, 50.0),
}

# Models that accept the `effort` parameter (Opus 4.5+, Sonnet 4.6, Fable 5).
EFFORT_CAPABLE = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-fable-5",
}

# Models that support adaptive thinking ({"type": "adaptive"}): the 4.6+ line and
# Fable. Older models (e.g. Haiku 4.5) only speak the legacy enabled/budget_tokens
# form, so when one of them is the EFFECTIVE model (per-book override) the
# adaptive param must be omitted entirely instead of sent (see llm._thinking_param).
ADAPTIVE_CAPABLE = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-fable-5",
}


@dataclass(frozen=True)
class StageModel:
    """Per-stage model selection."""
    model: str
    effort: str = "medium"   # low | medium | high | xhigh | max  (ignored if model lacks effort support)
    thinking: bool = True    # adaptive thinking on/off


@dataclass(frozen=True)
class QualityProfile:
    """Assigns a model to each pipeline stage.

    The whole cost/quality tradeoff lives here. ``plan`` and ``write`` are the
    quality-sensitive stages; ``extract`` and ``check`` are mechanical.
    """
    name: str
    plan: StageModel
    write: StageModel
    extract: StageModel
    check: StageModel


# Three presets. `premium` keeps prose on Opus (the skill default — never
# silently downgrade the marquee stage); `balanced` and `draft` are the
# explicit cost levers the project goal asks for.
QUALITY_PROFILES: Dict[str, QualityProfile] = {
    "premium": QualityProfile(
        name="premium",
        plan=StageModel("claude-opus-4-8", effort="high"),
        write=StageModel("claude-opus-4-8", effort="medium"),
        extract=StageModel("claude-haiku-4-5", effort="low", thinking=False),
        check=StageModel("claude-haiku-4-5", effort="low", thinking=False),
    ),
    "balanced": QualityProfile(
        name="balanced",
        plan=StageModel("claude-opus-4-8", effort="high"),
        write=StageModel("claude-sonnet-4-6", effort="medium"),
        extract=StageModel("claude-haiku-4-5", effort="low", thinking=False),
        check=StageModel("claude-haiku-4-5", effort="low", thinking=False),
    ),
    "draft": QualityProfile(
        name="draft",
        plan=StageModel("claude-sonnet-4-6", effort="medium"),
        write=StageModel("claude-sonnet-4-6", effort="low"),
        extract=StageModel("claude-haiku-4-5", effort="low", thinking=False),
        check=StageModel("claude-haiku-4-5", effort="low", thinking=False),
    ),
}

DEFAULT_PROFILE = "balanced"


@dataclass
class Settings:
    """Runtime settings for a generation run."""
    profile: QualityProfile = field(default_factory=lambda: QUALITY_PROFILES[DEFAULT_PROFILE])

    # Token-cost levers
    use_cache: bool = True          # cache the stable bible prefix (huge lever)
    cache_ttl: str = "1h"           # "5m" | "1h"; 1h survives slow/interactive runs
    synopsis_line_chars: int = 240  # cap per-chapter rolling-synopsis line
    prev_tail_words: int = 120      # words of previous chapter fed for voice continuity
    run_continuity_check: bool = True
    chapter_images: bool = False    # generate one inline illustration per chapter

    # Output sizing
    max_tokens_plan: int = 16000
    max_tokens_write: int = 16000   # ~ up to a long chapter; streamed
    max_tokens_extract: int = 4000

    # Persistence
    project_dir: str = "."          # where book.json / story_graph.json / chapters/ live

    api_key: Optional[str] = None   # falls back to ANTHROPIC_API_KEY env var

    def with_profile(self, name: str) -> "Settings":
        if name not in QUALITY_PROFILES:
            raise ValueError(f"unknown profile {name!r}; choose from {sorted(QUALITY_PROFILES)}")
        return replace(self, profile=QUALITY_PROFILES[name])
