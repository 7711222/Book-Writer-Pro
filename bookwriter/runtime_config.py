"""Runtime credential / setting store — the backing for the in-app Settings panel.

Providers historically read keys straight from ``os.environ`` (set before launch).
That left no way to authenticate from the UI. This module adds a small persisted
override layer on top of the environment:

    getenv(name)  ->  in-memory/persisted override  ELSE  os.environ

The server binds a JSON file (``<data_dir>/settings.json``) at startup and the
Settings endpoints write to it. Because every provider reads via ``getenv`` at
call time, saving a key takes effect immediately for the next generation — no
restart, no env vars.

Security note: this is a local-first, single-user app served on 127.0.0.1. Keys
are stored as plaintext JSON in the data dir; the file is restricted to the
current user (chmod 600 on POSIX, an icacls owner-only ACL on Windows) and keys
are never returned to the client in full or logged — the API exposes only
masked hints.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Dict, List, Optional

_lock = threading.RLock()
_overrides: Dict[str, str] = {}
_path: Optional[str] = None

# Secret keys — surfaced to the UI only as {set, masked}, never echoed in full.
SECRET_KEYS: List[str] = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "PIXIO_API_KEY",
    "BOOKWRITER_IMAGE_AUTH",
]

# Non-secret options the UI may read back and edit verbatim.
OPTION_KEYS: List[str] = [
    "BOOKWRITER_LLM_PROVIDER",
    "BOOKWRITER_IMAGE_PROVIDER",
    "BOOKWRITER_MODEL_STRONG",
    "BOOKWRITER_MODEL_MID",
    "BOOKWRITER_MODEL_CHEAP",
    "BOOKWRITER_CLAUDE_BIN",
    "BOOKWRITER_CODEX_CMD",
    "BOOKWRITER_GROK_CMD",
    "BOOKWRITER_CLI_CMD",
    "BOOKWRITER_CLI_MODEL_FLAG",
    "BOOKWRITER_CLI_TIMEOUT",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "GROK_BASE_URL",
    "BOOKWRITER_PIXIO_MODEL",
    "PIXIO_IMAGE_MODEL",  # legacy alias for BOOKWRITER_PIXIO_MODEL (images.py fallback)
    "BOOKWRITER_IMAGE_ASPECT",
    "BOOKWRITER_OPENAI_IMAGE_MODEL",
    "BOOKWRITER_OPENAI_IMAGE_SIZE",
    "BOOKWRITER_IMAGE_URL",
    "BOOKWRITER_IMAGE_BODY",
    "BOOKWRITER_IMAGE_RESULT_PATH",
    "BOOKWRITER_IMAGE_RESULT_B64",
    "BOOKWRITER_IMAGE_ALLOW_PRIVATE",
]

MANAGED_KEYS: List[str] = SECRET_KEYS + OPTION_KEYS


# --------------------------------------------------------------------------- #

def bind_file(path: str) -> None:
    """Point the store at a JSON file and load any saved overrides."""
    global _path
    _path = path
    load()


def load() -> None:
    if not _path or not os.path.exists(_path):
        return
    try:
        with open(_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if isinstance(data, dict):
        with _lock:
            for k, v in data.items():
                if isinstance(v, str) and v != "":
                    _overrides[k] = v


def save() -> None:
    if not _path:
        return
    with _lock:
        data = dict(_overrides)
    d = os.path.dirname(_path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = _path + ".tmp"
    # Create the temp file empty and restrict it BEFORE any secret bytes land in
    # it; os.replace keeps the tightened permissions on the final file.
    with open(tmp, "w", encoding="utf-8"):
        pass
    _restrict_to_owner(tmp)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _path)
    _restrict_to_owner(_path)


_win_user_sid: Optional[str] = None  # cached; one `whoami` per process


def _current_user_sid() -> Optional[str]:
    """The current Windows user's SID — icacls grants by SID are unambiguous,
    whereas a bare username can resolve to the machine/domain and lock the
    file away from everyone (including us)."""
    global _win_user_sid
    if _win_user_sid:
        return _win_user_sid
    try:
        out = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, check=False, timeout=20,
        )
        sid = out.stdout.strip().rsplit(",", 1)[-1].strip().strip('"')
        if sid.upper().startswith("S-1-"):
            _win_user_sid = sid
            return sid
    except Exception:  # noqa: BLE001 - whoami missing/blocked -> best-effort
        pass
    return None


def _restrict_to_owner(path: str) -> None:
    """Best-effort: the file holds plaintext API keys, so keep it readable by
    the current user only. POSIX: chmod 600. Windows: chmod cannot restrict
    reads, so strip ACL inheritance and grant only the current user (icacls).
    """
    if os.name == "nt":
        sid = _current_user_sid()
        if not sid:
            return
        try:
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"*{sid}:F"],
                capture_output=True, check=False, timeout=20,
            )
            with open(path, "rb"):
                pass  # confirm we still have access
        except OSError:
            # Never lock the app out of its own settings — restore the
            # inherited permissions (the owner always keeps WRITE_DAC).
            try:
                subprocess.run(["icacls", path, "/reset"],
                               capture_output=True, check=False, timeout=20)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - icacls missing/blocked -> best-effort
            pass
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a setting: persisted override first, then the process environment."""
    with _lock:
        v = _overrides.get(name)
    if v is not None and v != "":
        return v
    return os.environ.get(name, default)


def set_values(values: Dict[str, Optional[str]], *, persist: bool = True) -> None:
    """Apply a batch of settings. An empty string or None clears the override
    (falling back to the environment / built-in default)."""
    with _lock:
        for k, v in values.items():
            if k not in MANAGED_KEYS:
                continue
            if v is None or v == "":
                _overrides.pop(k, None)
            else:
                _overrides[k] = str(v)
    if persist:
        save()


def is_set(name: str) -> bool:
    return bool(getenv(name))


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return value[0] + "…"
    return f"{value[:4]}…{value[-3:]}"


def public_state() -> dict:
    """Masked, secret-free snapshot for the Settings UI."""
    keys = {}
    for name in SECRET_KEYS:
        val = getenv(name) or ""
        keys[name] = {"set": bool(val), "masked": _mask(val)}
    options = {name: (getenv(name) or "") for name in OPTION_KEYS}
    return {"keys": keys, "options": options}
