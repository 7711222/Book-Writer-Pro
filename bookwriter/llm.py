"""LLM client abstraction.

A thin protocol (`LLM`) plus the real `AnthropicLLM` implementation. The protocol
lets the pipeline run against a mock with zero network/credentials (see
``mock.py``), which is how the test suite and ``--mock`` smoke runs work.

Two entry points:
  * ``complete_json``  — structured output via ``output_config.format`` (planner, extractor, checker)
  * ``complete_text``  — streamed prose (chapter writer)

Both accept an optional ``cached`` block (the stable bible). When caching is on,
it is emitted as a ``cache_control`` system block — paid ~1.25-2x once, then read
at ~0.1x on every subsequent chapter. That is the core token-cost lever.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Protocol

from .config import StageModel, EFFORT_CAPABLE, ADAPTIVE_CAPABLE
from .costs import CostLedger, Usage


class TruncatedOutputError(RuntimeError):
    """A JSON-stage response stopped at ``max_tokens`` — the payload is cut off
    mid-object and can never parse. Raised instead of letting ``json.loads``
    fail with a cryptic decode error; the fix is raising the stage's token cap."""


class LLM(Protocol):
    # Implementations MAY also expose a ``last_stop_reason`` attribute (set after
    # each call; ``"max_tokens"`` marks a truncated response). It is read via
    # ``getattr(llm, "last_stop_reason", None)`` so adapters that don't track it
    # (the subscription CLIs) need no change.
    def complete_json(
        self, *, stage: str, model: StageModel, system: str, user: str,
        schema: Dict[str, Any], max_tokens: int, ledger: CostLedger,
        cached: Optional[str] = None, use_cache: bool = True, cache_ttl: str = "1h",
    ) -> Dict[str, Any]: ...

    def complete_text(
        self, *, stage: str, model: StageModel, system: str, user: str,
        max_tokens: int, ledger: CostLedger,
        cached: Optional[str] = None, use_cache: bool = True, cache_ttl: str = "1h",
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> str: ...


def _build_system(system: str, cached: Optional[str], use_cache: bool, cache_ttl: str) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": system}]
    if cached:
        block: Dict[str, Any] = {"type": "text", "text": cached}
        if use_cache:
            block["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}
        blocks.append(block)
    return blocks


# Models that reject an explicit thinking:{"type":"disabled"} with a 400 — for
# these the param must be *omitted* to turn thinking off, not set to disabled.
_THINKING_OMIT_WHEN_DISABLED = frozenset({"claude-fable-5"})


def _thinking_param(sm: StageModel, mdl: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The ``thinking`` request value for the EFFECTIVE model ``mdl`` (defaults
    to the profile model), or ``None`` to omit the param entirely.

    Gating on the effective id matters when a per-book override redirects a
    stage to a model that doesn't support adaptive thinking (e.g. Haiku 4.5):
    sending ``{"type": "adaptive"}`` there would 400 every request, so the
    param is omitted instead."""
    mdl = mdl or sm.model
    if sm.thinking:
        return {"type": "adaptive"} if mdl in ADAPTIVE_CAPABLE else None
    if mdl in _THINKING_OMIT_WHEN_DISABLED:
        return None
    return {"type": "disabled"}


def _output_config(sm: StageModel, mdl: Optional[str] = None,
                   fmt: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Build ``output_config``, gating ``effort`` on the EFFECTIVE model ``mdl``
    (a per-book override may pick a model that rejects the effort param)."""
    cfg: Dict[str, Any] = {}
    if (mdl or sm.model) in EFFORT_CAPABLE:
        cfg["effort"] = sm.effort
    if fmt is not None:
        cfg["format"] = fmt
    return cfg or None


class AnthropicLLM:
    """Real client. Imports the SDK lazily so the package imports without it."""

    def __init__(self, api_key: Optional[str] = None, model_override: Optional[str] = None):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "The 'anthropic' package is required for live generation. "
                "Install it with: pip install anthropic"
            ) from e
        self._anthropic = anthropic
        self.model_override = model_override
        # Why the last response stopped ("end_turn", "max_tokens", ...). The
        # pipeline reads it right after a prose call to flag truncated chapters.
        self.last_stop_reason: Optional[str] = None
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def _model_for(self, stage: str, model: StageModel) -> str:
        from .provider import resolve_model
        return resolve_model(stage, self.model_override, model.model)

    # ------------------------------------------------------------------
    def complete_json(self, *, stage, model, system, user, schema, max_tokens,
                       ledger, cached=None, use_cache=True, cache_ttl="1h"):
        sys_blocks = _build_system(system, cached, use_cache, cache_ttl)
        mdl = self._model_for(stage, model)
        out_cfg = _output_config(model, mdl, fmt={"type": "json_schema", "schema": schema})
        params: Dict[str, Any] = {
            "model": mdl,
            "max_tokens": max_tokens,
            "system": sys_blocks,
            "messages": [{"role": "user", "content": user}],
            "output_config": out_cfg,
        }
        think = _thinking_param(model, mdl)
        if think is not None:
            params["thinking"] = think
        resp = self.client.messages.create(**params)
        self._record(ledger, stage, mdl, resp, cache_ttl)
        self.last_stop_reason = getattr(resp, "stop_reason", None)
        if self.last_stop_reason == "max_tokens":
            # A cut-off JSON payload can never parse — fail with the real cause
            # instead of a cryptic JSONDecodeError downstream.
            raise TruncatedOutputError(
                f"{stage}: JSON output truncated at max_tokens={max_tokens} — "
                "raise the stage's max_tokens setting and retry."
            )
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return json.loads(text) if text else {}

    # ------------------------------------------------------------------
    def complete_text(self, *, stage, model, system, user, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h", on_delta=None):
        sys_blocks = _build_system(system, cached, use_cache, cache_ttl)
        mdl = self._model_for(stage, model)
        out_cfg = _output_config(model, mdl)
        kwargs: Dict[str, Any] = {
            "model": mdl,
            "max_tokens": max_tokens,
            "system": sys_blocks,
            "messages": [{"role": "user", "content": user}],
        }
        think = _thinking_param(model, mdl)
        if think is not None:
            kwargs["thinking"] = think
        if out_cfg:
            kwargs["output_config"] = out_cfg
        # Stream long prose to avoid SDK HTTP timeouts on large max_tokens, and to
        # forward live text deltas to the UI when on_delta is provided.
        with self.client.messages.stream(**kwargs) as stream:
            if on_delta is not None:
                for chunk in stream.text_stream:
                    on_delta(chunk)
            final = stream.get_final_message()
        self._record(ledger, stage, mdl, final, cache_ttl)
        # Don't raise for prose: the text up to the cut is still valuable. The
        # pipeline reads last_stop_reason and flags the chapter as truncated.
        self.last_stop_reason = getattr(final, "stop_reason", None)
        return "".join(b.text for b in final.content if getattr(b, "type", "") == "text")

    # ------------------------------------------------------------------
    @staticmethod
    def _record(ledger: CostLedger, stage: str, model: str, resp: Any, cache_ttl: str) -> None:
        u = resp.usage
        ledger.add(Usage(
            model=model,
            stage=stage,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_ttl=cache_ttl,
        ))
