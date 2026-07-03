"""OpenAI-compatible LLM backend (OpenAI direct *and* OpenRouter).

Implements the ``LLM`` protocol from ``llm.py`` against any OpenAI Chat
Completions endpoint. The same class serves both providers — OpenRouter is just
OpenAI's wire format pointed at a different ``base_url`` (set in ``provider.py``).

Anthropic-only features are intentionally dropped here: prompt caching
(``cache_control``), adaptive ``thinking``, and the ``effort`` knob have no
portable equivalent, so the "cached bible" block is simply folded into the
system message and paid for in full each call. Structured output uses
``response_format={"type":"json_object"}`` with the JSON Schema embedded in the
prompt — the most broadly supported path across OpenAI and OpenRouter models.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from .costs import CostLedger, Usage
from .llm import TruncatedOutputError
from .provider import resolve_model, target_model


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # ```json\n...\n``` -> drop the first line and a trailing fence
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class OpenAICompatLLM:
    """OpenAI / OpenRouter client implementing the pipeline ``LLM`` protocol.

    ``client`` may be injected for testing; otherwise the ``openai`` SDK is
    imported lazily and an ``OpenAI`` client is constructed from ``api_key`` /
    ``base_url``.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider: str = "openai",
        model_override: Optional[str] = None,
        client: Any = None,
    ):
        self.provider = provider
        self.model_override = model_override
        # Why the last response stopped, normalized to Anthropic vocabulary
        # ("length" -> "max_tokens") so the pipeline's truncation check is
        # provider-agnostic.
        self.last_stop_reason: Optional[str] = None
        if client is not None:
            self.client = client
            return
        try:
            import openai  # noqa: F401
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "The 'openai' package is required for the openai/openrouter "
                "providers. Install it with: pip install openai"
            ) from e
        kwargs: Dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = openai.OpenAI(**kwargs)

    # ------------------------------------------------------------------
    def complete_json(self, *, stage, model, system, user, schema, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h"):
        target = resolve_model(stage, self.model_override, target_model(self.provider, model.model))
        sys_text = self._system(system, cached)
        sys_text += (
            "\n\nReturn ONLY a single JSON object — no prose, no code fences — "
            "conforming to this JSON Schema:\n" + json.dumps(schema)
        )
        resp = self.client.chat.completions.create(
            model=target,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        self._record(ledger, stage, target, resp.usage, cache_ttl)
        finish = getattr(resp.choices[0], "finish_reason", None)
        self.last_stop_reason = "max_tokens" if finish == "length" else finish
        if finish == "length":
            # A cut-off JSON payload can never parse — fail with the real cause.
            raise TruncatedOutputError(
                f"{stage}: JSON output truncated at max_tokens={max_tokens} "
                "(finish_reason=length) — raise the stage's max_tokens setting and retry."
            )
        text = resp.choices[0].message.content or ""
        if not text:
            return {}
        return json.loads(_strip_fences(text))

    # ------------------------------------------------------------------
    def complete_text(self, *, stage, model, system, user, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h", on_delta=None):
        target = resolve_model(stage, self.model_override, target_model(self.provider, model.model))
        sys_text = self._system(system, cached)
        stream = self.client.chat.completions.create(
            model=target,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user},
            ],
            stream=True,
            stream_options={"include_usage": True},
        )
        parts = []
        usage = None
        finish = None
        for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            finish = getattr(choices[0], "finish_reason", None) or finish
            delta = getattr(choices[0], "delta", None)
            piece = getattr(delta, "content", None) if delta else None
            if piece:
                parts.append(piece)
                if on_delta is not None:
                    on_delta(piece)
        text = "".join(parts)
        self._record(ledger, stage, target, usage, cache_ttl, fallback_out=len(text) // 4)
        # Don't raise for prose (the text up to the cut is still valuable); the
        # pipeline reads last_stop_reason and flags the chapter as truncated.
        self.last_stop_reason = "max_tokens" if finish == "length" else finish
        return text

    # ------------------------------------------------------------------
    @staticmethod
    def _system(system: str, cached: Optional[str]) -> str:
        return system if not cached else f"{system}\n\n{cached}"

    @staticmethod
    def _record(ledger: CostLedger, stage: str, model: str, usage: Any,
                cache_ttl: str, fallback_out: int = 0) -> None:
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        if not out_tok:
            out_tok = fallback_out
        ledger.add(Usage(
            model=model,
            stage=stage,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cache_ttl=cache_ttl,
        ))
