"""LLM provider selection.

One place that decides *which* backend the pipeline runs against, driven by the
``BOOKWRITER_LLM_PROVIDER`` environment variable:

    anthropic   (default)  Anthropic API           — needs ANTHROPIC_API_KEY
    openai                 OpenAI API              — needs OPENAI_API_KEY
    openrouter             OpenRouter (OpenAI-compat) — needs OPENROUTER_API_KEY
    claude-cli             shells out to ``claude -p`` — uses Claude Code's own
                           auth, so a Claude Pro/Max *subscription* works with no
                           API key and no per-token billing.

The pipeline only knows the ``LLM`` protocol (see ``llm.py``); every backend here
implements it. The factory ``make_llm`` and the gate ``live_available`` are the
two functions the CLI / server / MCP layers call.

Model ids in the quality profiles are Anthropic ids (``claude-opus-4-8`` …). For
non-Anthropic providers we map each Anthropic id to a *tier* (strong / mid /
cheap) and then to a provider-specific model id, overridable per tier via env:

    BOOKWRITER_MODEL_STRONG   BOOKWRITER_MODEL_MID   BOOKWRITER_MODEL_CHEAP
"""
from __future__ import annotations

import os
import shlex
import shutil
from typing import List, Optional

from . import runtime_config as rc
from .config import MODEL_PRICES, ModelPrice

# --------------------------------------------------------------------------- #
# Provider name
# --------------------------------------------------------------------------- #

_ALIASES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "oai": "openai",
    "openrouter": "openrouter",
    "router": "openrouter",
    "claude-cli": "claude-cli",
    "claude_cli": "claude-cli",
    "subscription": "claude-cli",
    "claude-code": "claude-cli",
    # OpenAI ChatGPT subscription via the Codex CLI
    "codex": "codex",
    "openai-cli": "codex",
    "chatgpt": "codex",
    "openai-subscription": "codex",
    # Grok (xAI) — direct API (OpenAI-compatible) is the default "grok"…
    "grok": "grok",
    "xai": "grok",
    "x.ai": "grok",
    "grok-api": "grok",
    # …and the CLI/subscription path stays available as grok-cli.
    "grok-cli": "grok-cli",
    "xai-cli": "grok-cli",
    # Fully custom subprocess command
    "cli": "cli",
    "custom-cli": "cli",
}

# Providers that ride a subscription by shelling out to a vendor CLI.
_CLI_PROVIDERS = {"claude-cli", "codex", "grok-cli", "cli"}

DEFAULT_PROVIDER = "anthropic"


def normalize_provider(raw: Optional[str]) -> str:
    """Map any provider spelling/alias to its canonical id."""
    key = (raw or DEFAULT_PROVIDER).strip().lower()
    return _ALIASES.get(key, key)


def provider_name() -> str:
    """Normalized provider id from ``BOOKWRITER_LLM_PROVIDER`` (default anthropic)."""
    return normalize_provider(rc.getenv("BOOKWRITER_LLM_PROVIDER"))


# Stages whose model a per-book "Text model" pick should override. The cheap,
# mechanical stages (extract/check) keep the provider's cheap-tier default so the
# cost-tiering lever survives even when a user picks a strong prose model.
PROSE_STAGES = frozenset({"plan", "write"})


def resolve_model(stage: str, override: Optional[str], default: str) -> str:
    """The model id to actually use for *stage*, honoring a per-book override."""
    if override and stage in PROSE_STAGES:
        return override
    return default


# --------------------------------------------------------------------------- #
# Tier mapping  (Anthropic profile id -> strong/mid/cheap -> provider model id)
# --------------------------------------------------------------------------- #

_STRONG = {"claude-opus-4-8", "claude-opus-4-7", "claude-fable-5"}
_MID = {"claude-sonnet-4-6"}
# everything else (haiku) falls through to "cheap"


def tier_of(anthropic_model: str) -> str:
    if anthropic_model in _STRONG:
        return "strong"
    if anthropic_model in _MID:
        return "mid"
    return "cheap"


# Per-provider defaults. All overridable via the BOOKWRITER_MODEL_* env vars.
_PROVIDER_DEFAULTS = {
    "openai": {
        "strong": "gpt-4.1",
        "mid": "gpt-4.1-mini",
        "cheap": "gpt-4.1-nano",
    },
    "openrouter": {
        "strong": "openai/gpt-4.1",
        "mid": "openai/gpt-4.1-mini",
        "cheap": "openai/gpt-4.1-mini",
    },
    # Grok (xAI) — OpenAI-compatible API at api.x.ai.
    "grok": {
        "strong": "grok-4",
        "mid": "grok-3",
        "cheap": "grok-3-mini",
    },
    # claude-cli speaks Anthropic model *aliases* to the CLI's --model flag.
    "claude-cli": {
        "strong": "opus",
        "mid": "sonnet",
        "cheap": "haiku",
    },
}

# Default argv for each subscription CLI, overridable via env.
#   codex:    `codex exec -` — the trailing "-" tells Codex to read the whole
#             prompt from stdin (the hang-proof, documented non-interactive form).
#   grok-cli: `grok --prompt <text>` — bare `grok` opens an interactive TUI and
#             ignores stdin, so the prompt must be passed as an ARGUMENT after
#             --prompt (see STDIN_FALSE_PROVIDERS / make_llm). Argv prompts are
#             capped by the OS command line (~32 KB on Windows); a {promptfile}
#             placeholder in the command feeds the prompt via a temp file
#             instead (see llm_cli.PROMPT_FILE_TOKEN).
#   cli:      no default — must be supplied via BOOKWRITER_CLI_CMD.
_CLI_DEFAULTS = {
    "codex": ["codex", "exec", "-"],
    "grok-cli": ["grok", "--prompt"],
    "cli": [],
}

# Providers whose CLI does NOT read the prompt from stdin — the prompt is appended
# as a positional argument instead (see GenericCliLLM(stdin=False)).
STDIN_FALSE_PROVIDERS = frozenset({"grok-cli"})

_CLI_CMD_ENV = {
    "codex": "BOOKWRITER_CODEX_CMD",
    "grok-cli": "BOOKWRITER_GROK_CMD",
    "cli": "BOOKWRITER_CLI_CMD",
}


def cli_cmd_env(provider: str) -> str:
    """The env var that configures *provider*'s CLI command."""
    return _CLI_CMD_ENV.get(provider, "BOOKWRITER_CLI_CMD")


def _strip_outer_quotes(token: str) -> str:
    """Drop one matched pair of surrounding quotes from a token.

    Non-POSIX shlex keeps quote characters inside tokens, so on Windows
    ``"C:\\Program Files\\Grok\\grok.exe" --prompt`` would otherwise yield an
    argv[0] with literal quotes — which shutil.which() and CreateProcess both
    reject. Quoted paths are the normal way to write space-containing paths.
    """
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def cli_command(provider: str) -> List[str]:
    """Resolve the argv for a CLI-backed provider (env override wins)."""
    override = rc.getenv(_CLI_CMD_ENV.get(provider, ""))
    if override:
        if os.name == "nt":
            # posix=False keeps backslash paths intact but retains the quotes.
            return [_strip_outer_quotes(t) for t in shlex.split(override, posix=False)]
        return shlex.split(override)
    return list(_CLI_DEFAULTS.get(provider, []))

_ENV_BY_TIER = {
    "strong": "BOOKWRITER_MODEL_STRONG",
    "mid": "BOOKWRITER_MODEL_MID",
    "cheap": "BOOKWRITER_MODEL_CHEAP",
}


def target_model(provider: str, anthropic_model: str) -> str:
    """Translate an Anthropic profile model id into the id to send to *provider*."""
    tier = tier_of(anthropic_model)
    override = rc.getenv(_ENV_BY_TIER[tier])
    if override:
        return override
    table = _PROVIDER_DEFAULTS.get(provider)
    if not table:
        return anthropic_model
    return table[tier]


# Approximate prices for the default OpenAI/OpenRouter model ids so the cost
# meter is representative rather than zero. Unknown ids cost 0 (see costs.py),
# which is harmless. Override pricing by editing MODEL_PRICES if needed.
_EXTRA_PRICES = {
    "gpt-4.1": ModelPrice.from_base(2.0, 8.0),
    "gpt-4.1-mini": ModelPrice.from_base(0.4, 1.6),
    "gpt-4.1-nano": ModelPrice.from_base(0.1, 0.4),
    "openai/gpt-4.1": ModelPrice.from_base(2.0, 8.0),
    "openai/gpt-4.1-mini": ModelPrice.from_base(0.4, 1.6),
    # Legacy gpt-4o family kept priced so older books / overrides still meter.
    "gpt-4o": ModelPrice.from_base(2.5, 10.0),
    "gpt-4o-mini": ModelPrice.from_base(0.15, 0.6),
    "openai/gpt-4o": ModelPrice.from_base(2.5, 10.0),
    "openai/gpt-4o-mini": ModelPrice.from_base(0.15, 0.6),
    # Grok (xAI) — approximate published pricing; override in Settings if it moves.
    "grok-4": ModelPrice.from_base(3.0, 15.0),
    "grok-3": ModelPrice.from_base(3.0, 15.0),
    "grok-3-mini": ModelPrice.from_base(0.3, 0.5),
    # The rest of the OpenRouter catalog — every model the UI offers must be
    # priced, or the cost meter silently reports $0 for real per-token spend.
    "anthropic/claude-sonnet-4.6": ModelPrice.from_base(3.0, 15.0),
    "x-ai/grok-4": ModelPrice.from_base(3.0, 15.0),
    "meta-llama/llama-3.3-70b-instruct": ModelPrice.from_base(0.12, 0.3),
}
for _mid, _price in _EXTRA_PRICES.items():
    MODEL_PRICES.setdefault(_mid, _price)


# --------------------------------------------------------------------------- #
# Live-availability gate (can a non-mock run actually proceed?)
# --------------------------------------------------------------------------- #

def _claude_binary() -> Optional[str]:
    return rc.getenv("BOOKWRITER_CLAUDE_BIN") or shutil.which("claude")


# xAI's OpenAI-compatible endpoint + key (GROK_API_KEY preferred, XAI_API_KEY ok).
GROK_BASE_URL = "https://api.x.ai/v1"


def _grok_key() -> Optional[str]:
    return rc.getenv("GROK_API_KEY") or rc.getenv("XAI_API_KEY")


def live_available(provider: Optional[str] = None) -> bool:
    """True when the selected provider has the credentials/binary it needs."""
    p = provider or provider_name()
    if p == "openai":
        return bool(rc.getenv("OPENAI_API_KEY"))
    if p == "openrouter":
        return bool(rc.getenv("OPENROUTER_API_KEY"))
    if p == "grok":
        return bool(_grok_key())
    if p == "claude-cli":
        return _claude_binary() is not None
    if p in _CLI_PROVIDERS:  # codex / grok-cli / cli
        cmd = cli_command(p)
        return bool(cmd) and shutil.which(cmd[0]) is not None
    # anthropic (default / unknown)
    return bool(rc.getenv("ANTHROPIC_API_KEY"))


def _http_status(url: str, headers: dict, timeout: float = 12.0) -> tuple:
    """(status_code or None, error_text). Stdlib only."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read()[:200].decode("utf-8", "replace") if e.fp else "")
    except Exception as e:  # noqa: BLE001 - network/DNS/timeout
        return None, str(e)


def verify(provider: Optional[str] = None) -> dict:
    """Actively check whether the selected provider's account is usable.

    Returns ``{"ok": bool, "detail": str}``. API providers make a cheap
    authenticated GET; subscription CLIs check the signed-in binary is present.
    """
    p = provider or provider_name()
    if p in ("openai", "openrouter") and not live_available(p):
        return {"ok": False, "detail": "No API key set."}
    if p == "anthropic":
        key = rc.getenv("ANTHROPIC_API_KEY")
        if not key:
            return {"ok": False, "detail": "No API key set."}
        code, err = _http_status("https://api.anthropic.com/v1/models",
                                  {"x-api-key": key, "anthropic-version": "2023-06-01"})
        if code == 200:
            return {"ok": True, "detail": "Anthropic API reachable — key valid."}
        if code in (401, 403):
            return {"ok": False, "detail": "Key rejected (401/403)."}
        return {"ok": False, "detail": err or f"HTTP {code}."}
    if p == "openai":
        base = (rc.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
        code, err = _http_status(f"{base}/models",
                                  {"Authorization": f"Bearer {rc.getenv('OPENAI_API_KEY')}"})
        return ({"ok": True, "detail": "OpenAI API reachable — key valid."} if code == 200
                else {"ok": False, "detail": ("Key rejected." if code in (401, 403) else err or f"HTTP {code}.")})
    if p == "openrouter":
        code, err = _http_status("https://openrouter.ai/api/v1/key",
                                  {"Authorization": f"Bearer {rc.getenv('OPENROUTER_API_KEY')}"})
        return ({"ok": True, "detail": "OpenRouter reachable — key valid."} if code == 200
                else {"ok": False, "detail": ("Key rejected." if code in (401, 403) else err or f"HTTP {code}.")})
    if p == "grok":
        if not _grok_key():
            return {"ok": False, "detail": "No GROK_API_KEY set."}
        code, err = _http_status(f"{GROK_BASE_URL}/models",
                                  {"Authorization": f"Bearer {_grok_key()}"})
        return ({"ok": True, "detail": "Grok (xAI) reachable — key valid."} if code == 200
                else {"ok": False, "detail": ("Key rejected." if code in (401, 403) else err or f"HTTP {code}.")})
    if p in _CLI_PROVIDERS:
        if p == "claude-cli":
            found = _claude_binary()
            label = "claude"
        else:
            cmd = cli_command(p)
            found = (shutil.which(cmd[0]) if cmd else None)
            label = " ".join(cmd) if cmd else "(no command configured)"
        if found:
            return {"ok": True, "detail": f"CLI found: {label}. Make sure it's signed in to your subscription."}
        return {"ok": False, "detail": f"CLI not found on PATH: {label}."}
    return {"ok": False, "detail": f"Unknown provider '{p}'."}


def missing_credentials_message(provider: Optional[str] = None) -> str:
    p = provider or provider_name()
    hints = {
        "openai": "set OPENAI_API_KEY",
        "openrouter": "set OPENROUTER_API_KEY",
        "grok": "set GROK_API_KEY (your xAI API key)",
        "claude-cli": "install and log in to the Claude Code CLI (`claude`)",
        "codex": "install the OpenAI Codex CLI and sign in with ChatGPT (`codex login`)",
        "grok-cli": "install the Grok CLI and give it an xAI key (GROK_API_KEY) "
                    "(set BOOKWRITER_GROK_CMD if its command isn't `grok --prompt`)",
        "cli": "set BOOKWRITER_CLI_CMD to your signed-in CLI command",
        "anthropic": "set ANTHROPIC_API_KEY",
    }
    return (
        f"No credentials for LLM provider '{p}'; enable demo mode (mock) or "
        f"{hints.get(p, 'set ANTHROPIC_API_KEY')}."
    )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def make_llm(mock: bool = False, provider: Optional[str] = None,
             model: Optional[str] = None):
    """Construct the LLM backend.

    ``provider`` overrides ``BOOKWRITER_LLM_PROVIDER`` (used for per-book picks);
    ``model`` is an optional per-book "Text model" applied to the prose stages.
    Backends are imported lazily so the package (and the mock path) keep working
    without the optional ``anthropic`` / ``openai`` SDKs installed.
    """
    if mock:
        from .mock import MockLLM
        return MockLLM()

    p = normalize_provider(provider) if provider else provider_name()
    model = model or None
    if p == "openai":
        from .llm_openai import OpenAICompatLLM
        return OpenAICompatLLM(
            api_key=rc.getenv("OPENAI_API_KEY"),
            base_url=rc.getenv("OPENAI_BASE_URL"),
            provider="openai",
            model_override=model,
        )
    if p == "openrouter":
        from .llm_openai import OpenAICompatLLM
        return OpenAICompatLLM(
            api_key=rc.getenv("OPENROUTER_API_KEY"),
            base_url=rc.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            provider="openrouter",
            model_override=model,
        )
    if p == "grok":
        from .llm_openai import OpenAICompatLLM
        return OpenAICompatLLM(
            api_key=_grok_key(),
            base_url=rc.getenv("GROK_BASE_URL", GROK_BASE_URL),
            provider="grok",
            model_override=model,
        )
    if p == "claude-cli":
        from .llm_claude_cli import ClaudeCliLLM
        return ClaudeCliLLM(binary=_claude_binary(), model_override=model)
    if p in _CLI_PROVIDERS:  # codex / grok-cli / cli
        from .llm_cli import GenericCliLLM
        model_flag = rc.getenv("BOOKWRITER_CLI_MODEL_FLAG") or None
        return GenericCliLLM(command=cli_command(p), provider=p,
                             model_flag=model_flag, model_override=model,
                             stdin=(p not in STDIN_FALSE_PROVIDERS))

    from .llm import AnthropicLLM
    return AnthropicLLM(api_key=rc.getenv("ANTHROPIC_API_KEY"), model_override=model)


# --------------------------------------------------------------------------- #
# Catalog — what the UI shows in the provider / text-model pickers
# --------------------------------------------------------------------------- #

_PROVIDER_LABELS = {
    "anthropic": "Anthropic API",
    "openai": "OpenAI API",
    "openrouter": "OpenRouter",
    "grok": "Grok — xAI API",
    "claude-cli": "Claude (subscription)",
    "codex": "ChatGPT — Codex (subscription)",
    "grok-cli": "Grok CLI (subscription)",
    "cli": "Custom CLI",
}

# Ordered (id, label) per provider. The first entry is the default selection.
MODEL_CATALOG = {
    "anthropic": [
        ("claude-opus-4-8", "Opus 4.8 — best quality"),
        ("claude-sonnet-4-6", "Sonnet 4.6 — balanced"),
        ("claude-haiku-4-5", "Haiku 4.5 — fastest & cheapest"),
    ],
    "openai": [
        ("gpt-4.1", "GPT-4.1 — best quality"),
        ("gpt-4.1-mini", "GPT-4.1 mini — balanced"),
        ("gpt-4.1-nano", "GPT-4.1 nano — fastest & cheapest"),
    ],
    "openrouter": [
        ("openai/gpt-4.1", "GPT-4.1"),
        ("openai/gpt-4.1-mini", "GPT-4.1 mini"),
        # claude-3.7-sonnet was retired by Anthropic (2026-02) — offer a live model.
        ("anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6"),
        ("x-ai/grok-4", "Grok 4 (xAI)"),
        ("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B"),
    ],
    "grok": [
        ("grok-4", "Grok 4 — best quality"),
        ("grok-3", "Grok 3 — balanced"),
        ("grok-3-mini", "Grok 3 mini — fast & cheap"),
    ],
    "claude-cli": [
        ("opus", "Claude Opus (subscription)"),
        ("sonnet", "Claude Sonnet (subscription)"),
        ("haiku", "Claude Haiku (subscription)"),
    ],
    "codex": [("", "Codex default (your ChatGPT model)")],
    "grok-cli": [("", "Grok default (your subscription model)")],
    "cli": [("", "CLI default")],
}

_CATALOG_ORDER = ["anthropic", "openai", "openrouter", "grok", "claude-cli", "codex", "grok-cli", "cli"]


def provider_catalog() -> dict:
    """Provider + model options for the create-book UI, with availability flags."""
    providers = []
    for pid in _CATALOG_ORDER:
        providers.append({
            "id": pid,
            "label": _PROVIDER_LABELS.get(pid, pid),
            "kind": "subscription" if pid in _CLI_PROVIDERS else "api",
            "available": live_available(pid),
            "models": [{"id": m, "label": lbl} for m, lbl in MODEL_CATALOG.get(pid, [])],
        })
    return {"providers": providers, "current": provider_name()}
