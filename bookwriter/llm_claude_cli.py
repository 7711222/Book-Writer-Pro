"""Claude Code CLI backend — generate via ``claude -p`` (headless mode).

This backend shells out to the locally-installed Claude Code CLI instead of
hitting the Anthropic API directly. The point: ``claude`` authenticates with
whatever credentials Claude Code is configured with, which for most people is a
**Claude Pro/Max subscription** (via ``claude login``). So this lets the studio
generate real prose with *no* ANTHROPIC_API_KEY and *no* per-token API billing.

Tradeoffs vs the direct API backend:
  * No prompt-cache / thinking / effort controls (the CLI manages its own).
  * Prose arrives in one block, not token-by-token — we run the CLI in JSON mode
    for robustness across CLI versions/platforms, then emit the full text once
    via ``on_delta``. The "Writing…" UI still works; it just fills in at the end.
  * The reported ``$`` is the API-equivalent cost the CLI reports, recorded
    against the real Anthropic model id — useful as a gauge, but a subscription
    run does not actually bill per token.

The whole prompt (system + cached bible + task) is fed on **stdin**, not as argv,
to avoid the ~32 KB Windows command-line length limit on large bibles.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable, Dict, Optional

from . import runtime_config as rc
from .costs import CostLedger, Usage
from .provider import resolve_model, target_model

# Default seconds before a hung CLI subprocess is killed (BOOKWRITER_CLI_TIMEOUT).
_DEFAULT_CLI_TIMEOUT = 600.0


def _cli_timeout() -> float:
    """Subprocess timeout in seconds — BOOKWRITER_CLI_TIMEOUT, default 600."""
    raw = rc.getenv("BOOKWRITER_CLI_TIMEOUT") or ""
    try:
        return float(raw) if raw.strip() else _DEFAULT_CLI_TIMEOUT
    except ValueError:
        return _DEFAULT_CLI_TIMEOUT


def _subprocess_runner(args, stdin):
    """Default runner for every CLI backend: run the command with a hang-proof
    timeout. A CLI stuck on an interactive login/trust prompt (or a stalled
    network call) must not block a generation job forever."""
    timeout = _cli_timeout()
    try:
        proc = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"`{args[0]}` produced no result within {timeout:.0f}s — is the CLI "
            f"signed in and non-interactive? (set BOOKWRITER_CLI_TIMEOUT to raise "
            f"the limit)"
        ) from None
    return proc.returncode, proc.stdout, proc.stderr


class ClaudeCliLLM:
    """Implements the pipeline ``LLM`` protocol by invoking ``claude -p``.

    ``runner`` may be injected for testing: a callable
    ``(args: list[str], stdin: str) -> (returncode, stdout, stderr)``. The
    default runs the real CLI via :func:`subprocess.run`.
    """

    def __init__(self, binary: Optional[str] = None, runner: Optional[Callable] = None,
                 model_override: Optional[str] = None):
        self.binary = binary or shutil.which("claude") or "claude"
        self._runner = runner or _subprocess_runner
        self.model_override = model_override

    # ------------------------------------------------------------------
    def complete_json(self, *, stage, model, system, user, schema, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h"):
        instruction = (
            "Return ONLY a single JSON object — no prose, no code fences — "
            "conforming to this JSON Schema:\n" + json.dumps(schema)
        )
        text, usage = self._invoke(stage, model, system, cached, user, extra=instruction)
        self._record(ledger, stage, model.model, usage, cache_ttl)
        text = _strip_fences(text)
        return json.loads(text) if text else {}

    # ------------------------------------------------------------------
    def complete_text(self, *, stage, model, system, user, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h", on_delta=None):
        text, usage = self._invoke(stage, model, system, cached, user)
        self._record(ledger, stage, model.model, usage, cache_ttl)
        if on_delta is not None and text:
            on_delta(text)
        return text

    # ------------------------------------------------------------------
    def _invoke(self, stage, model, system, cached, user, extra: str = ""):
        alias = resolve_model(stage, self.model_override, target_model("claude-cli", model.model))
        prompt = _build_prompt(system, cached, user, extra)
        args = [self.binary, "-p", "--model", alias, "--output-format", "json"]
        code, out, err = self._runner(args, prompt)
        if code != 0:
            raise RuntimeError(
                f"`claude -p` exited {code}. Is the Claude Code CLI installed and "
                f"logged in (`claude login`)? stderr: {(err or '').strip()[:500]}"
            )
        try:
            obj = json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse `claude -p` JSON output: {e}. "
                f"First 300 chars: {out[:300]!r}"
            ) from e
        # Headless JSON shape: {"result": "...", "usage": {...}, "is_error": bool, ...}
        if obj.get("is_error"):
            raise RuntimeError(f"`claude -p` reported an error: {obj.get('result') or obj}")
        text = obj.get("result") or ""
        usage = obj.get("usage") or {}
        return text, usage

    # ------------------------------------------------------------------
    @staticmethod
    def _record(ledger: CostLedger, stage: str, model: str, usage: Dict[str, Any],
                cache_ttl: str) -> None:
        ledger.add(Usage(
            model=model,
            stage=stage,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_ttl=cache_ttl,
        ))


def _build_prompt(system: str, cached: Optional[str], user: str, extra: str) -> str:
    blocks = ["SYSTEM INSTRUCTIONS:", system]
    if cached:
        blocks += ["", "REFERENCE (story bible / context):", cached]
    if extra:
        blocks += ["", extra]
    blocks += ["", "TASK:", user]
    return "\n".join(blocks)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
