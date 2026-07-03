"""Generic subprocess LLM backend for *subscription* coding CLIs.

Same idea as ``llm_claude_cli.py`` but vendor-agnostic: it shells out to any
configured command, feeds the whole prompt on **stdin**, and reads the model's
reply from **stdout**. That's how you ride a monthly subscription instead of an
API key — the CLI carries the auth:

    codex     OpenAI Codex CLI   (`codex exec`)   — ChatGPT Plus/Pro login
    grok-cli  a Grok CLI                          — X Premium / SuperGrok login
    cli       anything you set in BOOKWRITER_CLI_CMD

CLIs that cannot read stdin (grok-cli) get the prompt as an argv element, which
is capped by the OS (~32 KB on Windows). For big prompts put ``{promptfile}``
in the configured command: the prompt is written to a temp file and the
placeholder is replaced with its path.

Because these CLIs rarely report token usage in a stable, parseable way, usage
is *estimated* from text length (so the cost meter isn't blank) and recorded
against the provider/model id — which is normally absent from MODEL_PRICES, so
the reported cost is $0. That's correct: a subscription run is not billed per
token. (Contrast ``ClaudeCliLLM``, which parses Claude's real usage JSON.)

Structured (JSON) stages append a "return only JSON" instruction and the schema
to the prompt, then extract the first JSON object from stdout.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, List, Optional

from .costs import CostLedger, Usage
from .llm_claude_cli import _build_prompt, _strip_fences, _subprocess_runner
from .provider import cli_cmd_env, resolve_model, target_model

# Literal placeholder a user can put in BOOKWRITER_*_CMD: the prompt is written
# to a temp file and the placeholder is replaced with that file's path. This is
# the command-line-length-proof way to feed a big prompt to an argv-only CLI.
PROMPT_FILE_TOKEN = "{promptfile}"

# CLIs that take the prompt as an argv element hit the OS command-line limit:
# ~32 KB for the whole line on Windows (CreateProcess), ~128 KB per argument on
# Linux. Refuse above these with a clear message instead of a cryptic OSError
# from deep inside subprocess.
_ARGV_PROMPT_LIMIT = 30_000 if os.name == "nt" else 100_000


class GenericCliLLM:
    """Implements the pipeline ``LLM`` protocol via an arbitrary subprocess.

    ``runner`` may be injected for testing: a callable
    ``(args: list[str], stdin: str) -> (returncode, stdout, stderr)``.
    """

    def __init__(
        self,
        *,
        command: List[str],
        provider: str = "cli",
        model_flag: Optional[str] = None,
        model_override: Optional[str] = None,
        stdin: bool = True,
        runner: Optional[Callable] = None,
    ):
        if not command:
            raise RuntimeError(
                f"No command configured for the '{provider}' provider. "
                f"Set the appropriate BOOKWRITER_*_CMD environment variable."
            )
        self.command = command
        self.provider = provider
        self.model_flag = model_flag
        self.model_override = model_override
        self.stdin = stdin
        self._runner = runner or _subprocess_runner

    # ------------------------------------------------------------------
    def complete_json(self, *, stage, model, system, user, schema, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h"):
        instruction = (
            "Return ONLY a single JSON object — no prose, no code fences — "
            "conforming to this JSON Schema:\n" + json.dumps(schema)
        )
        prompt = _build_prompt(system, cached, user, instruction)
        out = self._run(stage, model, prompt)
        self._record(ledger, stage, model, prompt, out, cache_ttl)
        text = _extract_json(out)
        return json.loads(text) if text else {}

    # ------------------------------------------------------------------
    def complete_text(self, *, stage, model, system, user, max_tokens,
                      ledger, cached=None, use_cache=True, cache_ttl="1h", on_delta=None):
        prompt = _build_prompt(system, cached, user, "")
        out = self._run(stage, model, prompt).strip()
        self._record(ledger, stage, model, prompt, out, cache_ttl)
        if on_delta is not None and out:
            on_delta(out)
        return out

    # ------------------------------------------------------------------
    def _target(self, stage, model) -> str:
        return resolve_model(stage, self.model_override,
                             target_model(self.provider, model.model))

    def _run(self, stage, model, prompt: str) -> str:
        args = list(self.command)
        if self.model_flag:
            args += [self.model_flag, self._target(stage, model)]
        stdin = prompt
        prompt_file = None
        try:
            if any(PROMPT_FILE_TOKEN in a for a in args):
                # Feed the prompt via a temp file — immune to argv length limits.
                fd, prompt_file = tempfile.mkstemp(prefix="bookwriter-prompt-", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(prompt)
                args = [a.replace(PROMPT_FILE_TOKEN, prompt_file) for a in args]
                stdin = ""
            elif not self.stdin:
                if len(prompt) > _ARGV_PROMPT_LIMIT:
                    env = cli_cmd_env(self.provider)
                    raise RuntimeError(
                        f"The '{self.provider}' CLI takes the prompt as a command-line "
                        f"argument, but this prompt is {len(prompt):,} characters — beyond "
                        f"the ~{_ARGV_PROMPT_LIMIT // 1000} KB the OS allows on a command "
                        f"line. Configure a stdin- or file-based invocation instead: set "
                        f"{env} to a command containing {PROMPT_FILE_TOKEN}, which is "
                        f"replaced with the path of a temp file holding the prompt."
                    )
                args.append(prompt)
                stdin = ""
            code, out, err = self._runner(args, stdin)
        finally:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass
        if code != 0:
            raise RuntimeError(
                f"`{' '.join(self.command)}` (provider '{self.provider}') exited "
                f"{code}. Is the CLI installed and signed in to your subscription? "
                f"stderr: {(err or '').strip()[:500]}"
            )
        return out or ""

    def _record(self, ledger: CostLedger, stage: str, model, prompt: str,
                out: str, cache_ttl: str) -> None:
        # No reliable token counts from these CLIs -> estimate (~4 chars/token).
        # Tag the model id with the provider so it is NOT priced like the Anthropic
        # tier it stands in for: a subscription run is $0 per token, and the cost
        # meter should say so.
        label = f"{self.provider}:{self._target(stage, model)}"
        ledger.add(Usage(
            model=label,
            stage=stage,
            input_tokens=len(prompt) // 4,
            output_tokens=len(out) // 4,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cache_ttl=cache_ttl,
        ))


def _extract_json(text: str) -> str:
    """Pull a JSON object out of arbitrary CLI stdout (logs, banners, fences)."""
    t = _strip_fences(text)
    try:
        json.loads(t)
        return t
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    return t
