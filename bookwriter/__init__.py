"""BookwriterPro — a local, token-cost-optimized book generation engine.

Architecture (adapted from the Understand-Anything knowledge-graph approach):

    premise ──▶ Planner ──▶ Story Bible + Continuity Graph (committed JSON)
                                 │
              ┌──────────────────┴───────────────────┐
              ▼                                       │
        Chapter Writer  ◀── cached bible prefix ──────┤
              │            (paid once at ~0.1x/read)   │
              ▼                                        │
        Extractor (cheap model) ──▶ state deltas ──────┘
              │                     merged into graph
              ▼
        Continuity Checker (cheap model, optional)

Token-cost minimization is the central design constraint. See ``costs.py`` and
the README for the full accounting.
"""

__version__ = "1.1.0"

from .config import Settings, QUALITY_PROFILES  # noqa: E402,F401
