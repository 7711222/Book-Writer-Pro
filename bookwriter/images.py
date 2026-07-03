"""Chapter image generation — a pluggable image-provider layer.

Mirrors the LLM provider design: one env var picks the backend, and each backend
implements a tiny ``ImageProvider`` protocol (``generate(prompt) -> (bytes, ext)``).
The default is **Pixio** (set ``PIXIO_API_KEY``); a user can point at any other
image API instead.

    BOOKWRITER_IMAGE_PROVIDER selects the backend (default: pixio)
      pixio   (default)  Pixio API           -> PIXIO_API_KEY
      openai             OpenAI Images API   -> OPENAI_API_KEY
      http               ANY image HTTP API  -> BOOKWRITER_IMAGE_URL (+ auth/body/result)

Everything here uses the Python standard library only (urllib) so the core
package keeps its "zero third-party installs" promise — no SDKs required.

When the toggle is on but no image provider is configured (or a call fails), the
pipeline logs a note and writes the chapter without an image; image generation
is best-effort and never blocks the prose.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from . import runtime_config as rc

PIXIO_BASE = "https://beta.pixio.myapps.ai"


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #

_ALIASES = {
    "pixio": "pixio",
    "openai": "openai",
    "dalle": "openai",
    "dall-e": "openai",
    "http": "http",
    "custom": "http",
    "generic": "http",
}

DEFAULT_IMAGE_PROVIDER = "pixio"


def image_provider_name() -> str:
    raw = (rc.getenv("BOOKWRITER_IMAGE_PROVIDER") or DEFAULT_IMAGE_PROVIDER).strip().lower()
    return _ALIASES.get(raw, raw)


def image_available(provider: Optional[str] = None) -> bool:
    """True when the selected image backend has the credentials/config it needs."""
    p = provider or image_provider_name()
    if p == "pixio":
        return bool(rc.getenv("PIXIO_API_KEY"))
    if p == "openai":
        return bool(rc.getenv("OPENAI_API_KEY"))
    if p == "http":
        return bool(rc.getenv("BOOKWRITER_IMAGE_URL"))
    return False


def image_status() -> dict:
    """Small dict for the UI: which image backend is active and is it usable."""
    p = image_provider_name()
    return {"provider": p, "available": image_available(p)}


def verify(provider: Optional[str] = None) -> dict:
    """Actively check the image backend. {"ok": bool, "detail": str}."""
    p = provider or image_provider_name()
    if p == "pixio":
        key = rc.getenv("PIXIO_API_KEY")
        if not key:
            return {"ok": False, "detail": "No PIXIO_API_KEY set."}
        try:
            data = _get_json(f"{PIXIO_BASE}/api/v1/credits", {"Authorization": f"Bearer {key}"}, timeout=12.0)
            bal = data.get("credits", data.get("balance", "?"))
            return {"ok": True, "detail": f"Pixio reachable — credits: {bal}."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)[:200]}
    if p == "openai":
        if not rc.getenv("OPENAI_API_KEY"):
            return {"ok": False, "detail": "No OPENAI_API_KEY set."}
        from . import provider as _prov
        return _prov.verify("openai")
    if p == "http":
        url = rc.getenv("BOOKWRITER_IMAGE_URL")
        # Config-only check: we don't POST a real generation here, so make clear
        # this is not a live reachability/auth test (unlike pixio/openai above).
        return ({"ok": True, "detail": f"Endpoint configured (not live-tested): {url}"} if url
                else {"ok": False, "detail": "BOOKWRITER_IMAGE_URL not set."})
    return {"ok": False, "detail": f"Unknown image provider '{p}'."}


def make_image_provider(provider: Optional[str] = None) -> "ImageProvider":
    p = (provider or image_provider_name())
    p = _ALIASES.get(p.strip().lower(), p)
    if p == "openai":
        return OpenAIImageProvider()
    if p == "http":
        return HttpImageProvider()
    return PixioImageProvider()


# --------------------------------------------------------------------------- #
# Small HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #

# Hard cap on any response body read from an image endpoint — a hostile or
# misconfigured endpoint must not be able to exhaust memory mid-write.
MAX_FETCH_BYTES = 25 * 1024 * 1024


def _allow_private() -> bool:
    return (rc.getenv("BOOKWRITER_IMAGE_ALLOW_PRIVATE") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _check_url(url: str) -> None:
    """Refuse anything but http(s) and, unless the user opts in, hosts that
    resolve to private/loopback/link-local addresses.

    Both the endpoint (BOOKWRITER_IMAGE_URL) and the image URL its response
    points at are settings-controlled, so without this check the server could
    be steered at cloud metadata (169.254.169.254) or intranet hosts (SSRF) —
    or handed a file:// URL we'd save as a "chapter image".
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(f"Refusing non-http(s) image URL: {url!r}")
    if _allow_private():
        return
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise RuntimeError(f"Could not resolve image host {host!r}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RuntimeError(
                f"Refusing image URL {url!r}: host resolves to a private/internal "
                f"address ({ip}). Set BOOKWRITER_IMAGE_ALLOW_PRIVATE=1 to allow "
                f"local/intranet image endpoints."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target — even a 'trusted' endpoint could 302
    to file:// or an internal address otherwise."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirectHandler)


def _read_capped(resp, url: str) -> bytes:
    """Read a response body, aborting past MAX_FETCH_BYTES (unbounded-read DoS)."""
    cap = MAX_FETCH_BYTES
    length = (resp.headers.get("Content-Length") or "").strip()
    if length.isdigit() and int(length) > cap:
        raise RuntimeError(
            f"Response from {url} declares {int(length):,} bytes — over the "
            f"{cap // (1024 * 1024)} MB image cap."
        )
    body = resp.read(cap + 1)
    if len(body) > cap:
        raise RuntimeError(
            f"Response from {url} exceeded the {cap // (1024 * 1024)} MB image cap."
        )
    return body


def _request(method: str, url: str, *, headers: dict, data: Optional[bytes] = None,
             timeout: float = 60.0) -> Tuple[int, bytes]:
    _check_url(url)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.getcode(), _read_capped(resp, url)
    except urllib.error.HTTPError as e:
        # Error bodies are only ever used for short diagnostics — cap likewise.
        return e.code, e.read(65536)


def _get_json(url: str, headers: dict, timeout: float = 60.0):
    code, body = _request("GET", url, headers=headers, timeout=timeout)
    if code >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {code}: {body[:300]!r}")
    return json.loads(body.decode("utf-8"))


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 60.0):
    h = {"Content-Type": "application/json", **headers}
    code, body = _request("POST", url, headers=h, data=json.dumps(payload).encode("utf-8"), timeout=timeout)
    if code == 402:
        raise RuntimeError("Image provider returned 402 — insufficient credits.")
    if code == 429:
        raise RuntimeError("Image provider returned 429 — rate/concurrency limit.")
    if code >= 400:
        raise RuntimeError(f"POST {url} -> HTTP {code}: {body[:300]!r}")
    return json.loads(body.decode("utf-8"))


def _download(url: str, timeout: float = 60.0) -> bytes:
    code, body = _request("GET", url, headers={}, timeout=timeout)
    if code >= 400:
        raise RuntimeError(f"download {url} -> HTTP {code}")
    return body


def _ext_from_url(url: str, default: str = "png") -> str:
    tail = url.split("?")[0].rsplit(".", 1)
    if len(tail) == 2 and 1 <= len(tail[1]) <= 5:
        return tail[1].lower()
    return default


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class PixioImageProvider:
    """Default backend. Uses the Pixio public API (see the pixio-skill).

    Model id comes from ``BOOKWRITER_PIXIO_MODEL`` / ``PIXIO_IMAGE_MODEL``; if
    unset it is auto-discovered from ``GET /api/v1/models`` (first text-to-image
    model) so a bare ``PIXIO_API_KEY`` works with zero extra config.
    """

    _discovered_model: Optional[str] = None  # class-level cache across chapters

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or rc.getenv("PIXIO_API_KEY") or ""
        self.model = model or rc.getenv("BOOKWRITER_PIXIO_MODEL") or rc.getenv("PIXIO_IMAGE_MODEL") or ""
        self.aspect = rc.getenv("BOOKWRITER_IMAGE_ASPECT", "3:2")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _resolve_model(self) -> str:
        if self.model:
            return self.model
        if PixioImageProvider._discovered_model:
            return PixioImageProvider._discovered_model
        data = _get_json(f"{PIXIO_BASE}/api/v1/models", self._headers())
        models = data.get("models", []) or []
        # Prefer a pure text-to-image model; fall back to any non-edit image model.
        def score(m):
            t = (m.get("type") or "").lower()
            if t == "text-to-image":
                return 0
            if "image" in t and "edit" not in t and "to-image" not in t:
                return 1
            if "image" in t and "edit" not in t:
                return 2
            return 9
        usable = sorted((m for m in models if "image" in (m.get("type") or "").lower()
                         and "edit" not in (m.get("type") or "").lower()), key=score)
        if not usable:
            raise RuntimeError("No text-to-image model is visible to this Pixio account.")
        PixioImageProvider._discovered_model = usable[0]["id"]
        return PixioImageProvider._discovered_model

    def generate(self, prompt: str, *, timeout: float = 180.0) -> Tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("PIXIO_API_KEY is not set.")
        model = self._resolve_model()
        started = _post_json(
            f"{PIXIO_BASE}/api/v1/generate",
            {"providerId": "pixio", "modelId": model,
             "params": {"prompt": prompt, "aspect_ratio": self.aspect, "output_format": "png"}},
            self._headers(),
        )
        cid = started.get("contentId") or started.get("id")
        if not cid:
            raise RuntimeError(f"Pixio generate returned no contentId: {started}")
        deadline = time.monotonic() + timeout
        delay = 1.5
        while True:
            gen = _get_json(f"{PIXIO_BASE}/api/v1/generations/{cid}", self._headers())
            status = (gen.get("status") or "").lower()
            if status == "succeeded":
                url = gen.get("outputUrl") or (gen.get("outputs") or {}).get("imageUrl")
                if not url:
                    raise RuntimeError("Pixio generation succeeded but returned no image URL.")
                return _download(url), _ext_from_url(url)
            if status == "failed":
                raise RuntimeError(f"Pixio generation failed: {gen.get('error')}")
            if time.monotonic() > deadline:
                raise RuntimeError("Pixio generation timed out.")
            time.sleep(delay)
            delay = min(delay * 1.3, 6.0)


class OpenAIImageProvider:
    """OpenAI Images API via REST (no SDK needed). Model: BOOKWRITER_OPENAI_IMAGE_MODEL
    (default gpt-image-1)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or rc.getenv("OPENAI_API_KEY") or ""
        self.model = model or rc.getenv("BOOKWRITER_OPENAI_IMAGE_MODEL") or "gpt-image-1"
        self.size = rc.getenv("BOOKWRITER_OPENAI_IMAGE_SIZE", "1536x1024")

    def generate(self, prompt: str, *, timeout: float = 180.0) -> Tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        base = rc.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        resp = _post_json(
            f"{base}/images/generations",
            {"model": self.model, "prompt": prompt, "size": self.size, "n": 1},
            {"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )
        item = (resp.get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), "png"
        if item.get("url"):
            return _download(item["url"]), _ext_from_url(item["url"])
        raise RuntimeError(f"OpenAI image response had no image: {resp}")


class HttpImageProvider:
    """Generic backend for ANY image HTTP API, configured by env:

      BOOKWRITER_IMAGE_URL          POST endpoint (required)
      BOOKWRITER_IMAGE_AUTH         value for the Authorization header (optional)
      BOOKWRITER_IMAGE_BODY         JSON body template; the literal {prompt} is
                                    replaced with the (JSON-escaped) prompt.
                                    Default: {"prompt": "{prompt}"}
      BOOKWRITER_IMAGE_RESULT_PATH  dot-path to the image URL in the JSON response
                                    (e.g. "data.0.url"). If it instead points to
                                    base64 data, set BOOKWRITER_IMAGE_RESULT_B64=1.
    """

    def __init__(self):
        self.url = rc.getenv("BOOKWRITER_IMAGE_URL") or ""
        self.auth = rc.getenv("BOOKWRITER_IMAGE_AUTH") or ""
        self.body_tpl = rc.getenv("BOOKWRITER_IMAGE_BODY") or '{"prompt": "{prompt}"}'
        self.result_path = rc.getenv("BOOKWRITER_IMAGE_RESULT_PATH") or "url"
        # Parse as a real boolean: bool("0")/bool("false") are both True, so
        # `=0`/`=false` (meaning "off") must not accidentally enable base64 mode.
        self.result_b64 = (rc.getenv("BOOKWRITER_IMAGE_RESULT_B64") or "").strip().lower() \
            in ("1", "true", "yes", "on")

    def generate(self, prompt: str, *, timeout: float = 180.0) -> Tuple[bytes, str]:
        if not self.url:
            raise RuntimeError("BOOKWRITER_IMAGE_URL is not set.")
        # Substitute the prompt safely (json.dumps gives a quoted string; strip
        # the surrounding quotes so it slots into the template's existing quotes).
        safe = json.dumps(prompt)[1:-1]
        payload = json.loads(self.body_tpl.replace("{prompt}", safe))
        headers = {}
        if self.auth:
            headers["Authorization"] = self.auth
        resp = _post_json(self.url, payload, headers, timeout=timeout)
        val = _dig(resp, self.result_path)
        if val is None:
            raise RuntimeError(f"Image API response had nothing at '{self.result_path}'.")
        if self.result_b64:
            return base64.b64decode(val), "png"
        return _download(str(val)), _ext_from_url(str(val))


def _dig(obj, path: str):
    """Walk a dot-path like 'data.0.url' through dicts/lists."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


# Type alias for readability (any object with the generate() method above).
ImageProvider = object


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

def build_chapter_prompt(bible, plan) -> str:
    """A compact, spoiler-light visual prompt for one chapter's illustration.

    Pulls the chapter title/purpose, the POV character's look, and the primary
    location from the bible so the image stays on-model with the book.
    """
    bits = [plan.title]
    if getattr(plan, "purpose", ""):
        bits.append(plan.purpose)
    pov = bible.character(plan.pov_character) if getattr(plan, "pov_character", "") else None
    if pov and getattr(pov, "appearance", ""):
        bits.append(f"Featured figure — {pov.name}: {pov.appearance}")
    for lid in (getattr(plan, "location_ids", []) or [])[:1]:
        loc = bible.location(lid)
        if loc and loc.description:
            bits.append(f"Setting — {loc.name}: {loc.description}")
    subject = ". ".join(b for b in bits if b)

    mood = ", ".join(x for x in [bible.genre, bible.tone] if x) or "literary, atmospheric"
    prompt = (
        f"Editorial book illustration for the chapter “{plan.title}”. "
        f"{subject}. Mood: {mood}. "
        "Painterly, cinematic lighting, cohesive book-illustration style. "
        "No text, no words, no lettering, no captions, no borders."
    )
    return prompt[:1100]


def build_cover_prompt(bible) -> str:
    """A striking, text-free front-cover ART prompt from the story bible.

    We deliberately ask for NO text/lettering — the title and author are overlaid
    as crisp typography afterward (see kdp.compose_cover_svg), which is far more
    legible and on-brand than letting the model render words.
    """
    mood = ", ".join(x for x in [bible.genre, bible.tone] if x) or "literary, atmospheric"
    themes = ", ".join((bible.themes or [])[:3])
    bits = []
    if getattr(bible, "logline", ""):
        bits.append(bible.logline)
    elif getattr(bible, "premise", ""):
        bits.append(bible.premise[:300])
    if themes:
        bits.append(f"Themes: {themes}")
    subject = " ".join(bits)
    prompt = (
        "Striking, professional BOOK COVER ART, portrait orientation, single bold "
        "focal image with strong silhouette and dramatic composition that reads at "
        f"thumbnail size. {subject}. Genre & mood: {mood}. Rich, art-directed colour "
        "palette, cinematic lighting, premium publishing aesthetic, lots of clean "
        "negative space at the top and bottom for a title and author name. "
        "Absolutely NO text, NO words, NO letters, NO title, NO typography, no logos, "
        "no watermark, no borders, no frame."
    )
    return prompt[:1100]


# Portrait aspect/size hints for a 1600x2560-ish (~1:1.6) book cover.
_COVER_ASPECT = "2:3"
_COVER_OPENAI_SIZE = "1024x1536"


def generate_cover_art(bible, provider: Optional[str] = None,
                       *, timeout: float = 180.0) -> Tuple[bytes, str]:
    """Generate front-cover ARTWORK (no text) via the configured image backend.

    Returns ``(image_bytes, ext)``. Raises if no backend is configured/usable —
    callers surface that as a clear message. Forces a portrait cover aspect.
    """
    prov = make_image_provider(provider)
    # Override the per-call aspect/size to portrait for a book cover (duck-typed:
    # each backend exposes the relevant knob).
    if hasattr(prov, "aspect"):
        prov.aspect = _COVER_ASPECT
    if hasattr(prov, "size"):
        prov.size = _COVER_OPENAI_SIZE
    return prov.generate(build_cover_prompt(bible), timeout=timeout)
