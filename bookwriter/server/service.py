"""BookService — the application layer between the HTTP API and the core engine.

Owns the books data directory, meta.json bookkeeping, LLM/Settings construction
(mock vs live), and the background write-job lifecycle. Every method that the API
exposes routes through here; the API module only deals with HTTP concerns.

The write job runs ``BookPipeline.write_all`` on a daemon thread; its on_event
callback publishes structured events to the :class:`EventBroker`, which the SSE
endpoint replays-then-tails. Exactly one job per book id: the broker enforces it
in-process, and a cross-process file lock (``bookwriter.locks``) also excludes
writers in other processes (e.g. the MCP server) sharing the same data dir.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..config import (
    Settings,
    QUALITY_PROFILES,
    DEFAULT_PROFILE,
    MODEL_PRICES,
)
from ..costs import CostLedger
from ..kdp import (
    KdpMetadata,
    MAX_CONTRIBUTORS,
    build_epub,
    build_kdp_kit,
    generate_kdp_metadata,
    generate_marketing as _generate_marketing,
)
from ..locks import BookLockedError, book_write_lock
from ..print_export import build_docx, print_spec as _print_spec, build_print_cover_svg
from ..royalties import estimate_page_count, estimate_royalties
from ..pipeline import BookPipeline
from ..store import BookStore, _write_json
from .broker import EventBroker

logger = logging.getLogger(__name__)

# How long a quick mutator (edit/revise/append) waits for the cross-process
# write lock before returning 409. Long enough to ride out another quick op,
# short enough that colliding with a full write job fails fast.
_MUTATE_LOCK_TIMEOUT = 0.5


class ServiceError(Exception):
    """Raised for expected, client-facing failures; carries an HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "book"


def _display_filename(text: str) -> str:
    """Human-readable export filename stem: keeps Unicode letters (a Cyrillic
    title stays readable), strips path separators / control chars / quotes.
    The API layer emits it RFC 5987-encoded with an ASCII fallback."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", (text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120] or "book"


# The exact shape of an id we mint: a lowercase slug + "-" + 6 hex chars. We
# reject anything else, which structurally forbids path traversal (no "/", "\",
# "..", absolute paths, drive letters, NUL, etc.) before any os.path.join.
_BOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_book_id(book_id: str) -> str:
    """Return ``book_id`` if it is a safe, well-formed id, else raise 404.

    Used by every entry point that turns a caller-supplied id into a path, so
    read/write/delete all inherit containment. Raising 404 (not 400) avoids
    leaking whether a given path exists on disk.
    """
    if not isinstance(book_id, str) or not _BOOK_ID_RE.match(book_id):
        raise ServiceError(404, f"Book {book_id!r} not found.")
    return book_id


class BookService:
    def __init__(self, data_dir: str, broker: Optional[EventBroker] = None) -> None:
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.broker = broker or EventBroker()
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Paths / meta
    # ------------------------------------------------------------------ #
    def _book_dir(self, book_id: str) -> str:
        validate_book_id(book_id)
        return os.path.join(self.data_dir, book_id)

    def _meta_path(self, book_id: str) -> str:
        return os.path.join(self._book_dir(book_id), "meta.json")

    def _book_exists(self, book_id: str) -> bool:
        return os.path.isfile(self._meta_path(book_id))

    def _read_meta(self, book_id: str) -> Dict[str, Any]:
        with open(self._meta_path(book_id), "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_meta(self, book_id: str, meta: Dict[str, Any]) -> None:
        # Atomic write: a crash mid-write must never corrupt meta.json (a corrupt
        # meta makes the book un-listable -> effectively lost).
        os.makedirs(self._book_dir(book_id), exist_ok=True)
        _write_json(self._meta_path(book_id), meta)

    def _require_meta(self, book_id: str) -> Dict[str, Any]:
        if not self._book_exists(book_id):
            raise ServiceError(404, f"Book {book_id!r} not found.")
        return self._read_meta(book_id)

    @contextlib.contextmanager
    def _write_lock(self, book_id: str, timeout: float = _MUTATE_LOCK_TIMEOUT) -> Iterator[None]:
        """Cross-process writer lock for one book, as a 409 on contention.

        The broker's one-job-per-book guard is in-process memory only; this
        OS-level lock also excludes the MCP server (a separate process on the
        same data dir), so concurrent graph saves can't silently lose updates.
        """
        try:
            with book_write_lock(self._book_dir(book_id), timeout=timeout):
                yield
        except BookLockedError:
            raise ServiceError(
                409, "Another write is in progress for this book; try again shortly.")

    def _make_id(self, title: str) -> str:
        base = _slug(title)
        h = hashlib.sha256(f"{title}{time.time()}".encode("utf-8")).hexdigest()[:6]
        book_id = f"{base}-{h}"
        # Extremely unlikely collision guard.
        while self._book_exists(book_id):
            h = hashlib.sha256(f"{book_id}{time.time()}".encode("utf-8")).hexdigest()[:6]
            book_id = f"{base}-{h}"
        return book_id

    # ------------------------------------------------------------------ #
    # LLM / Settings construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def has_api_key() -> bool:
        # "Can a live (non-mock) run proceed?" — true for whichever provider is
        # configured (Anthropic API key, OpenAI/OpenRouter key, or the claude CLI).
        from ..provider import live_available
        return live_available()

    @staticmethod
    def _no_creds_msg(provider: Optional[str] = None) -> str:
        from ..provider import missing_credentials_message
        return missing_credentials_message(provider)

    @staticmethod
    def _live(provider: Optional[str] = None) -> bool:
        from ..provider import live_available
        return live_available(provider)

    def _make_llm(self, mock: bool, meta: Optional[Dict[str, Any]] = None):
        # Lazily dispatch to the configured provider so the server boots even
        # without any LLM SDK installed. A book's saved provider/model (from the
        # create modal) overrides the server's env default.
        from ..provider import make_llm

        meta = meta or {}
        return make_llm(
            mock=mock,
            provider=meta.get("provider") or None,
            model=meta.get("model") or None,
        )

    def _make_settings(self, meta: Dict[str, Any], book_id: str) -> Settings:
        profile = meta.get("profile", DEFAULT_PROFILE)
        if profile not in QUALITY_PROFILES:
            profile = DEFAULT_PROFILE
        s = Settings(project_dir=self._book_dir(book_id)).with_profile(profile)
        s.use_cache = bool(meta.get("use_cache", True))
        s.run_continuity_check = bool(meta.get("run_continuity_check", True))
        s.chapter_images = bool(meta.get("chapter_images", False))
        return s

    @staticmethod
    def _make_image_provider(meta: Dict[str, Any]):
        """An image backend for the write job, or None if this book didn't opt in
        or no provider is configured (then chapters are written without images)."""
        if not meta.get("chapter_images"):
            return None
        from ..images import image_available, make_image_provider
        if not image_available():
            return None
        try:
            return make_image_provider()
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Profiles
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Settings (in-app credential / provider configuration)
    # ------------------------------------------------------------------ #
    def get_settings(self) -> Dict[str, Any]:
        from .. import runtime_config as rc
        from ..provider import provider_catalog
        from ..images import image_status
        state = rc.public_state()
        cat = provider_catalog()
        return {
            "keys": state["keys"],
            "options": state["options"],
            "llm": {"selected": cat["current"], "providers": cat["providers"]},
            "image": image_status(),
        }

    def save_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        from .. import runtime_config as rc
        rc.set_values(values)
        return self.get_settings()

    @staticmethod
    def verify_provider(kind: str, provider: Optional[str]) -> Dict[str, Any]:
        if kind == "image":
            from ..images import verify as _verify_image
            return _verify_image(provider)
        from ..provider import verify as _verify_llm
        return _verify_llm(provider)

    # ------------------------------------------------------------------ #
    # Profiles
    # ------------------------------------------------------------------ #
    def profiles(self) -> Dict[str, Any]:
        # Contract-authoritative shape: plan/write/extract are BARE model
        # strings; only `check` is an object {model, effort}. This matches the
        # MCP server's _profiles_payload() so HTTP, MCP and the frontend all
        # agree on one shape.
        out: List[Dict[str, Any]] = []
        for name, p in QUALITY_PROFILES.items():
            prices: Dict[str, Dict[str, float]] = {}
            for sm in (p.plan, p.write, p.extract, p.check):
                price = MODEL_PRICES.get(sm.model)
                if price is not None:
                    prices[sm.model] = {"input": price.input, "output": price.output}
            stages = {
                "plan": p.plan.model,
                "write": p.write.model,
                "extract": p.extract.model,
                "check": {"model": p.check.model, "effort": p.check.effort},
            }
            out.append({"name": name, "stages": stages, "prices": prices})
        return {"default": DEFAULT_PROFILE, "profiles": out}

    # ------------------------------------------------------------------ #
    # Summaries
    # ------------------------------------------------------------------ #
    def _summary(self, book_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        store = BookStore(self._book_dir(book_id))
        bible = store.load_bible()
        chapters_total = len(bible.outline) if bible else 0
        chapters_written = 0
        words = 0
        if bible:
            for p in bible.outline:
                if store.has_chapter(p.number):
                    chapters_written += 1
                    try:
                        rec = self._load_chapter_record(store, p.number)
                        if rec is not None:
                            words += int(rec.get("word_count", 0) or 0)
                    except Exception:
                        pass
        return {
            "id": book_id,
            "title": meta.get("title") or (bible.title if bible else "") or "Untitled",
            "logline": meta.get("logline") or (bible.logline if bible else ""),
            "genre": meta.get("genre") or (bible.genre if bible else ""),
            "chapters_total": chapters_total,
            "chapters_written": chapters_written,
            "words": words,
            "created_at": meta.get("created_at", ""),
            "profile": meta.get("profile", DEFAULT_PROFILE),
            "mock": bool(meta.get("mock", False)),
        }

    def list_books(self) -> Dict[str, Any]:
        books: List[Dict[str, Any]] = []
        for entry in sorted(os.listdir(self.data_dir)):
            # The data dir also holds non-book entries (notably settings.json).
            # Skip anything that isn't a well-formed book id BEFORE it reaches
            # validate_book_id (which would raise 404 and kill the whole listing).
            if not _BOOK_ID_RE.match(entry):
                continue
            try:
                if not self._book_exists(entry):
                    continue
                meta = self._read_meta(entry)
                books.append(self._summary(entry, meta))
            except Exception:
                continue
        books.sort(key=lambda b: b.get("created_at", ""), reverse=True)
        return {"books": books}

    # ------------------------------------------------------------------ #
    # Create (plan synchronously)
    # ------------------------------------------------------------------ #
    def create_book(self, req: "CreateBookRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        if req.profile not in QUALITY_PROFILES:
            raise ServiceError(
                400,
                f"Unknown profile {req.profile!r}; choose from {sorted(QUALITY_PROFILES)}.",
            )
        if not req.mock and not self._live(req.provider):
            raise ServiceError(
                400,
                self._no_creds_msg(req.provider),
            )

        title_hint = req.title or req.premise
        book_id = self._make_id(title_hint)
        meta = {
            "id": book_id,
            "title": req.title or "",
            "created_at": _now_iso(),
            "profile": req.profile,
            "mock": bool(req.mock),
            "genre": req.genre or "",
            "logline": "",
            "use_cache": bool(req.use_cache),
            "run_continuity_check": bool(req.run_continuity_check),
            "provider": req.provider or "",
            "model": req.model or "",
            "chapter_images": bool(req.chapter_images),
        }
        self._write_meta(book_id, meta)

        try:
            settings = self._make_settings(meta, book_id)
            llm = self._make_llm(req.mock, meta)
            pipe = BookPipeline(llm, settings)
            bible = pipe.plan(
                premise=req.premise,
                chapters=req.chapters,
                words_per_chapter=req.words_per_chapter,
                title=req.title,
                genre=req.genre,
                extra_guidance=req.guidance or "",
            )
        except ServiceError:
            raise
        except Exception:
            # Planning failed — clean up the half-created book dir. Log the full
            # detail server-side; the client gets a generic message so provider
            # error bodies / filesystem paths never leak.
            logger.exception("planning failed for book %s", book_id)
            shutil.rmtree(self._book_dir(book_id), ignore_errors=True)
            raise ServiceError(500, "Planning failed.")

        # A user-supplied title is authoritative; otherwise use the planned one.
        if req.title:
            bible.title = req.title
            BookStore(self._book_dir(book_id)).save_bible(bible)
        meta["title"] = (req.title or bible.title) or meta["title"]
        meta["genre"] = req.genre or bible.genre or meta["genre"]
        meta["logline"] = bible.logline or meta["logline"]
        self._write_meta(book_id, meta)

        return {
            "book": self._summary(book_id, meta),
            "bible": bible.to_dict(),
        }

    # ------------------------------------------------------------------ #
    # Import pre-written material + modify existing chapters
    # ------------------------------------------------------------------ #
    def import_book(self, req: "ImportRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        """Turn a pasted/uploaded manuscript into a first-class book."""
        from .. import importer
        from ..pipeline import _ledger_dict

        text = (req.text or "").strip()
        if not text:
            raise ServiceError(400, "Paste or upload a manuscript to import.")
        if req.profile not in QUALITY_PROFILES:
            raise ServiceError(400, f"Unknown profile {req.profile!r}.")

        prov = req.provider or None
        title_hint = req.title or "Imported manuscript"
        book_id = self._make_id(title_hint)
        meta = {
            "id": book_id,
            "title": req.title or "",
            "created_at": _now_iso(),
            "profile": req.profile,
            "mock": bool(req.mock),
            "genre": req.genre or "",
            "logline": "",
            "use_cache": bool(req.use_cache),
            "run_continuity_check": True,
            "provider": req.provider or "",
            "model": req.model or "",
            "chapter_images": False,
            "imported": True,
        }
        self._write_meta(book_id, meta)

        # Reverse-engineer the bible + continuity when a model is available; with
        # no creds (and not mock) fall back to a structure-only import so it never
        # fails — the chapters are still imported and fully editable.
        analyze = True if req.analyze is None else bool(req.analyze)
        if req.mock:
            llm = self._make_llm(True, meta)
        elif self._live(prov):
            llm = self._make_llm(False, meta)
        else:
            llm, analyze = None, False

        settings = self._make_settings(meta, book_id)
        ledger = CostLedger()
        try:
            graph = importer.build_graph_from_text(
                llm, settings, ledger, text=text, title=req.title, genre=req.genre,
                guidance=req.guidance or "", analyze=analyze, run_extract=analyze,
            )
        except Exception:  # noqa: BLE001 - clean up; generic message to client
            logger.exception("import failed for book %s", book_id)
            shutil.rmtree(self._book_dir(book_id), ignore_errors=True)
            raise ServiceError(500, "Import failed.")

        store = BookStore(self._book_dir(book_id))
        store.save_graph(graph)
        for rec in graph.chapters.values():
            store.save_chapter(rec)
        store.assemble_manuscript(graph)
        try:
            store.save_cost(ledger.report(), _ledger_dict(ledger))
        except Exception:  # noqa: BLE001 - cost snapshot is non-critical
            pass

        meta["title"] = (req.title or graph.bible.title) or meta["title"]
        meta["genre"] = req.genre or graph.bible.genre or meta["genre"]
        meta["logline"] = graph.bible.logline or meta["logline"]
        self._write_meta(book_id, meta)
        return {"book": self._summary(book_id, meta), "bible": graph.bible.to_dict()}

    def _chapter_payload(self, book_id, graph, n) -> Dict[str, Any]:
        rec = graph.chapters.get(n)
        plan = graph.bible.plan(n)
        return {
            "number": n,
            "title": (rec.title if rec else (plan.title if plan else f"Chapter {n}")),
            "text": rec.text if rec else "",
            "word_count": rec.word_count if rec else 0,
            "written": bool(rec),
        }

    def set_chapter_text(self, book_id: str, n: int, req: "ChapterEditRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        """Replace a chapter's prose (manual edit). Instant; no model needed.
        Optionally re-run continuity extraction when ``reextract`` and creds exist."""
        meta = self._require_meta(book_id)
        with self._write_lock(book_id):
            store = BookStore(self._book_dir(book_id))
            graph = store.load_graph()
            if graph is None:
                raise ServiceError(404, f"Book {book_id!r} has no plan.")
            plan = graph.bible.plan(n)
            if plan is None:
                raise ServiceError(404, f"Chapter {n} is not in the outline.")
            text = (req.text or "").strip()
            if not text:
                raise ServiceError(400, "Chapter text cannot be empty.")

            from ..models import ChapterRecord
            rec = graph.chapters.get(n) or ChapterRecord(number=n, title=plan.title, text="")
            rec.text = text
            if req.title:
                rec.title = req.title.strip()
                plan.title = req.title.strip()
            rec.word_count = len(text.split())
            rec.compute_fingerprint()
            graph.chapters[n] = rec

            settings = self._make_settings(meta, book_id)
            synopsis = rec.synopsis_line
            if req.reextract:
                mock = bool(req.mock) if req.mock is not None else bool(meta.get("mock", False))
                if mock or self._live(meta.get("provider") or None):
                    try:
                        from ..extractor import extract_delta
                        llm = self._make_llm(mock, meta)
                        delta = extract_delta(llm, settings, CostLedger(), graph, plan, rec)
                        graph.apply_delta(delta)
                        synopsis = delta.synopsis_line
                    except Exception:  # noqa: BLE001 - best-effort
                        pass
            graph.record_chapter(rec, synopsis, settings.synopsis_line_chars)
            store.save_chapter(rec)
            store.save_graph(graph)
            store.assemble_manuscript(graph)
            return self._chapter_payload(book_id, graph, n)

    def revise_chapter(self, book_id: str, n: int, req: "ReviseRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        """AI-revise an existing chapter per instructions (or polish if none)."""
        meta = self._require_meta(book_id)
        with self._write_lock(book_id):
            store = BookStore(self._book_dir(book_id))
            graph = store.load_graph()
            if graph is None:
                raise ServiceError(404, f"Book {book_id!r} has no plan.")
            plan = graph.bible.plan(n)
            rec = graph.chapters.get(n)
            if plan is None or rec is None:
                raise ServiceError(404, f"Chapter {n} hasn't been written yet.")

            mock = bool(req.mock) if req.mock is not None else bool(meta.get("mock", False))
            prov = meta.get("provider") or None
            if not mock and not self._live(prov):
                raise ServiceError(400, self._no_creds_msg(prov))

            settings = self._make_settings(meta, book_id)
            llm = self._make_llm(mock, meta)
            ledger = CostLedger()
            from ..writer import revise_chapter as _revise
            try:
                new_rec = _revise(llm, settings, ledger, graph, plan, rec.text,
                                  instructions=req.instructions or "")
            except Exception:  # noqa: BLE001
                # Full detail (provider bodies, paths) stays in the server log.
                logger.exception("revision failed for book %s chapter %s", book_id, n)
                raise ServiceError(500, "Revision failed.")
            graph.chapters[n] = new_rec
            graph.record_chapter(new_rec, new_rec.synopsis_line, settings.synopsis_line_chars)
            store.save_chapter(new_rec)
            store.save_graph(graph)
            store.assemble_manuscript(graph)
            return self._chapter_payload(book_id, graph, n)

    def append_chapters(self, book_id: str, req: "AppendChaptersRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        """Propose & append N new outline chapters that continue the story. They
        are left UNwritten — the caller then runs the normal write/generate flow."""
        meta = self._require_meta(book_id)
        with self._write_lock(book_id):
            store = BookStore(self._book_dir(book_id))
            graph = store.load_graph()
            if graph is None:
                raise ServiceError(404, f"Book {book_id!r} has no plan.")
            mock = bool(req.mock) if req.mock is not None else bool(meta.get("mock", False))
            prov = meta.get("provider") or None
            if not mock and not self._live(prov):
                raise ServiceError(400, self._no_creds_msg(prov))

            settings = self._make_settings(meta, book_id)
            llm = self._make_llm(mock, meta)
            from ..planner import extend_outline
            try:
                new_plans = extend_outline(
                    llm, settings, CostLedger(), graph,
                    count=req.count, words_per_chapter=req.words_per_chapter,
                    guidance=req.guidance or "",
                )
            except Exception:  # noqa: BLE001
                logger.exception("outline extension failed for book %s", book_id)
                raise ServiceError(500, "Could not extend the outline.")
            graph.bible.outline.extend(new_plans)
            graph.bible.target_chapters = len(graph.bible.outline)
            store.save_graph(graph)
            return {
                "added": [{"number": p.number, "title": p.title} for p in new_plans],
                "chapters_total": len(graph.bible.outline),
            }

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def get_book(self, book_id: str) -> Dict[str, Any]:
        meta = self._require_meta(book_id)
        store = BookStore(self._book_dir(book_id))
        bible = store.load_bible()
        if bible is None:
            raise ServiceError(404, f"Book {book_id!r} has no plan.")
        chapters = []
        for p in bible.outline:
            written = store.has_chapter(p.number)
            word_count = 0
            title = p.title
            if written:
                try:
                    rec = self._load_chapter_record(store, p.number)
                    if rec is not None:
                        word_count = rec.get("word_count", 0)
                        title = rec.get("title", p.title)
                except Exception:
                    pass
            chapters.append({
                "number": p.number,
                "title": title,
                "act": p.act,
                "written": written,
                "word_count": word_count,
                "has_image": store.has_image(p.number),
            })
        return {
            "book": self._summary(book_id, meta),
            "bible": bible.to_dict(),
            "chapters": chapters,
            "cost": self._cost_snapshot(book_id),
        }

    @staticmethod
    def _load_chapter_record(store: BookStore, n: int) -> Optional[Dict[str, Any]]:
        path = store.chapter_json(n)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_chapter(self, book_id: str, n: int) -> Dict[str, Any]:
        self._require_meta(book_id)
        store = BookStore(self._book_dir(book_id))
        bible = store.load_bible()
        if bible is None:
            raise ServiceError(404, f"Book {book_id!r} has no plan.")
        plan = bible.plan(n)
        if plan is None:
            raise ServiceError(404, f"Chapter {n} not in outline.")
        rec = self._load_chapter_record(store, n)
        written = rec is not None
        has_image = store.has_image(n)
        return {
            "number": n,
            "title": (rec.get("title") if rec else None) or plan.title,
            "text": (rec.get("text") if rec else "") or "",
            "word_count": (rec.get("word_count") if rec else 0) or 0,
            "synopsis_line": (rec.get("synopsis_line") if rec else "") or "",
            "fingerprint": (rec.get("fingerprint") if rec else "") or "",
            "written": written,
            "has_image": has_image,
            "image_url": f"/api/books/{book_id}/chapters/{n}/image" if has_image else "",
            "plan": plan.to_dict(),
        }

    def get_chapter_image(self, book_id: str, n: int) -> Tuple[str, str]:
        """Return (filesystem path, media-type) for a chapter image, or 404."""
        self._require_meta(book_id)
        store = BookStore(self._book_dir(book_id))
        path = store.find_image(n)
        if not path:
            raise ServiceError(404, f"Chapter {n} has no image.")
        ext = path.rsplit(".", 1)[-1].lower()
        media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "webp": "image/webp", "gif": "image/gif"}.get(ext, "application/octet-stream")
        return path, media

    def get_graph(self, book_id: str) -> Dict[str, Any]:
        self._require_meta(book_id)
        store = BookStore(self._book_dir(book_id))
        graph = store.load_graph()
        if graph is None:
            raise ServiceError(404, f"Book {book_id!r} has no plan.")
        b = graph.bible
        return {
            "characters": [c.to_dict() for c in b.characters],
            "locations": [l.to_dict() for l in b.locations],
            "items": [i.to_dict() for i in b.items],
            "threads": [t.to_dict() for t in b.threads],
            "timeline": [e.to_dict() for e in graph.timeline],
            "synopsis": list(graph.synopsis),
        }

    def _cost_snapshot(self, book_id: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self._book_dir(book_id), "cost.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_cost(self, book_id: str) -> Dict[str, Any]:
        self._require_meta(book_id)
        snapshot = self._cost_snapshot(book_id)
        report = ""
        path = os.path.join(self._book_dir(book_id), "cost.txt")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    report = f.read()
            except Exception:
                report = ""
        return {"snapshot": snapshot, "report": report}

    def get_manuscript(self, book_id: str) -> Dict[str, Any]:
        self._require_meta(book_id)
        store = BookStore(self._book_dir(book_id))
        graph = store.load_graph()
        if graph is None:
            raise ServiceError(404, f"Book {book_id!r} has no plan.")
        markdown = store.assemble_manuscript(graph)
        words = len(markdown.split())
        # Augment (for the response only — not the saved .md) with an image marker
        # after each chapter heading that has an illustration, so the reader and
        # plain view can show it inline.
        augmented = self._with_chapter_images(book_id, store, graph, markdown)
        return {"markdown": augmented, "words": words}

    @staticmethod
    def _with_chapter_images(book_id: str, store: BookStore, graph, markdown: str) -> str:
        import re
        if not any(store.has_image(p.number) for p in graph.bible.outline):
            return markdown
        out = []
        for line in markdown.split("\n"):
            out.append(line)
            m = re.match(r"^##\s+Chapter\s+(\d+)\b", line.strip())
            if m:
                n = int(m.group(1))
                if store.has_image(n):
                    out.append("")
                    out.append(f"![Chapter {n} illustration](/api/books/{book_id}/chapters/{n}/image)")
        return "\n".join(out)

    # ------------------------------------------------------------------ #
    # KDP packaging
    # ------------------------------------------------------------------ #
    def _kdp_dir(self, book_id: str) -> str:
        return os.path.join(self._book_dir(book_id), "kdp")

    def _kdp_json_path(self, book_id: str) -> str:
        return os.path.join(self._book_dir(book_id), "kdp.json")

    def _load_graph_or_404(self, book_id: str):
        store = BookStore(self._book_dir(book_id))
        graph = store.load_graph()
        if graph is None:
            raise ServiceError(404, f"Book {book_id!r} has no plan.")
        return graph

    def prepare_kdp(self, book_id: str, req: "KdpRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        if not graph.chapters:
            raise ServiceError(404, f"Book {book_id!r} has no written chapters.")

        # Pick the LLM: explicit req.mock wins; otherwise mock unless creds exist.
        prov = meta.get("provider") or None
        if req.mock is None:
            mock = bool(meta.get("mock", False)) or not self._live(prov)
        else:
            mock = bool(req.mock)
        if not mock and not self._live(prov):
            raise ServiceError(
                400,
                self._no_creds_msg(prov),
            )

        settings = self._make_settings(meta, book_id)
        llm = self._make_llm(mock, meta)
        ledger = CostLedger()

        try:
            kdp_meta = generate_kdp_metadata(
                llm, settings, ledger, graph,
                author_first=req.author_first,
                author_last=req.author_last,
                language=req.language or "English",
                subtitle=req.subtitle,
                series=req.series or "",
                edition=req.edition or "",
                contributors=req.contributors or [],
            )
        except Exception:  # noqa: BLE001 - generic message to client
            logger.exception("KDP metadata generation failed for book %s", book_id)
            raise ServiceError(500, "KDP metadata generation failed.")

        # Apply user overrides that the generator may not honor verbatim.
        kdp_meta.language = req.language or "English"
        if req.subtitle is not None:
            kdp_meta.subtitle = req.subtitle.strip()
        if req.series:
            kdp_meta.series = req.series
        kdp_meta.series_part = req.series_part or ""
        kdp_meta.edition = req.edition or ""
        kdp_meta.publishing_rights = (
            "public_domain" if req.publishing_rights == "public_domain" else "owned"
        )
        kdp_meta.sexually_explicit = bool(req.sexually_explicit)
        kdp_meta.reading_age_min = req.reading_age_min or ""
        kdp_meta.reading_age_max = req.reading_age_max or ""
        if req.contributors:
            contribs: List[Dict[str, str]] = []
            for c in req.contributors[:MAX_CONTRIBUTORS]:
                contribs.append({
                    "first": str(c.get("first", "")).strip(),
                    "last": str(c.get("last", "")).strip(),
                })
            kdp_meta.contributors = contribs

        out_dir = self._kdp_dir(book_id)
        images = BookStore(self._book_dir(book_id)).collect_images(
            [p.number for p in graph.bible.outline])
        # Prefer the client's cover; else a previously generated AI cover.svg.
        cover_svg = req.cover_svg
        if not cover_svg:
            saved = os.path.join(out_dir, "cover.svg")
            if os.path.isfile(saved):
                with open(saved, "r", encoding="utf-8") as f:
                    cover_svg = f.read()
        result = build_kdp_kit(graph, kdp_meta, out_dir, cover_svg=cover_svg, images=images)

        meta_dict = result["metadata"]
        # Persist a kdp.json snapshot in the book dir for later retrieval.
        _write_json(self._kdp_json_path(book_id), meta_dict)

        listing = ""
        listing_path = result["paths"].get("listing")
        if listing_path and os.path.isfile(listing_path):
            with open(listing_path, "r", encoding="utf-8") as f:
                listing = f.read()

        return {"metadata": meta_dict, "listing": listing, "paths": result["paths"]}

    def get_kdp(self, book_id: str):
        # Returns the saved KDP metadata, or None if it hasn't been prepared yet.
        # (None — not a 404 — so the Publish page can probe it on mount without a
        # noisy console error; the book itself still 404s via _require_meta.)
        self._require_meta(book_id)
        path = self._kdp_json_path(book_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def kdp_listing(self, book_id: str) -> str:
        self._require_meta(book_id)
        path = os.path.join(self._kdp_dir(book_id), "kdp-listing.txt")
        if not os.path.isfile(path):
            raise ServiceError(404, f"Book {book_id!r} has no KDP listing; prepare it first.")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def epub_path(self, book_id: str) -> str:
        """Path to the built manuscript.epub, building it on demand if missing."""
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        if not graph.chapters:
            raise ServiceError(404, f"Book {book_id!r} has no written chapters.")

        out_dir = self._kdp_dir(book_id)
        epub = os.path.join(out_dir, "manuscript.epub")
        if os.path.isfile(epub):
            return epub

        # Build on demand. Prefer saved KDP metadata (so the cover/author match a
        # prior prepare); otherwise synthesize a minimal metadata with a fallback
        # cover so an EPUB is always available post-write.
        kdp_json = self._kdp_json_path(book_id)
        if os.path.isfile(kdp_json):
            with open(kdp_json, "r", encoding="utf-8") as f:
                kdp_meta = KdpMetadata.from_dict(json.load(f))
        else:
            kdp_meta = KdpMetadata(
                title=meta.get("title") or graph.bible.title or "Untitled",
                author_first="",
                author_last="",
            )
        store = BookStore(self._book_dir(book_id))
        images = store.collect_images([p.number for p in graph.bible.outline])
        # Embed a previously generated AI cover, like build_kdp_kit does.
        art, art_ext = self._load_cover_art(book_id)
        # Build the bytes BEFORE opening the file: open("wb") first would leave
        # a 0-byte manuscript.epub behind if build_epub raised, and every later
        # call would return that empty file as "already built".
        data = build_epub(graph, kdp_meta, images=images,
                          art_bytes=art, art_ext=art_ext or "png")
        os.makedirs(out_dir, exist_ok=True)
        with open(epub, "wb") as f:
            f.write(data)
        return epub

    def epub_filename(self, book_id: str) -> str:
        meta = self._require_meta(book_id)
        return f"{_display_filename(meta.get('title') or book_id)}.epub"

    # ------------------------------------------------------------------ #
    # Print / pricing / marketing
    # ------------------------------------------------------------------ #
    def _load_kdp_meta(self, book_id: str, meta: Dict[str, Any], graph) -> KdpMetadata:
        """Saved KDP metadata if a prior prepare ran, else minimal fallback."""
        kdp_json = self._kdp_json_path(book_id)
        if os.path.isfile(kdp_json):
            with open(kdp_json, "r", encoding="utf-8") as f:
                return KdpMetadata.from_dict(json.load(f))
        return KdpMetadata(
            title=meta.get("title") or graph.bible.title or "Untitled",
            author_first="",
            author_last="",
        )

    def export_docx_path(self, book_id: str) -> str:
        """Path to a built print interior .docx, building it on demand."""
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        if not graph.chapters:
            raise ServiceError(404, f"Book {book_id!r} has no written chapters.")

        kdp_meta = self._load_kdp_meta(book_id, meta, graph)
        out_dir = self._kdp_dir(book_id)
        os.makedirs(out_dir, exist_ok=True)
        docx = os.path.join(out_dir, "interior.docx")
        with open(docx, "wb") as f:
            f.write(build_docx(graph, kdp_meta))
        return docx

    def docx_filename(self, book_id: str) -> str:
        meta = self._require_meta(book_id)
        return f"{_display_filename(meta.get('title') or book_id)}.docx"

    def print_spec(self, book_id: str) -> Dict[str, Any]:
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        kdp_meta = self._load_kdp_meta(book_id, meta, graph)
        return _print_spec(graph, kdp_meta)

    def print_cover_svg(self, book_id: str) -> str:
        """Full-wrap print-cover SVG (back blurb + spine + front), embedding the
        saved procedural front cover if a prior prepare produced one."""
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        kdp_meta = self._load_kdp_meta(book_id, meta, graph)
        spec = _print_spec(graph, kdp_meta)
        front = None
        cover_path = os.path.join(self._kdp_dir(book_id), "cover.svg")
        if os.path.isfile(cover_path):
            with open(cover_path, "r", encoding="utf-8") as f:
                front = f.read()
        return build_print_cover_svg(graph, kdp_meta, spec, front_cover_svg=front)

    # ------------------------------------------------------------------ #
    # AI cover art / back cover / PDF exports
    # ------------------------------------------------------------------ #
    def _load_cover_art(self, book_id: str):
        """Return (bytes, ext) for a previously generated AI cover, else (None, None)."""
        d = self._kdp_dir(book_id)
        if not os.path.isdir(d):
            return None, None
        for name in sorted(os.listdir(d)):
            if name.startswith("cover-art."):
                try:
                    with open(os.path.join(d, name), "rb") as f:
                        return f.read(), name.rsplit(".", 1)[-1].lower()
                except OSError:
                    return None, None
        return None, None

    def _cover_meta(self, book_id: str, meta: Dict[str, Any], graph, req) -> KdpMetadata:
        """KdpMetadata for cover typography: saved meta, with form overrides."""
        km = self._load_kdp_meta(book_id, meta, graph)
        if req is not None:
            if getattr(req, "title", None):
                km.title = req.title.strip()
            if getattr(req, "subtitle", None) is not None:
                km.subtitle = (req.subtitle or "").strip()
            if getattr(req, "author_first", None) is not None:
                km.author_first = (req.author_first or "").strip()
            if getattr(req, "author_last", None) is not None:
                km.author_last = (req.author_last or "").strip()
        return km

    def generate_cover(self, book_id: str, req: "CoverRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        """Generate AI cover ARTWORK via the image backend and compose a finished
        front cover (art + title/author typography). Persists the raw art and the
        composed cover.svg so the EPUB / print cover / PDFs reuse them."""
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        from ..images import image_available, generate_cover_art
        if not image_available():
            raise ServiceError(
                400,
                "No image backend configured — set PIXIO_API_KEY (or choose an "
                "image provider in Settings) to generate an AI cover.",
            )
        try:
            art, ext = generate_cover_art(graph.bible)
        except Exception:  # noqa: BLE001 - network/provider error, sanitized
            logger.exception("AI cover generation failed for book %s", book_id)
            raise ServiceError(502, "AI cover generation failed.")

        ext = (ext or "png").lower().lstrip(".")
        d = self._kdp_dir(book_id)
        os.makedirs(d, exist_ok=True)
        # Clear any prior art (possibly a different extension), then save.
        for name in list(os.listdir(d)):
            if name.startswith("cover-art."):
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
        with open(os.path.join(d, f"cover-art.{ext}"), "wb") as f:
            f.write(art)

        km = self._cover_meta(book_id, meta, graph, req)
        from ..kdp import compose_cover_svg
        svg = compose_cover_svg(km, art, ext)
        with open(os.path.join(d, "cover.svg"), "w", encoding="utf-8") as f:
            f.write(svg)
        return {"cover_svg": svg, "has_art": True}

    def back_cover_svg(self, book_id: str) -> str:
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        km = self._load_kdp_meta(book_id, meta, graph)
        art, ext = self._load_cover_art(book_id)
        from ..kdp import back_cover_svg as _bc
        return _bc(graph, km, art_bytes=art, ext=ext or "png")

    def export_pdf(self, book_id: str, part: str):
        """Return (pdf_bytes, filename) for part in interior|front-cover|back-cover|full."""
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        from .. import pdf as _pdf
        if not _pdf.pdf_available():
            raise ServiceError(501, _pdf._INSTALL_HINT)
        part = (part or "full").lower()
        if part not in _pdf.PDF_PARTS:
            raise ServiceError(400, f"Unknown PDF part {part!r}; choose from {list(_pdf.PDF_PARTS)}.")
        # interior/full need written chapters; covers can render from metadata.
        if part in ("interior", "full") and not graph.chapters:
            raise ServiceError(404, f"Book {book_id!r} has no written chapters.")
        km = self._load_kdp_meta(book_id, meta, graph)
        art, ext = self._load_cover_art(book_id)
        try:
            data = _pdf.build_pdf(part, graph, km, art_bytes=art, ext=ext or "png")
        except _pdf.PdfUnavailable as e:
            raise ServiceError(501, str(e))
        fname = f"{_display_filename(meta.get('title') or book_id)}-{part}.pdf"
        return data, fname

    def estimate_pricing(self, book_id: str, req: "PricingRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        pages = estimate_page_count(graph)
        # Use the real built EPUB's size for the delivery-fee math when one
        # exists; else keep the 1.0 MB default assumption.
        ebook_file_mb = 1.0
        epub = os.path.join(self._kdp_dir(book_id), "manuscript.epub")
        if os.path.isfile(epub):
            ebook_file_mb = os.path.getsize(epub) / 1e6
        return estimate_royalties(
            list_price=req.list_price,
            marketplace=req.marketplace or "US",
            page_count=pages,
            paper=req.paper or "white",
            ebook_file_mb=ebook_file_mb,
        )

    def generate_marketing(self, book_id: str, req: "MarketingRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        meta = self._require_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        if not graph.chapters:
            raise ServiceError(404, f"Book {book_id!r} has no written chapters.")

        # Pick the LLM: explicit req.mock wins; else mock unless creds exist.
        prov = meta.get("provider") or None
        if req.mock is None:
            mock = bool(meta.get("mock", False)) or not self._live(prov)
        else:
            mock = bool(req.mock)
        if not mock and not self._live(prov):
            raise ServiceError(
                400,
                self._no_creds_msg(prov),
            )

        kdp_meta = self._load_kdp_meta(book_id, meta, graph)
        settings = self._make_settings(meta, book_id)
        llm = self._make_llm(mock, meta)
        ledger = CostLedger()

        try:
            marketing = _generate_marketing(llm, settings, ledger, graph, kdp_meta)
        except Exception:  # noqa: BLE001 - generic message to client
            logger.exception("marketing generation failed for book %s", book_id)
            raise ServiceError(500, "Marketing generation failed.")

        # Persist a marketing.json snapshot in the kit dir — the same
        # kdp/marketing.json the MCP server reads/writes, so either surface's
        # copy gets reused by a later prepare.
        _write_json(os.path.join(self._kdp_dir(book_id), "marketing.json"), marketing)

        return marketing

    # ------------------------------------------------------------------ #
    # Delete
    # ------------------------------------------------------------------ #
    def delete_book(self, book_id: str) -> Dict[str, Any]:
        self._require_meta(book_id)
        if self.broker.is_running(book_id):
            raise ServiceError(409, "A write job is running for this book; cannot delete.")
        # Also refuse while ANOTHER process (e.g. the MCP server) is writing:
        # probe the cross-process lock, then release it before the rmtree (an
        # open handle would block deleting the lock file on Windows).
        try:
            with book_write_lock(self._book_dir(book_id), timeout=0):
                pass
        except BookLockedError:
            raise ServiceError(409, "A write job is running for this book; cannot delete.")
        shutil.rmtree(self._book_dir(book_id), ignore_errors=True)
        # Release the in-memory event ring so deleted books don't leak their
        # (full-text) event buffer for the process lifetime.
        self.broker.drop(book_id)
        return {"status": "deleted"}

    # ------------------------------------------------------------------ #
    # Write (background job)
    # ------------------------------------------------------------------ #
    def start_write(self, book_id: str, req: "WriteRequest") -> Dict[str, Any]:  # type: ignore[name-defined]
        meta = self._require_meta(book_id)
        store = BookStore(self._book_dir(book_id))
        if store.load_bible() is None:
            raise ServiceError(404, f"Book {book_id!r} has no plan.")
        if not meta.get("mock", False) and not self._live(meta.get("provider") or None):
            raise ServiceError(
                400,
                self._no_creds_msg(meta.get("provider") or None),
            )

        # Cross-process guard first: the file lock also excludes an MCP-process
        # write on the same book dir, which the in-memory broker cannot see.
        write_lock = book_write_lock(self._book_dir(book_id), timeout=0)
        try:
            write_lock.__enter__()
        except BookLockedError:
            raise ServiceError(409, "A write job is already running for this book.")

        # Reserve the running slot atomically; broker enforces one-job-per-book.
        if not self.broker.start_job(book_id):
            write_lock.__exit__(None, None, None)
            raise ServiceError(409, "A write job is already running for this book.")

        only = req.only or None
        restart = bool(req.restart)
        mock = bool(meta.get("mock", False))

        def _run() -> None:
            try:
                settings = self._make_settings(meta, book_id)
                llm = self._make_llm(mock, meta)

                def on_event(event: Dict[str, Any]) -> None:
                    self.broker.publish(book_id, event)

                pipe = BookPipeline(
                    llm, settings, on_event=on_event, stream_prose=True,
                    image_provider=self._make_image_provider(meta),
                )
                if not pipe.load():
                    raise RuntimeError("No plan found for this book.")
                pipe.write_all(resume=not restart, only=only)
                terminal = {"type": "done"}
            except Exception:  # noqa: BLE001 - surfaced to the client via SSE
                # Always record the full traceback server-side — the SSE client may
                # have disconnected, and we must not lose the failure. The client
                # gets a generic message: exception text can carry provider
                # response bodies and filesystem paths.
                logger.exception("write job failed for book %s", book_id)
                terminal = {"type": "error", "message": "Generation failed."}
            finally:
                # Release the cross-process lock BEFORE publishing the terminal
                # event, so a client reacting to it can start a new job at once.
                write_lock.__exit__(None, None, None)
            self.broker.publish(book_id, terminal)

        t = threading.Thread(target=_run, name=f"write-{book_id}", daemon=True)
        with self._lock:
            self._threads[book_id] = t
        t.start()
        return {"status": "started"}
