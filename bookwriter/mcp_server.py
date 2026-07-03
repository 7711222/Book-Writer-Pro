"""Model Context Protocol (stdio) server for BookwriterPro.

Exposes the book-generation engine as a set of MCP tools so an *agent* can plan
and write full books the same way the HTTP UI does. It reuses
``bookwriter.server.service.BookService`` when that module is importable, so the
MCP server and the FastAPI server share the exact same data directory, book ids,
``meta.json`` shape and on-disk layout. When the HTTP service module is not
present, an in-module fallback implements the identical conventions directly on
top of the core package (``Settings`` / ``BookStore`` / ``BookPipeline``), so
state is still shared at the only boundary that matters: the filesystem.

Run it as a stdio MCP server:

    python -m bookwriter.mcp_server

The ``mcp`` package is imported lazily inside :func:`main` / :func:`build_server`
so this module always imports (and ``py_compile``s) even when ``mcp`` is not
installed; ``main()`` then prints a helpful ``pip install mcp`` message.

Design notes for the agent reading these docstrings:
  * Every tool returns plain JSON-serializable Python (dicts / lists / str).
  * ``mock=True`` runs fully offline with no API key (great for trying the tools).
  * ``write_book`` is SYNCHRONOUS: it returns only once the requested chapters
    are fully written, so the agent gets the finished cost + flags in one call.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Shared data-dir conventions (identical to the HTTP API contract).
# ---------------------------------------------------------------------------

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_data_root() -> str:
    """Repo-root ``.bookwriter_data`` in a source checkout, else a user-writable
    ``~/.bookwriter_data``. Deriving it from the install dir would put books in
    site-packages (read-only / wiped on upgrade) for a pip-installed wheel."""
    if os.path.isfile(os.path.join(_PKG_ROOT, "pyproject.toml")):
        return os.path.join(_PKG_ROOT, ".bookwriter_data")
    return os.path.join(os.path.expanduser("~"), ".bookwriter_data")


_DEFAULT_DATA_DIR = _default_data_root()


def data_root() -> str:
    """The books data directory (env ``BOOKWRITER_DATA_DIR`` overrides default)."""
    root = os.environ.get("BOOKWRITER_DATA_DIR") or _DEFAULT_DATA_DIR
    os.makedirs(root, exist_ok=True)
    return root


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "book"


_BOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _validate_book_id(book_id: str) -> str:
    """Reject ids that aren't the slug-hash shape we mint (path-traversal guard).

    Mirrors ``bookwriter.server.service.validate_book_id`` so the MCP fallback
    is as safe as the HTTP service: a caller-supplied ``book_id`` can never
    contain ``/``, ``\\``, ``..`` or absolute-path components before it is
    os.path.join'd onto the data root.
    """
    if not isinstance(book_id, str) or not _BOOK_ID_RE.match(book_id):
        raise BookNotFound(book_id)
    return book_id


def make_book_id(title: str, *, exists=None) -> str:
    """Slug of the title + 6-char hash, in the same ``slug-hash`` shape the HTTP
    service uses (only ever hit on the rare HTTP-import-fail fallback path).

    ``exists`` is an optional predicate ``(book_id) -> bool``; when supplied we
    re-seed the hash on collision so two books with the same title don't clobber
    each other's meta.json. (This local minter is deterministic — sha1 + a counter
    seed — whereas the HTTP service uses sha256 + a time seed; ids are not
    byte-identical across the two, only the same shape.)
    """
    base = _slug(title)
    h = hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:6]
    book_id = f"{base}-{h}"
    if exists is not None:
        seed = 0
        while exists(book_id):
            seed += 1
            h = hashlib.sha1(f"{title}-{seed}".encode("utf-8")).hexdigest()[:6]
            book_id = f"{base}-{h}"
    return book_id


# ---------------------------------------------------------------------------
# Service resolution: prefer the shared HTTP BookService; else local fallback.
# Both speak the same on-disk layout, so they share state either way.
# ---------------------------------------------------------------------------

_service_lock = threading.Lock()
_service_singleton: Any = None


def _chapter_word_count(store: Any, n: int) -> int:
    """Words written for chapter *n* from its on-disk JSON record (0 if absent).

    Mirrors the HTTP BookService so the MCP summary's ``words`` field matches the
    HTTP contract on the local-fallback path.
    """
    try:
        path = store.chapter_json(n)
        if not os.path.isfile(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        return int(rec.get("word_count", 0) or 0)
    except Exception:
        return 0


def get_service() -> Any:
    """Return a process-wide book service.

    Tries ``bookwriter.server.service.BookService`` first (the same object the
    FastAPI app uses). Falls back to :class:`_LocalBookService` which implements
    the identical data conventions on top of the core package.
    """
    global _service_singleton
    with _service_lock:
        if _service_singleton is None:
            _service_singleton = _build_service()
        return _service_singleton


def _build_service() -> Any:
    root = data_root()
    try:
        from bookwriter.server.service import BookService  # type: ignore

        # The HTTP service may accept the data dir positionally or by keyword,
        # or read the env var itself. Try the common shapes, then bare.
        for attempt in (
            lambda: BookService(root),
            lambda: BookService(data_dir=root),
            lambda: BookService(),
        ):
            try:
                svc = attempt()
            except TypeError:
                continue
            return _ServiceAdapter(svc)
    except Exception:
        # ImportError (module not built yet) or any construction failure ->
        # fall back to the self-contained local implementation.
        pass
    return _LocalBookService(root)


# ---------------------------------------------------------------------------
# Adapter: normalize whatever BookService exposes to the small surface the
# tools below need. We probe for the most likely method names and degrade to
# the local implementation for anything the HTTP service does not provide.
# ---------------------------------------------------------------------------


class _ServiceAdapter:
    """Wrap the HTTP ``BookService`` so MCP and HTTP share identical state.

    Read/profile/create operations delegate to the wrapped service, so their
    payloads are byte-identical to the HTTP API. The two write operations
    (``write_book`` / ``get_status``) are served by the local synchronous
    implementation against the *same* data dir, because the HTTP service only
    offers a background (async) write job and an MCP tool call must block until
    the prose is finished.
    """

    def __init__(self, svc: Any):
        self._svc = svc
        root = (
            getattr(svc, "data_dir", None)
            or getattr(svc, "root", None)
            or getattr(svc, "data_root", None)
            or data_root()
        )
        self._local = _LocalBookService(str(root))

    # ---- delegated to the shared HTTP service -------------------------
    def profiles(self) -> Dict[str, Any]:
        fn = getattr(self._svc, "profiles", None)
        if callable(fn):
            return fn()
        return _profiles_payload()

    def list_books(self) -> List[Dict[str, Any]]:
        fn = getattr(self._svc, "list_books", None)
        if callable(fn):
            res = fn()
            if isinstance(res, dict) and "books" in res:
                return res["books"]
            if isinstance(res, list):
                return res
        return self._local.list_books()

    def create_book(self, **kw) -> Dict[str, Any]:
        fn = getattr(self._svc, "create_book", None)
        if callable(fn):
            try:
                from bookwriter.server.schemas import CreateBookRequest

                req = CreateBookRequest(
                    premise=kw["premise"],
                    chapters=kw.get("chapters"),
                    words_per_chapter=kw.get("words_per_chapter", 2000),
                    title=kw.get("title") or None,
                    genre=kw.get("genre") or None,
                    guidance=kw.get("guidance") or None,
                    profile=kw.get("profile", "balanced"),
                    mock=bool(kw.get("mock", False)),
                    use_cache=bool(kw.get("use_cache", True)),
                    run_continuity_check=bool(kw.get("run_continuity_check", True)),
                    provider=kw.get("provider") or None,
                    model=kw.get("model") or None,
                    chapter_images=bool(kw.get("chapter_images", False)),
                )
                res = fn(req)
                return _flatten_create(res)
            except Exception as e:
                # Translate the HTTP service's typed errors into the same
                # PermissionError/ValueError the tools expect, else re-raise.
                detail = getattr(e, "detail", str(e))
                status = getattr(e, "status", None)
                if status == 400 and "demo mode (mock)" in detail:
                    raise PermissionError(detail)
                if status == 400:
                    raise ValueError(detail)
                raise
        return self._local.create_book(**kw)

    def get_book(self, book_id: str) -> Dict[str, Any]:
        fn = getattr(self._svc, "get_book", None)
        if callable(fn):
            return _map_service_errors(lambda: fn(book_id))
        return self._local.get_book(book_id)

    def get_chapter(self, book_id: str, number: int) -> Dict[str, Any]:
        fn = getattr(self._svc, "get_chapter", None)
        if callable(fn):
            return _map_service_errors(lambda: fn(book_id, number))
        return self._local.get_chapter(book_id, number)

    def get_graph(self, book_id: str) -> Dict[str, Any]:
        fn = getattr(self._svc, "get_graph", None)
        if callable(fn):
            return _map_service_errors(lambda: fn(book_id))
        return self._local.get_graph(book_id)

    def get_cost(self, book_id: str) -> Dict[str, Any]:
        fn = getattr(self._svc, "get_cost", None)
        if callable(fn):
            return _map_service_errors(lambda: fn(book_id))
        return self._local.get_cost(book_id)

    def get_manuscript(self, book_id: str) -> Dict[str, Any]:
        fn = getattr(self._svc, "get_manuscript", None)
        if callable(fn):
            return _map_service_errors(lambda: fn(book_id))
        return self._local.get_manuscript(book_id)

    # ---- synchronous write path (local, shared dir) -------------------
    def write_book(self, book_id: str, only: Optional[List[int]] = None,
                   restart: bool = False) -> Dict[str, Any]:
        # Share the HTTP service's one-job-per-book lock so an MCP write and an
        # HTTP POST /write can't run concurrently against the same project_dir,
        # and publish events so a web client watching /events sees MCP-driven
        # progress live.
        broker = getattr(self._svc, "broker", None)
        if broker is None or not hasattr(broker, "start_job"):
            return self._local.write_book(book_id, only=only, restart=restart)

        if not broker.start_job(book_id):
            raise RuntimeError(
                f"A write job is already running for {book_id!r}."
            )
        try:
            def publish(ev: Dict[str, Any]) -> None:
                try:
                    broker.publish(book_id, ev)
                except Exception:
                    pass

            result = self._local.write_book(
                book_id, only=only, restart=restart, on_event=publish
            )
            publish({"type": "done"})
            return result
        except Exception:
            # Same generic terminal the HTTP write job publishes — exception
            # text can carry provider response bodies and filesystem paths,
            # and web SSE viewers must not see it. The MCP tool caller still
            # gets the real exception via the raise.
            try:
                broker.publish(book_id, {"type": "error", "message": "Generation failed."})
            except Exception:
                pass
            raise
        finally:
            # start_job sets running=True; a terminal publish flips it back to
            # False. Guarantee the slot is released even if publish failed.
            ch = getattr(broker, "_channels", {}).get(book_id)
            if ch is not None and getattr(ch, "running", False):
                ch.running = False
                ch.finished = True

    def get_status(self, book_id: str) -> Dict[str, Any]:
        return self._local.get_status(book_id)

    # ---- KDP packaging (local, shared dir) ----------------------------
    # The KDP engine call is a pure function of (graph, settings, ledger) plus
    # caller identity fields, so we run it locally against the *same* data dir
    # the HTTP service uses. The kit lands in <book_dir>/kdp/ either way.
    def prepare_kdp(self, book_id: str, **kw) -> Dict[str, Any]:
        return self._local.prepare_kdp(book_id, **kw)

    def get_kdp(self, book_id: str) -> Dict[str, Any]:
        return self._local.get_kdp(book_id)

    def epub_path(self, book_id: str) -> str:
        # The HTTP service builds the EPUB on demand (reusing prepare_kdp's
        # metadata when present, else synthesizing a minimal one), so delegate
        # to it — export_epub then matches GET /api/books/{id}/export/epub.
        fn = getattr(self._svc, "epub_path", None)
        if callable(fn):
            return _map_service_errors(lambda: fn(book_id))
        return self._local.epub_path(book_id)

    # ---- print / royalties / marketing (local, shared dir) ------------
    def export_docx_path(self, book_id: str, **kw) -> str:
        return self._local.export_docx_path(book_id, **kw)

    def print_spec(self, book_id: str, **kw) -> Dict[str, Any]:
        return self._local.print_spec(book_id, **kw)

    def estimate_pricing(self, book_id: str, **kw) -> Dict[str, Any]:
        return self._local.estimate_pricing(book_id, **kw)

    def generate_marketing(self, book_id: str) -> Dict[str, Any]:
        return self._local.generate_marketing(book_id)

    def generate_cover(self, book_id: str, **kw) -> Dict[str, Any]:
        return self._local.generate_cover(book_id, **kw)

    def back_cover(self, book_id: str) -> Dict[str, Any]:
        return self._local.back_cover(book_id)

    def export_pdf(self, book_id: str, part: str = "full", **kw) -> Dict[str, Any]:
        return self._local.export_pdf(book_id, part, **kw)

    # import / modify share the local impl (same on-disk data dir as HTTP)
    def import_book(self, **kw) -> Dict[str, Any]:
        return self._local.import_book(**kw)

    def edit_chapter(self, book_id: str, number: int, **kw) -> Dict[str, Any]:
        return self._local.edit_chapter(book_id, number, **kw)

    def revise_chapter(self, book_id: str, number: int, **kw) -> Dict[str, Any]:
        return self._local.revise_chapter(book_id, number, **kw)

    def append_chapters(self, book_id: str, **kw) -> Dict[str, Any]:
        return self._local.append_chapters(book_id, **kw)


def _flatten_create(res: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the HTTP {book, bible} into the flat MCP create_book payload."""
    book = res.get("book", {}) if isinstance(res, dict) else {}
    out = dict(book)
    if isinstance(res, dict) and "bible" in res:
        out["bible"] = res["bible"]
    # `chapters` convenience alias for chapters_total (agents look for it).
    out.setdefault("chapters", out.get("chapters_total", 0))
    return out


def _map_service_errors(call):
    """Run a delegated service call, mapping a 404 ServiceError to BookNotFound."""
    try:
        return call()
    except Exception as e:
        status = getattr(e, "status", None)
        if status == 404:
            raise BookNotFound(getattr(e, "detail", str(e)))
        raise


# ---------------------------------------------------------------------------
# Self-contained local service — the source of truth for the fallback path.
# Implements the HTTP contract's data layout directly on the core package.
# ---------------------------------------------------------------------------


class BookNotFound(Exception):
    pass


# How long a mutation waits for the cross-process book write lock before
# failing (matches the HTTP service's _MUTATE_LOCK_TIMEOUT): long enough to
# ride out another quick op, short enough that colliding with a full write
# job fails fast.
_MUTATE_LOCK_TIMEOUT = 0.5


class _LocalBookService:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    # ---- paths / meta -------------------------------------------------
    def _book_dir(self, book_id: str) -> str:
        _validate_book_id(book_id)
        return os.path.join(self.root, book_id)

    def _meta_path(self, book_id: str) -> str:
        return os.path.join(self._book_dir(book_id), "meta.json")

    def _read_meta(self, book_id: str) -> Dict[str, Any]:
        p = self._meta_path(book_id)
        if not os.path.exists(p):
            raise BookNotFound(book_id)
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        from bookwriter.store import _write_json
        os.makedirs(self._book_dir(meta["id"]), exist_ok=True)
        _write_json(self._meta_path(meta["id"]), meta)  # atomic, matches HTTP service

    @contextlib.contextmanager
    def _write_lock(self, book_id: str):
        """Cross-process writer lock for one book directory.

        The HTTP server may be running against the same data dir in another
        process; its in-memory broker guard can't see our writes (nor ours
        its), so every on-disk mutation coordinates through the OS-level lock
        in ``bookwriter.locks`` — the same lock BookService holds for the
        duration of an HTTP write job. Contention maps to the busy-book
        RuntimeError shape the MCP error contract already uses.
        """
        from bookwriter.locks import BookLockedError, book_write_lock
        try:
            with book_write_lock(self._book_dir(book_id), timeout=_MUTATE_LOCK_TIMEOUT):
                yield
        except BookLockedError:
            raise RuntimeError(
                f"A write job is already running for {book_id!r}."
            )

    # ---- core wiring --------------------------------------------------
    def _settings(self, book_id: str, meta: Dict[str, Any]) -> Any:
        from bookwriter.config import Settings

        s = Settings(project_dir=self._book_dir(book_id)).with_profile(
            meta.get("profile", "balanced")
        )
        if "use_cache" in meta:
            s.use_cache = bool(meta["use_cache"])
        if "run_continuity_check" in meta:
            s.run_continuity_check = bool(meta["run_continuity_check"])
        s.chapter_images = bool(meta.get("chapter_images", False))
        return s

    @staticmethod
    def _make_image_provider(meta: Dict[str, Any]):
        """Image backend for the write job (parity with the HTTP service), or None
        if this book didn't opt in or no provider is configured."""
        if not meta.get("chapter_images"):
            return None
        from bookwriter.images import image_available, make_image_provider
        if not image_available():
            return None
        try:
            return make_image_provider()
        except Exception:
            return None

    def _make_llm(self, mock: bool, meta: Optional[Dict[str, Any]] = None) -> Any:
        from bookwriter.provider import make_llm

        meta = meta or {}
        # Honor the book's per-book provider/model (persisted in meta.json by the
        # HTTP service) so a book written via MCP uses the same backend it would
        # via HTTP — not just the env default.
        return make_llm(
            mock=mock,
            provider=(meta.get("provider") or None),
            model=(meta.get("model") or None),
        )

    def _store(self, book_id: str) -> Any:
        from bookwriter.store import BookStore

        return BookStore(self._book_dir(book_id))

    # ---- summaries ----------------------------------------------------
    def _summary(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        book_id = meta["id"]
        store = self._store(book_id)
        graph = store.load_graph()
        total = 0
        written = 0
        words = 0
        title = meta.get("title", "")
        genre = meta.get("genre", "")
        logline = meta.get("logline", "")
        if graph is not None:
            total = len(graph.bible.outline)
            # Use on-disk truth (store.has_chapter) — the same definition the
            # HTTP BookService._summary uses — so chapters_written and words match
            # across the MCP and HTTP surfaces for the same book.
            for p in graph.bible.outline:
                if store.has_chapter(p.number):
                    written += 1
                    words += _chapter_word_count(store, p.number)
            title = graph.bible.title or title
            genre = graph.bible.genre or genre
            logline = graph.bible.logline or logline
        return {
            "id": book_id,
            "title": title,
            "logline": logline,
            "genre": genre,
            "chapters_total": total,
            "chapters_written": written,
            "words": words,
            "created_at": meta.get("created_at", ""),
            "profile": meta.get("profile", "balanced"),
            "mock": bool(meta.get("mock", False)),
        }

    def _iter_meta(self):
        if not os.path.isdir(self.root):
            return
        for name in sorted(os.listdir(self.root)):
            mp = os.path.join(self.root, name, "meta.json")
            if os.path.exists(mp):
                try:
                    with open(mp, "r", encoding="utf-8") as f:
                        yield json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue

    # ---- operations ---------------------------------------------------
    def profiles(self) -> Dict[str, Any]:
        return _profiles_payload()

    def list_books(self) -> List[Dict[str, Any]]:
        return [self._summary(m) for m in self._iter_meta()]

    def create_book(self, *, premise: str, chapters: Optional[int] = None,
                    words_per_chapter: int = 2000, title: str = "",
                    genre: str = "", guidance: str = "", profile: str = "balanced",
                    mock: bool = False, use_cache: bool = True,
                    run_continuity_check: bool = True, provider: str = "",
                    model: str = "", chapter_images: bool = False) -> Dict[str, Any]:
        if not premise or not premise.strip():
            raise ValueError("premise is required")
        from bookwriter.config import QUALITY_PROFILES
        if profile not in QUALITY_PROFILES:
            raise ValueError(f"unknown profile {profile!r}; choose from {sorted(QUALITY_PROFILES)}")
        if not mock:
            from bookwriter.provider import live_available, missing_credentials_message
            if not live_available(provider or None):
                raise PermissionError(missing_credentials_message(provider or None))
        # id from the provided title, or a slug of the premise as a fallback.
        seed_title = title.strip() or premise.strip()[:48]
        book_id = make_book_id(
            seed_title,
            exists=lambda bid: os.path.exists(self._meta_path(bid)),
        )
        meta = {
            "id": book_id,
            "title": title.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile,
            "mock": bool(mock),
            "genre": genre,
            "logline": "",
            "provider": provider or "",   # parity with the HTTP service meta shape
            "model": model or "",
            "chapter_images": bool(chapter_images),
            "use_cache": bool(use_cache),
            "run_continuity_check": bool(run_continuity_check),
        }
        self._write_meta(meta)

        from bookwriter.pipeline import BookPipeline

        try:
            settings = self._settings(book_id, meta)
            llm = self._make_llm(mock, meta)
            pipe = BookPipeline(llm, settings)
            bible = pipe.plan(
                premise=premise,
                chapters=chapters,
                words_per_chapter=words_per_chapter,
                title=title or None,
                genre=genre or None,
                extra_guidance=guidance or "",
            )
        except Exception:
            import shutil
            shutil.rmtree(self._book_dir(book_id), ignore_errors=True)
            raise
        # backfill meta from the planned bible
        meta["title"] = bible.title or meta["title"]
        meta["genre"] = bible.genre or meta["genre"]
        meta["logline"] = bible.logline or meta["logline"]
        self._write_meta(meta)

        out = self._summary(meta)
        out["chapters"] = out["chapters_total"]
        out["bible"] = bible.to_dict()
        return out

    def write_book(self, book_id: str, only: Optional[List[int]] = None,
                   restart: bool = False, on_event=None) -> Dict[str, Any]:
        meta = self._read_meta(book_id)
        self._require_creds(meta)  # clean error (not a deep SDK auth crash) if no creds
        from bookwriter.pipeline import BookPipeline

        flags: List[str] = []

        def _on_event(ev: Dict[str, Any]) -> None:
            if ev.get("type") == "chapter_done":
                flags.extend(ev.get("flags", []))
            if on_event is not None:
                on_event(ev)

        # Hold the cross-process lock for the whole pipeline run, exactly like
        # the HTTP write job — an HTTP POST /write in another process 409s
        # instead of interleaving chapter/graph saves with ours.
        with self._write_lock(book_id):
            settings = self._settings(book_id, meta)
            llm = self._make_llm(bool(meta.get("mock", False)), meta)

            # stream_prose only when someone is listening (broker viewers); the
            # extra delta events are wasted work for a pure local call.
            pipe = BookPipeline(
                llm, settings, on_event=_on_event, stream_prose=on_event is not None,
                image_provider=self._make_image_provider(meta),
            )
            if not pipe.load():
                raise BookNotFound(f"{book_id} has no plan; create_book first")

            resume = not restart
            ledger = pipe.write_all(resume=resume, only=only)

            snap = pipe._cost_snapshot()
        status = self.get_status(book_id)
        return {
            "book_id": book_id,
            "chapters_written": status["chapters_written"],
            "chapters_total": status["chapters_total"],
            "cost": snap,
            "flags": flags,
        }

    def get_book(self, book_id: str) -> Dict[str, Any]:
        meta = self._read_meta(book_id)
        store = self._store(book_id)
        graph = store.load_graph()
        bible = graph.bible.to_dict() if graph is not None else None
        chapters: List[Dict[str, Any]] = []
        if graph is not None:
            for p in graph.bible.outline:
                rec = graph.chapters.get(p.number)
                chapters.append({
                    "number": p.number,
                    "title": rec.title if rec else p.title,
                    "act": p.act,
                    "written": store.has_chapter(p.number),
                    "word_count": rec.word_count if rec else 0,
                    "has_image": store.has_image(p.number),  # parity with HTTP get_book
                })
        return {
            "book": self._summary(meta),
            "bible": bible,
            "chapters": chapters,
            "cost": self._cost_snapshot_or_none(book_id),
        }

    def get_status(self, book_id: str) -> Dict[str, Any]:
        meta = self._read_meta(book_id)
        s = self._summary(meta)
        return {
            "book_id": book_id,
            "chapters_total": s["chapters_total"],
            "chapters_written": s["chapters_written"],
        }

    def get_chapter(self, book_id: str, number: int) -> Dict[str, Any]:
        self._read_meta(book_id)
        store = self._store(book_id)
        graph = store.load_graph()
        if graph is None:
            raise BookNotFound(f"{book_id} has no plan")
        plan = graph.bible.plan(number)
        if plan is None:
            # Match the HTTP 404 for an out-of-outline chapter number.
            raise BookNotFound(f"{book_id} chapter {number} not in outline")
        rec = graph.chapters.get(number)
        has_image = store.has_image(number)
        return {
            "number": number,
            "title": (rec.title if rec else (plan.title if plan else "")),
            "text": rec.text if rec else "",
            "word_count": rec.word_count if rec else 0,
            "synopsis_line": rec.synopsis_line if rec else "",
            "fingerprint": rec.fingerprint if rec else "",
            "written": store.has_chapter(number),
            "has_image": has_image,  # parity with HTTP get_chapter
            "image_url": f"/api/books/{book_id}/chapters/{number}/image" if has_image else "",
            "plan": plan.to_dict() if plan else None,
        }

    def get_graph(self, book_id: str) -> Dict[str, Any]:
        self._read_meta(book_id)
        graph = self._store(book_id).load_graph()
        if graph is None:
            raise BookNotFound(f"{book_id} has no plan")
        b = graph.bible
        return {
            "characters": [c.to_dict() for c in b.characters],
            "locations": [l.to_dict() for l in b.locations],
            "items": [i.to_dict() for i in b.items],
            "threads": [t.to_dict() for t in b.threads],
            "timeline": [e.to_dict() for e in graph.timeline],
            "synopsis": list(graph.synopsis),
        }

    def _cost_snapshot_or_none(self, book_id: str) -> Optional[Dict[str, Any]]:
        p = os.path.join(self._book_dir(book_id), "cost.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return {
            "total_cost": data.get("total_cost", 0.0),
            "words": data.get("words", 0),
            "by_stage": data.get("by_stage", {}),
            "tokens": data.get("tokens", {}),
            "cache_savings": data.get("cache_savings", 0.0),
        }

    def get_cost(self, book_id: str) -> Dict[str, Any]:
        self._read_meta(book_id)
        report = ""
        rp = os.path.join(self._book_dir(book_id), "cost.txt")
        if os.path.exists(rp):
            with open(rp, "r", encoding="utf-8") as f:
                report = f.read()
        return {"snapshot": self._cost_snapshot_or_none(book_id), "report": report}

    def get_manuscript(self, book_id: str) -> Dict[str, Any]:
        self._read_meta(book_id)
        graph = self._store(book_id).load_graph()
        if graph is None:
            # Match the HTTP 404 and the sibling local reads (get_chapter/get_graph)
            # so "no plan yet" is distinguishable from "empty book".
            raise BookNotFound(f"{book_id} has no plan")
        md = self._store(book_id).assemble_manuscript(graph)
        return {"markdown": md, "words": len(md.split())}

    # ---- import pre-written material + modify chapters -----------------
    def import_book(self, *, text: str, title: str = "", genre: str = "",
                    guidance: str = "", profile: str = "balanced",
                    analyze: bool = True, mock: bool = False) -> Dict[str, Any]:
        from bookwriter.importer import build_graph_from_text
        from bookwriter.costs import CostLedger
        if not (text or "").strip():
            raise ValueError("text is required")
        from bookwriter.config import QUALITY_PROFILES
        if profile not in QUALITY_PROFILES:
            raise ValueError(f"unknown profile {profile!r}; choose from {sorted(QUALITY_PROFILES)}")
        book_id = make_book_id(title.strip() or "Imported manuscript",
                              exists=lambda bid: os.path.exists(self._meta_path(bid)))
        meta = {
            "id": book_id, "title": title.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile, "mock": bool(mock), "genre": genre,
            "logline": "", "provider": "", "model": "", "chapter_images": False,
            "use_cache": True, "run_continuity_check": True, "imported": True,
        }
        self._write_meta(meta)
        try:
            settings = self._settings(book_id, meta)
            # Mirror the HTTP service: with no creds (and not mock) fall back to
            # a structure-only import instead of failing mid-import on a raw SDK
            # auth error — the chapters are still imported and fully editable.
            from bookwriter.provider import live_available
            if mock:
                llm = self._make_llm(True, meta)
            elif analyze and live_available():
                llm = self._make_llm(False, meta)
            else:
                llm, analyze = None, False
            graph = build_graph_from_text(
                llm, settings, CostLedger(), text=text, title=title or None,
                genre=genre or None, guidance=guidance, analyze=analyze, run_extract=analyze)
        except Exception:
            import shutil
            shutil.rmtree(self._book_dir(book_id), ignore_errors=True)
            raise
        # The id minter is deterministic (slug + title hash), so two concurrent
        # imports of the same title target the same dir — serialize the saves.
        # Acquired only after the analyze step so the failure-path rmtree above
        # never runs with an open lock handle inside the dir (Windows).
        with self._write_lock(book_id):
            store = self._store(book_id)
            store.save_graph(graph)
            for rec in graph.chapters.values():
                store.save_chapter(rec)
            store.assemble_manuscript(graph)
            meta["title"] = (title or graph.bible.title) or meta["title"]
            meta["genre"] = genre or graph.bible.genre or meta["genre"]
            meta["logline"] = graph.bible.logline or ""
            self._write_meta(meta)
        out = self._summary(meta)
        out["chapters"] = out["chapters_total"]
        out["bible"] = graph.bible.to_dict()
        return out

    def edit_chapter(self, book_id: str, number: int, *, text: str,
                     title: str = "", reextract: bool = False) -> Dict[str, Any]:
        from bookwriter.models import ChapterRecord
        meta = self._read_meta(book_id)
        with self._write_lock(book_id):
            store = self._store(book_id)
            graph = self._load_graph_or_404(book_id)
            plan = graph.bible.plan(number)
            if plan is None:
                raise BookNotFound(f"{book_id} chapter {number} not in outline")
            if not (text or "").strip():
                raise ValueError("text is required")
            rec = graph.chapters.get(number) or ChapterRecord(number=number, title=plan.title, text="")
            rec.text = text.strip()
            if title:
                rec.title = title.strip()
                plan.title = title.strip()
            rec.word_count = len(rec.text.split())
            rec.compute_fingerprint()
            graph.chapters[number] = rec
            settings = self._settings(book_id, meta)
            synopsis = rec.synopsis_line
            if reextract:
                try:
                    from bookwriter.extractor import extract_delta
                    from bookwriter.costs import CostLedger
                    delta = extract_delta(self._make_llm(bool(meta.get("mock", False)), meta),
                                          settings, CostLedger(), graph, plan, rec)
                    graph.apply_delta(delta)
                    synopsis = delta.synopsis_line
                except Exception:
                    pass
            graph.record_chapter(rec, synopsis, settings.synopsis_line_chars)
            store.save_chapter(rec)
            store.save_graph(graph)
            store.assemble_manuscript(graph)
            return {"number": number, "title": rec.title, "word_count": rec.word_count}

    def _require_creds(self, meta: Dict[str, Any]) -> None:
        """Raise PermissionError when a live (non-mock) op has no provider creds —
        mirrors the HTTP service's clean 400 instead of an uncaught auth error."""
        if bool(meta.get("mock", False)):
            return
        from bookwriter.provider import live_available, missing_credentials_message
        prov = meta.get("provider") or None
        if not live_available(prov):
            raise PermissionError(missing_credentials_message(prov))

    def revise_chapter(self, book_id: str, number: int, *,
                       instructions: str = "") -> Dict[str, Any]:
        from bookwriter.writer import revise_chapter as _revise
        from bookwriter.costs import CostLedger
        meta = self._read_meta(book_id)
        self._require_creds(meta)
        with self._write_lock(book_id):
            store = self._store(book_id)
            graph = self._load_graph_or_404(book_id)
            plan = graph.bible.plan(number)
            rec = graph.chapters.get(number)
            if plan is None or rec is None:
                raise BookNotFound(f"{book_id} chapter {number} not written yet")
            settings = self._settings(book_id, meta)
            new_rec = _revise(self._make_llm(bool(meta.get("mock", False)), meta),
                              settings, CostLedger(), graph, plan, rec.text, instructions or "")
            graph.chapters[number] = new_rec
            graph.record_chapter(new_rec, new_rec.synopsis_line, settings.synopsis_line_chars)
            store.save_chapter(new_rec)
            store.save_graph(graph)
            store.assemble_manuscript(graph)
            return {"number": number, "title": new_rec.title, "word_count": new_rec.word_count}

    def append_chapters(self, book_id: str, *, count: int = 3,
                        words_per_chapter: int = 2000, guidance: str = "") -> Dict[str, Any]:
        from bookwriter.planner import extend_outline
        from bookwriter.costs import CostLedger
        meta = self._read_meta(book_id)
        self._require_creds(meta)
        with self._write_lock(book_id):
            store = self._store(book_id)
            graph = self._load_graph_or_404(book_id)
            new_plans = extend_outline(self._make_llm(bool(meta.get("mock", False)), meta),
                                       self._settings(book_id, meta), CostLedger(), graph,
                                       count=count, words_per_chapter=words_per_chapter,
                                       guidance=guidance)
            graph.bible.outline.extend(new_plans)
            graph.bible.target_chapters = len(graph.bible.outline)
            store.save_graph(graph)
            return {"added": [{"number": p.number, "title": p.title} for p in new_plans],
                    "chapters_total": len(graph.bible.outline)}

    # ---- KDP packaging ------------------------------------------------
    def _kdp_dir(self, book_id: str) -> str:
        return os.path.join(self._book_dir(book_id), "kdp")

    def prepare_kdp(self, book_id: str, *, author_first: str, author_last: str,
                    language: str = "English", subtitle: str = "",
                    series: str = "", series_part: str = "", edition: str = "",
                    contributors: Optional[List[Dict[str, str]]] = None,
                    publishing_rights: str = "owned", sexually_explicit: bool = False,
                    reading_age_min: str = "", reading_age_max: str = "",
                    ) -> Dict[str, Any]:
        """Generate KDP metadata + build the upload kit into <book>/kdp/.

        Returns {"metadata": <dict>, "listing": <copy-paste text>,
        "paths": {metadata, epub, cover, listing, checklist}}.
        """
        from bookwriter.kdp import (
            generate_kdp_metadata, build_kdp_kit, _listing_text,
        )
        from bookwriter.costs import CostLedger
        from bookwriter.store import _write_json

        meta = self._read_meta(book_id)
        self._require_creds(meta)
        graph = self._store(book_id).load_graph()
        if graph is None:
            raise BookNotFound(f"{book_id} has no plan; create_book first")
        if not graph.chapters:
            # Parity with HTTP (service.py 404): don't build an empty EPUB.
            raise BookNotFound(f"{book_id} has no written chapters; write_book first")

        settings = self._settings(book_id, meta)
        llm = self._make_llm(bool(meta.get("mock", False)), meta)
        ledger = CostLedger()

        kdp_meta = generate_kdp_metadata(
            llm, settings, ledger, graph,
            author_first=author_first,
            author_last=author_last,
            language=language or "English",
            subtitle=subtitle,
            series=series,
            edition=edition,
            contributors=contributors,
        )
        # User-set identity fields the generator doesn't produce (parity with HTTP).
        kdp_meta.series_part = series_part or ""
        kdp_meta.publishing_rights = (
            "public_domain" if publishing_rights == "public_domain" else "owned")
        kdp_meta.sexually_explicit = bool(sexually_explicit)
        kdp_meta.reading_age_min = reading_age_min or ""  # parity with HTTP service
        kdp_meta.reading_age_max = reading_age_max or ""
        # Include any generated chapter images (parity with the HTTP path).
        images = self._store(book_id).collect_images(
            [p.number for p in graph.bible.outline])
        # Reuse marketing copy cached by an earlier generate_marketing so the
        # kit ships it (and its path) instead of silently dropping it. Prefer
        # the kit location (kdp/marketing.json, written by both surfaces and by
        # build_kdp_kit) but fall back to <book>/marketing.json, where older
        # HTTP builds cached their snapshot — either copy gets reused.
        marketing = None
        for mk_path in (os.path.join(self._kdp_dir(book_id), "marketing.json"),
                        os.path.join(self._book_dir(book_id), "marketing.json")):
            if os.path.exists(mk_path):
                try:
                    with open(mk_path, "r", encoding="utf-8") as f:
                        marketing = json.load(f)
                    break
                except (OSError, json.JSONDecodeError):
                    marketing = None
        kit = build_kdp_kit(graph, kdp_meta, self._kdp_dir(book_id),
                            images=images, marketing=marketing)
        # Persist <book>/kdp.json so the HTTP readers (get_kdp / epub_path /
        # interior) find MCP-prepared metadata — shared filesystem state.
        _write_json(os.path.join(self._book_dir(book_id), "kdp.json"), kit["metadata"])
        return {
            "metadata": kit["metadata"],
            "listing": _listing_text(kdp_meta),
            "paths": kit["paths"],
        }

    def get_kdp(self, book_id: str) -> Dict[str, Any]:
        """Return the copy-paste KDP listing text for an already-prepared kit."""
        self._read_meta(book_id)
        listing = os.path.join(self._kdp_dir(book_id), "kdp-listing.txt")
        if not os.path.exists(listing):
            raise BookNotFound(f"{book_id} has no KDP kit; run prepare_kdp first")
        with open(listing, "r", encoding="utf-8") as f:
            return {"listing": f.read()}

    def epub_path(self, book_id: str) -> str:
        """Return the path to the book's KDP-ready EPUB, building it on demand.

        Mirrors the HTTP GET /export/epub contract: prefer the kit built by
        prepare_kdp; when it's missing, build the EPUB from saved KDP metadata
        (or a minimal title-only one) — no LLM call needed.
        """
        self._read_meta(book_id)
        epub = os.path.join(self._kdp_dir(book_id), "manuscript.epub")
        if os.path.exists(epub):
            return epub
        graph = self._load_graph_or_404(book_id)
        if not graph.chapters:
            raise BookNotFound(f"{book_id} has no written chapters; write_book first")
        from bookwriter.kdp import build_epub
        km = self._kdp_meta_or_minimal(book_id, graph)
        images = self._store(book_id).collect_images(
            [p.number for p in graph.bible.outline])
        # Build the bytes BEFORE opening the file: open("wb") first would leave
        # a 0-byte manuscript.epub behind if build_epub raised, and every later
        # call would return that empty file as "already built".
        data = build_epub(graph, km, images=images)
        os.makedirs(self._kdp_dir(book_id), exist_ok=True)
        with open(epub, "wb") as f:
            f.write(data)
        return epub

    # ---- print / royalties / marketing --------------------------------
    def _kdp_metadata(self, book_id: str, graph) -> Any:
        """Return a KdpMetadata for the book.

        Reuses the kit's metadata.json (written by prepare_kdp) when present so
        the print interior / marketing match the already-prepared listing; else
        generates metadata fresh from the continuity graph. The author identity
        on a fresh build falls back to the book title's implied author only if
        prepare_kdp has never run — callers wanting real author fields should run
        prepare_kdp first.
        """
        from bookwriter.kdp import KdpMetadata, generate_kdp_metadata
        from bookwriter.costs import CostLedger

        meta_json = os.path.join(self._kdp_dir(book_id), "metadata.json")
        if os.path.exists(meta_json):
            with open(meta_json, "r", encoding="utf-8") as f:
                return KdpMetadata.from_dict(json.load(f))

        meta = self._read_meta(book_id)
        settings = self._settings(book_id, meta)
        llm = self._make_llm(bool(meta.get("mock", False)), meta)
        return generate_kdp_metadata(
            llm, settings, CostLedger(), graph,
            author_first="", author_last="",
        )

    def _load_graph_or_404(self, book_id: str):
        graph = self._store(book_id).load_graph()
        if graph is None:
            raise BookNotFound(f"{book_id} has no plan; create_book first")
        return graph

    def export_docx_path(self, book_id: str, *, trim=(6.0, 9.0)) -> str:
        """Build the 6x9 paperback interior DOCX into <book>/kdp/print/ and return its path."""
        from bookwriter.print_export import build_docx

        self._read_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        kdp_meta = self._kdp_metadata(book_id, graph)
        print_dir = os.path.join(self._kdp_dir(book_id), "print")
        os.makedirs(print_dir, exist_ok=True)
        path = os.path.join(print_dir, "interior.docx")
        with open(path, "wb") as f:
            f.write(build_docx(graph, kdp_meta, trim=trim))
        return path

    def print_spec(self, book_id: str, *, trim=(6.0, 9.0),
                   paper: str = "white") -> Dict[str, Any]:
        """Return trim, page-count estimate, spine width and full-cover dimensions."""
        from bookwriter.print_export import print_spec as _print_spec

        self._read_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        kdp_meta = self._kdp_metadata(book_id, graph)
        return _print_spec(graph, kdp_meta, trim=trim, paper=paper)

    def estimate_pricing(self, book_id: str, *, list_price: float,
                         marketplace: str = "US", paper: str = "white",
                         trim=(6.0, 9.0)) -> Dict[str, Any]:
        """Estimate ebook + paperback per-sale royalties for a list price."""
        from bookwriter.royalties import estimate_page_count, estimate_royalties

        self._read_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        pages = estimate_page_count(graph)
        # Use the real built EPUB's size for the delivery-fee math when one
        # exists; else keep the 1.0 MB default assumption.
        ebook_file_mb = 1.0
        epub = os.path.join(self._kdp_dir(book_id), "manuscript.epub")
        if os.path.isfile(epub):
            ebook_file_mb = os.path.getsize(epub) / 1e6
        return estimate_royalties(
            list_price=float(list_price),
            marketplace=marketplace or "US",
            page_count=pages,
            trim=trim,
            paper=paper or "white",
            ebook_file_mb=ebook_file_mb,
        )

    # ---- AI cover / back cover / PDF (parity with the HTTP service) --------
    def _kdp_meta_or_minimal(self, book_id: str, graph) -> Any:
        """Saved KDP metadata if prepare_kdp ran, else a minimal one — WITHOUT an
        LLM call (covers/PDFs only need title/author + any saved copy)."""
        from bookwriter.kdp import KdpMetadata
        meta_json = os.path.join(self._kdp_dir(book_id), "metadata.json")
        if os.path.exists(meta_json):
            with open(meta_json, "r", encoding="utf-8") as f:
                return KdpMetadata.from_dict(json.load(f))
        return KdpMetadata(title=graph.bible.title or "Untitled",
                           author_first="", author_last="")

    def _load_cover_art(self, book_id: str):
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

    def generate_cover(self, book_id: str, *, title: str = "",
                       subtitle: Optional[str] = None,
                       author_first: Optional[str] = None,
                       author_last: Optional[str] = None) -> Dict[str, Any]:
        """Generate AI cover artwork + composed front cover.svg into <book>/kdp/."""
        self._read_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        from bookwriter.images import image_available, generate_cover_art
        if not image_available():
            raise RuntimeError("No image backend configured — set PIXIO_API_KEY to "
                               "generate an AI cover.")
        art, ext = generate_cover_art(graph.bible)
        ext = (ext or "png").lower().lstrip(".")
        d = self._kdp_dir(book_id)
        os.makedirs(d, exist_ok=True)
        for name in list(os.listdir(d)):
            if name.startswith("cover-art."):
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
        with open(os.path.join(d, f"cover-art.{ext}"), "wb") as f:
            f.write(art)
        km = self._kdp_meta_or_minimal(book_id, graph)
        if title:
            km.title = title
        if subtitle is not None:
            km.subtitle = subtitle
        if author_first is not None:
            km.author_first = author_first
        if author_last is not None:
            km.author_last = author_last
        from bookwriter.kdp import compose_cover_svg
        path = os.path.join(d, "cover.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(compose_cover_svg(km, art, ext))
        return {"path": path, "art_path": os.path.join(d, f"cover-art.{ext}")}

    def back_cover(self, book_id: str) -> Dict[str, Any]:
        """Render the back cover SVG (blurb + bio + imprint) into <book>/kdp/."""
        self._read_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        km = self._kdp_meta_or_minimal(book_id, graph)
        art, ext = self._load_cover_art(book_id)
        from bookwriter.kdp import back_cover_svg
        d = self._kdp_dir(book_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "back-cover.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(back_cover_svg(graph, km, art_bytes=art, ext=ext or "png"))
        return {"path": path}

    def export_pdf(self, book_id: str, part: str = "full", *,
                   trim=(6.0, 9.0)) -> Dict[str, Any]:
        """Build a PDF (interior|front-cover|back-cover|full) into <book>/kdp/."""
        self._read_meta(book_id)
        graph = self._load_graph_or_404(book_id)
        from bookwriter import pdf as _pdf
        if not _pdf.pdf_available():
            raise RuntimeError(_pdf._INSTALL_HINT)
        part = (part or "full").lower()
        if part not in _pdf.PDF_PARTS:
            raise ValueError(f"unknown PDF part {part!r}; choose from {list(_pdf.PDF_PARTS)}")
        if part in ("interior", "full") and not graph.chapters:
            raise BookNotFound(f"{book_id} has no written chapters; write_book first")
        km = self._kdp_meta_or_minimal(book_id, graph)
        art, ext = self._load_cover_art(book_id)
        data = _pdf.build_pdf(part, graph, km, art_bytes=art, ext=ext or "png", trim=trim)
        d = self._kdp_dir(book_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{part}.pdf")
        with open(path, "wb") as f:
            f.write(data)
        return {"path": path}

    def generate_marketing(self, book_id: str) -> Dict[str, Any]:
        """Generate marketing copy (blurbs, A+ modules, bio, taglines) and cache it."""
        from bookwriter.kdp import generate_marketing as _generate_marketing
        from bookwriter.costs import CostLedger

        meta = self._read_meta(book_id)
        self._require_creds(meta)
        graph = self._load_graph_or_404(book_id)
        kdp_meta = self._kdp_metadata(book_id, graph)
        settings = self._settings(book_id, meta)
        llm = self._make_llm(bool(meta.get("mock", False)), meta)
        marketing = _generate_marketing(
            llm, settings, CostLedger(), graph, kdp_meta
        )
        # Cache alongside the kit so a later prepare_kdp/get can reuse it.
        kdp_dir = self._kdp_dir(book_id)
        os.makedirs(kdp_dir, exist_ok=True)
        with open(os.path.join(kdp_dir, "marketing.json"), "w",
                  encoding="utf-8") as f:
            json.dump(marketing, f, indent=2, ensure_ascii=False)
        return marketing


# ---------------------------------------------------------------------------
# Profiles helper (no I/O, pure config) — shared by the tool below.
# ---------------------------------------------------------------------------


def _profiles_payload() -> Dict[str, Any]:
    from bookwriter.config import (
        QUALITY_PROFILES, DEFAULT_PROFILE, MODEL_PRICES,
    )

    profiles = []
    for name, prof in QUALITY_PROFILES.items():
        models = {
            prof.plan.model, prof.write.model, prof.extract.model, prof.check.model,
        }
        prices = {
            m: {"input": MODEL_PRICES[m].input, "output": MODEL_PRICES[m].output}
            for m in models if m in MODEL_PRICES
        }
        profiles.append({
            "name": name,
            "stages": {
                "plan": prof.plan.model,
                "write": prof.write.model,
                "extract": prof.extract.model,
                "check": {"model": prof.check.model, "effort": prof.check.effort},
            },
            "prices": prices,
        })
    return {"default": DEFAULT_PROFILE, "profiles": profiles}


# ---------------------------------------------------------------------------
# MCP server construction (lazy import of `mcp`).
# ---------------------------------------------------------------------------


def build_server():
    """Create and return the FastMCP stdio server with all tools registered.

    Imports ``mcp`` lazily; raises ``ModuleNotFoundError`` if it is missing.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "bookwriter",
        instructions=(
            "Generate full-length books with a continuity-aware pipeline. "
            "Plan a book with create_book, then write_book to generate prose "
            "(synchronous). Use mock=True to run fully offline with no API key. "
            "Inspect progress with get_status, read prose with get_chapter, the "
            "continuity graph with get_graph, spend with get_cost, and the "
            "assembled book with get_manuscript."
        ),
    )

    @mcp.tool()
    def list_profiles() -> Dict[str, Any]:
        """List the available quality profiles (premium/balanced/draft).

        Returns the default profile plus, for each profile, which Claude model
        runs each pipeline stage (plan/write/extract/check) and per-model input/
        output token prices in USD per 1M tokens. Use this to choose a `profile`
        for create_book based on the cost/quality tradeoff you want.
        """
        return get_service().profiles()

    @mcp.tool()
    def list_books() -> Dict[str, Any]:
        """List every book in the shared data directory.

        Returns {"books": [...]} where each entry has id, title, logline, genre,
        chapters_total, chapters_written, created_at, profile and mock. Use the
        `id` field with the other tools.
        """
        return {"books": get_service().list_books()}

    @mcp.tool()
    def create_book(
        premise: str,
        chapters: int = 12,
        words_per_chapter: int = 2000,
        title: str = "",
        genre: str = "",
        guidance: str = "",
        profile: str = "balanced",
        provider: str = "",
        model: str = "",
        chapter_images: bool = False,
        mock: bool = False,
    ) -> Dict[str, Any]:
        """Plan a new book SYNCHRONOUSLY (bible + characters + chapter outline).

        This only PLANS the book; call write_book afterwards to generate prose.

        Args:
            premise: One or more sentences describing the story. Required.
            chapters: Number of chapters to outline (default 12).
            words_per_chapter: Target words per chapter (default 2000).
            title: Optional title; if empty the planner invents one.
            genre: Optional genre hint (e.g. "science fiction", "cozy mystery").
            guidance: Optional extra planning instructions (tone, POV, themes,
                      things to avoid, ...), passed verbatim to the planner.
            profile: Quality profile: "premium", "balanced" (default) or "draft".
                     See list_profiles for the model/cost of each.
            provider: Optional per-book LLM backend (e.g. "anthropic", "openai",
                      "openrouter", "grok", "claude-cli"); empty = server default
                      (BOOKWRITER_LLM_PROVIDER).
            model: Optional per-book text model for the prose stages; empty =
                   the profile's default models.
            chapter_images: If True, generate one inline illustration per
                            chapter during write_book (best-effort; needs the
                            configured image backend).
            mock: If True, run fully offline with a deterministic mock model
                  (no API key, no spend). If False, requires credentials for
                  the chosen provider (an API key or a signed-in CLI — see
                  .env.example).

        Returns the new book's id, title, genre, chapter counts and the full
        planned `bible` dict (characters, locations, outline, ...).
        """
        try:
            return get_service().create_book(
                premise=premise, chapters=chapters,
                words_per_chapter=words_per_chapter, title=title, genre=genre,
                guidance=guidance, profile=profile, provider=provider,
                model=model, chapter_images=chapter_images, mock=mock,
            )
        except PermissionError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

    @mcp.tool()
    def write_book(book_id: str, only: Optional[List[int]] = None,
                   restart: bool = False) -> Dict[str, Any]:
        """Write the book's chapters SYNCHRONOUSLY and return when finished.

        Resumes from the last unwritten chapter by default, so calling it again
        after an interruption continues where it left off.

        Args:
            book_id: The id returned by create_book / list_books.
            only: Optional list of chapter numbers to (re)write, e.g. [1, 2].
                  When omitted, writes all not-yet-written chapters.
            restart: If True, re-run the whole book from chapter 1 instead of
                     resuming (rewrites already-written chapters).

        Returns chapters_written, chapters_total, the cost snapshot
        (total_cost, words, by_stage, tokens, cache_savings) and any continuity
        `flags` raised while writing. A busy book (a write job already running,
        the MCP equivalent of HTTP 409) returns {"error": ...} like every other
        failure.
        """
        try:
            return get_service().write_book(book_id, only=only, restart=restart)
        except (BookNotFound, PermissionError, RuntimeError) as e:
            return {"error": str(e)}
        except Exception as e:  # provider/SDK failure mid-write — keep the
            # documented error shape; partial progress is visible via get_status.
            return {"error": f"write failed: {e}"}

    @mcp.tool()
    def write_chapter(book_id: str, number: int) -> Dict[str, Any]:
        """Write (or rewrite) a single chapter SYNCHRONOUSLY.

        Convenience wrapper over write_book(only=[number]).

        Args:
            book_id: The id returned by create_book / list_books.
            number: The 1-based chapter number to write.

        Returns the same shape as write_book.
        """
        try:
            return get_service().write_book(book_id, only=[number])
        except (BookNotFound, PermissionError, RuntimeError) as e:
            return {"error": str(e)}
        except Exception as e:  # same contract as write_book
            return {"error": f"write failed: {e}"}

    @mcp.tool()
    def get_status(book_id: str) -> Dict[str, Any]:
        """Get write progress for a book.

        Returns {book_id, chapters_total, chapters_written}.
        """
        try:
            return get_service().get_status(book_id)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def get_chapter(book_id: str, number: int) -> Dict[str, Any]:
        """Get a single chapter's prose and metadata.

        Args:
            book_id: The book id.
            number: The 1-based chapter number.

        Returns number, title, text (the prose; empty if not yet written),
        word_count, synopsis_line, fingerprint, written (bool) and the chapter
        `plan` (purpose, beats, tension, forward_hook, ...).
        """
        try:
            return get_service().get_chapter(book_id, number)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def get_graph(book_id: str) -> Dict[str, Any]:
        """Get the continuity knowledge graph for a book.

        Returns characters, locations, items, threads, timeline and the rolling
        synopsis (one line per written chapter). This is the shared source of
        truth the pipeline uses to keep characters and plot consistent.
        """
        try:
            return get_service().get_graph(book_id)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def get_cost(book_id: str) -> Dict[str, Any]:
        """Get token-cost accounting for the most recent write run.

        Returns {"snapshot": {total_cost, words, by_stage, tokens,
        cache_savings} | null, "report": <human-readable cost report or "">}.
        """
        try:
            return get_service().get_cost(book_id)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def get_manuscript(book_id: str) -> Dict[str, Any]:
        """Get the assembled full manuscript as Markdown.

        Returns {"markdown": <full book>, "words": <int>}. Chapters that have
        not been written yet are simply omitted.
        """
        try:
            return get_service().get_manuscript(book_id)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def prepare_kdp(
        book_id: str,
        author_first: str,
        author_last: str,
        language: str = "English",
        subtitle: str = "",
        series: str = "",
        series_part: str = "",
        edition: str = "",
        publishing_rights: str = "owned",
        sexually_explicit: bool = False,
        reading_age_min: str = "",
        reading_age_max: str = "",
    ) -> Dict[str, Any]:
        """Generate Amazon KDP metadata and build the upload-ready kit.

        Runs the KDP listing copywriter over the book's continuity graph to
        generate the marketing fields (description, up to 7 keywords, up to 3
        categories, reading age, series suggestion), enforces every KDP limit in
        Python, then writes the kit into the book's `kdp/` directory:
        metadata.json, manuscript.epub, cover.svg, kdp-listing.txt, CHECKLIST.md.

        Identity fields are carried verbatim from the caller (never invented).
        The book must already be planned AND written (call create_book then
        write_book first) so the EPUB contains the chapter prose.

        Args:
            book_id: The id returned by create_book / list_books.
            author_first: Primary author first name (put a middle name/prefix
                          here too). Pen names allowed.
            author_last: Primary author last name (put a suffix here too).
            language: Book language (default "English"; English is supported).
            subtitle: Optional subtitle; KDP auto-inserts the colon, so this is
                      stored separately from the title. Empty = none. If empty,
                      the model may suggest one.
            series: Optional series name; if empty the model may suggest one.
            edition: Optional edition number (cannot be changed after publish).

        Returns {"metadata": <full page-1 fields dict>, "listing": <copy-paste
        KDP listing text>, "paths": {metadata, epub, cover, listing, checklist}}.
        """
        try:
            return get_service().prepare_kdp(
                book_id,
                author_first=author_first,
                author_last=author_last,
                language=language,
                subtitle=subtitle,
                series=series,
                series_part=series_part,
                edition=edition,
                publishing_rights=publishing_rights,
                sexually_explicit=sexually_explicit,
                reading_age_min=reading_age_min,
                reading_age_max=reading_age_max,
            )
        except (BookNotFound, PermissionError, ValueError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def export_epub(book_id: str) -> Dict[str, Any]:
        """Return the path to the book's KDP-ready EPUB, building it if needed.

        Prefers the kit built by prepare_kdp (which embeds the cover, listing
        metadata and chapter prose). When prepare_kdp hasn't run yet, builds
        the EPUB on demand from the written chapters (same as the HTTP export)
        — the book must be written first.

        Returns {"path": <absolute path to manuscript.epub>}.
        """
        try:
            return {"path": get_service().epub_path(book_id)}
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def get_kdp_listing(book_id: str) -> str:
        """Return the copy-paste KDP listing text (all page-1 fields).

        This is the contents of `kdp-listing.txt` from the kit: every Amazon KDP
        book-details field labeled as KDP shows it (language, title, subtitle,
        series, author, description, rights, categories, keywords, ...), ready to
        paste field by field. Call prepare_kdp first to build the kit.
        """
        try:
            return get_service().get_kdp(book_id)["listing"]
        except BookNotFound as e:
            return f"error: book not found: {e}"

    @mcp.tool()
    def export_docx(book_id: str) -> Dict[str, Any]:
        """Build the 6x9 paperback interior as a Word .docx and return its path.

        Produces the print companion to the EPUB: a valid Office Open XML
        manuscript interior (title page, copyright page, each chapter on a fresh
        6x9 page, 1" margins) written to the book's `kdp/print/interior.docx`.
        Reuses the KDP listing metadata from prepare_kdp when it exists (so the
        title/author/subtitle match the listing); otherwise it generates metadata
        from the continuity graph first. The book must be planned AND written.

        Returns {"path": <absolute path to interior.docx>}.
        """
        try:
            return {"path": get_service().export_docx_path(book_id)}
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def print_spec(book_id: str, paper: str = "white") -> Dict[str, Any]:
        """Compute the paperback print/cover spec (trim, pages, spine, full cover).

        Returns the cover math for a 6x9 KDP paperback: trim size, estimated page
        count (from total written words), spine width (KDP's APPROXIMATE per-page
        constant for the chosen paper), full-wrap cover width/height in inches AND
        the recommended pixel canvas at 300 DPI, plus a notes list. All estimates —
        confirm in KDP's cover calculator.

        Args:
            book_id: The id returned by create_book / list_books.
            paper: "white" (default) or "cream"; cream is marginally thicker.

        Returns the full print-spec dict.
        """
        try:
            return get_service().print_spec(book_id, paper=paper)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def estimate_royalties(
        book_id: str,
        list_price: float,
        marketplace: str = "US",
        paper: str = "white",
    ) -> Dict[str, Any]:
        """Estimate per-sale ebook + paperback royalties for a list price.

        Deterministic, dependency-free KDP estimate. Ebook: Kindle 35% vs 70%
        (70% eligible only for a $2.99-$9.99 US list price, minus a per-MB
        delivery fee), reporting the applicable plan and the alternate for
        comparison. Paperback: 60% of list price minus an APPROXIMATE US B&W
        printing cost derived from the book's estimated page count. All figures
        are ESTIMATES — confirm in the KDP UI.

        Args:
            book_id: The id returned by create_book / list_books.
            list_price: The retail list price in marketplace currency (e.g. 4.99).
            marketplace: Marketplace code (default "US"; figures use US constants).
            paper: "white" (default) or "cream" for the paperback print cost.

        Returns {"ebook": {...}, "paperback": {...}, "assumptions": [...],
        "note": ...}.
        """
        try:
            return get_service().estimate_pricing(
                book_id, list_price=list_price,
                marketplace=marketplace, paper=paper,
            )
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def generate_marketing(book_id: str) -> Dict[str, Any]:
        """Generate ad/listing marketing copy for the book (uses the LLM).

        Runs the marketing copywriter over the book's continuity graph and KDP
        listing to produce copy beyond the single KDP description: up to 3
        alternative blurb variants (different angles), up to 3 Amazon A+ Content
        modules (headline + body), a short third-person author bio, and up to 5
        one-line ad taglines. Every cap is enforced in Python. The result is also
        cached to `kdp/marketing.json` (a later prepare_kdp ships it in the kit).
        Reuses prepare_kdp's metadata when present. Mock books use the
        deterministic mock copywriter; live books need credentials for the
        configured provider (else this returns {"error": ...}).

        Returns {"blurb_variants": [...], "a_plus_modules": [...],
        "author_bio": str, "taglines": [...]}.
        """
        try:
            return get_service().generate_marketing(book_id)
        except (BookNotFound, PermissionError, RuntimeError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def generate_cover(book_id: str, title: str = "", subtitle: str = "",
                       author_first: str = "", author_last: str = "") -> Dict[str, Any]:
        """Generate a catchy AI cover (artwork + title/author typography).

        Calls the configured image backend (default Pixio — needs PIXIO_API_KEY)
        to paint text-free cover ARTWORK from the story bible, then composes a
        finished front cover with the title/author typeset over it. Saves the raw
        art and the composed `kdp/cover.svg`, which prepare_kdp/EPUB/PDF reuse.
        Optional title/subtitle/author override the typography.

        Returns {"path": <cover.svg>, "art_path": <cover-art.png>} or {"error":...}.
        """
        try:
            return get_service().generate_cover(
                book_id, title=title, subtitle=subtitle,
                author_first=author_first, author_last=author_last)
        except (BookNotFound, RuntimeError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def generate_back_cover(book_id: str) -> Dict[str, Any]:
        """Render the back cover (blurb + author bio + imprint + barcode area).

        Uses the KDP metadata's back-cover blurb and author bio (from prepare_kdp)
        and the generated cover artwork as a matching darkened background. Saves
        `kdp/back-cover.svg`. Returns {"path": <back-cover.svg>} or {"error":...}.
        """
        try:
            return get_service().back_cover(book_id)
        except BookNotFound as e:
            return {"error": f"book not found: {e}"}

    @mcp.tool()
    def export_pdf(book_id: str, part: str = "full") -> Dict[str, Any]:
        """Export the book as a PDF and return its path.

        part = "interior" (no cover), "front-cover", "back-cover", or "full"
        (front cover + interior + back cover). Embeds the generated cover artwork
        when present. Needs the optional reportlab dependency (pip install
        bookwriterpro[pdf]); interior/full require written chapters.

        Returns {"path": <pdf path>} or {"error": ...}.
        """
        try:
            return get_service().export_pdf(book_id, part)
        except (BookNotFound, ValueError, RuntimeError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def import_book(text: str, title: str = "", genre: str = "", guidance: str = "",
                    profile: str = "balanced", analyze: bool = True,
                    mock: bool = False) -> Dict[str, Any]:
        """Import pre-written material as a new, fully-editable book.

        Splits the manuscript into chapters (on markdown headings or 'Chapter N'
        lines), reverse-engineers the story bible + continuity from the prose when
        a model is available (analyze=True; mock=True for offline), and records
        every chapter. With analyze=True but no provider credentials it degrades
        to a structure-only import (chapters recorded, no bible analysis) instead
        of failing. `profile` picks the quality profile later operations use
        (see list_profiles). The result is a normal book you can then revise,
        continue, illustrate, and publish. Returns the new book id + planned
        bible (or {"error":...}).
        """
        try:
            return get_service().import_book(
                text=text, title=title, genre=genre, guidance=guidance,
                profile=profile, analyze=analyze, mock=mock)
        except (ValueError, RuntimeError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def edit_chapter(book_id: str, number: int, text: str, title: str = "",
                     reextract: bool = False) -> Dict[str, Any]:
        """Replace a chapter's prose (manual edit). Instant; no model needed.
        Set reextract=True to re-run continuity extraction over the new text.
        Returns {"number","title","word_count"} or {"error":...}.
        """
        try:
            return get_service().edit_chapter(book_id, number, text=text,
                                              title=title, reextract=reextract)
        except (BookNotFound, ValueError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def revise_chapter(book_id: str, number: int, instructions: str = "") -> Dict[str, Any]:
        """AI-revise an existing chapter per `instructions` (or polish if empty),
        keeping it consistent with the bible + continuity. Returns
        {"number","title","word_count"} or {"error":...}.
        """
        try:
            return get_service().revise_chapter(book_id, number, instructions=instructions)
        except (BookNotFound, RuntimeError, PermissionError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def add_chapters(book_id: str, count: int = 3, guidance: str = "") -> Dict[str, Any]:
        """Continue the story: propose & append `count` new outline chapters that
        follow from the current ending. They're left UNwritten — call write_book
        afterward to generate them. Returns {"added":[...],"chapters_total"} or
        {"error":...}.
        """
        try:
            return get_service().append_chapters(book_id, count=count, guidance=guidance)
        except (BookNotFound, RuntimeError, ValueError, PermissionError) as e:
            return {"error": str(e)}

    return mcp


def main() -> None:
    """Entry point for ``python -m bookwriter.mcp_server`` (stdio transport)."""
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        import sys

        sys.stderr.write(
            "The 'mcp' package is required to run the BookwriterPro MCP server.\n"
            "Install it with:\n\n    pip install mcp\n\n"
            "Then run again:\n\n    python -m bookwriter.mcp_server\n"
        )
        raise SystemExit(1)

    server = build_server()
    server.run()  # stdio transport by default


if __name__ == "__main__":
    main()
