"""FastAPI application: JSON API under /api + static frontend at /.

All routes match the authoritative HTTP contract. The SSE endpoint is
hand-rolled (no sse-starlette): a StreamingResponse over an async generator that
replays the broker ring buffer, then tails an asyncio.Queue fed thread-safely by
the background write job, emitting a periodic comment heartbeat.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .broker import EventBroker, TERMINAL_TYPES
from .schemas import (
    AppendChaptersRequest,
    ChapterEditRequest,
    CoverRequest,
    CreateBookRequest,
    ImportRequest,
    KdpRequest,
    MarketingRequest,
    PricingRequest,
    ReviseRequest,
    SettingsUpdate,
    VerifyRequest,
    WriteRequest,
)
from .service import BookService, ServiceError

logger = logging.getLogger(__name__)

# Heartbeat interval (seconds) for the SSE stream.
_HEARTBEAT = 15.0

# Request-body cap: comfortably above the largest legal payload (a 5,000,000-
# char import inside its JSON envelope) while stopping multi-GB memory-DoS
# bodies before Pydantic ever buffers them.
_MAX_BODY_BYTES = 12 * 1024 * 1024

# Methods that change server state; cross-site requests carrying a foreign
# Origin are rejected for these (CSRF / DNS-rebinding hardening).
_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def _allowed_hosts() -> List[str]:
    """Host-header allowlist: local names plus the configured bind host.

    The app is unauthenticated, so an unvalidated Host header enables DNS
    rebinding (a page on attacker.com whose DNS re-resolves to 127.0.0.1 makes
    same-origin requests that skip CORS entirely). ``testserver`` is what
    Starlette's TestClient sends.
    """
    hosts = ["localhost", "127.0.0.1", "::1", "testserver"]
    # Normalize a bracketed IPv6 bind ("[::1]") to the bare address.
    env_host = (os.environ.get("BOOKWRITER_HOST") or "").strip().strip("[]")
    if env_host and env_host not in hosts:
        hosts.append(env_host)
    return hosts


def _host_from_header(value: str) -> str:
    """Hostname from a Host header value: strips the port, and handles IPv6
    literals ("[::1]:8000" -> "::1"), which a naive split(":") does not."""
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else ""
    return value.split(":")[0]


class _HostAllowlistMiddleware:
    """400 for HTTP requests whose Host header is not in the local allowlist.

    Same intent as Starlette's TrustedHostMiddleware (block DNS rebinding), but
    parses IPv6 literal hosts correctly — TrustedHostMiddleware's
    ``host.split(":")[0]`` turns "[::1]:8000" into "[", so a ``::1`` bind (a
    supported local host per serve.py) would reject every request.
    """

    def __init__(self, app: Any, allowed_hosts: List[str]) -> None:
        self.app = app
        self.allowed = {h.lower() for h in allowed_hosts}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            host = ""
            for k, v in scope.get("headers") or []:
                if k == b"host":
                    host = _host_from_header(v.decode("latin-1"))
                    break
            if host.lower() not in self.allowed:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid host header."},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _LocalOriginMiddleware:
    """Reject state-changing requests whose Origin is not a local page.

    CORS only governs whether cross-origin JS may *read* responses; a simple
    no-preflight POST still executes server-side. Requests without an Origin
    header (same-origin navigations, curl, tests) pass through.
    """

    def __init__(self, app: Any, allowed_hostnames: List[str]) -> None:
        self.app = app
        self.allowed = {h.lower() for h in allowed_hostnames}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method") in _STATE_CHANGING_METHODS:
            origin = ""
            for k, v in scope.get("headers") or []:
                if k == b"origin":
                    origin = v.decode("latin-1")
                    break
            if origin and not self._origin_allowed(origin):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-origin request rejected."},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _origin_allowed(self, origin: str) -> bool:
        try:
            hostname = urlsplit(origin).hostname
        except ValueError:
            return False
        return bool(hostname) and hostname.lower() in self.allowed


class _BodySizeLimitMiddleware:
    """413 for request bodies over the cap — before they are fully buffered.

    Checks the declared Content-Length up front, and counts streamed (chunked)
    body bytes as they arrive. The mid-stream abort raises HTTPException, which
    FastAPI explicitly re-raises from middleware so the registered handler
    returns the API's usual ``{"detail": ...}`` shape.
    """

    def __init__(self, app: Any, max_bytes: int = _MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    too_big = int(v) > self.max_bytes
                except ValueError:
                    too_big = False
                if too_big:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large."},
                    )
                    await response(scope, receive, send)
                    return
                break

        seen = 0

        async def limited_receive() -> Dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b"") or b"")
                if seen > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Request body too large.")
            return message

        await self.app(scope, limited_receive, send)


def _content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Content-Disposition with an ASCII fallback + RFC 5987 UTF-8 filename*.

    Starlette encodes header values as latin-1, so a raw non-ASCII filename
    (e.g. a Cyrillic book title) would raise UnicodeEncodeError -> 500. The
    primary ``filename=`` is reduced to ASCII; browsers that understand
    ``filename*`` pick the real UTF-8 name.
    """
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        ext = "." + re.sub(r"[^A-Za-z0-9]+", "", ext)
    else:
        stem, ext = filename, ""
    ascii_stem = re.sub(r"[^A-Za-z0-9-_ ]+", "", stem).strip(" -_.")
    fallback = (ascii_stem or "book") + ext
    header = f'{disposition}; filename="{fallback}"'
    if filename != fallback:
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header


def _default_data_dir() -> str:
    env = os.environ.get("BOOKWRITER_DATA_DIR")
    if env:
        return env
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # In a source checkout `here` is the repo root; for a pip-installed wheel it is
    # site-packages (read-only / wiped on upgrade) — fall back to the user's home.
    if os.path.isfile(os.path.join(here, "pyproject.toml")):
        return os.path.join(here, ".bookwriter_data")
    return os.path.join(os.path.expanduser("~"), ".bookwriter_data")


def create_app(data_dir: str | None = None) -> FastAPI:
    broker = EventBroker()
    resolved_dir = data_dir or _default_data_dir()
    # Persist in-app Settings (API keys / provider choices) next to the books, and
    # make every provider read credentials from this store (overrides env).
    from .. import runtime_config
    runtime_config.bind_file(os.path.join(resolved_dir, "settings.json"))
    service = BookService(resolved_dir, broker=broker)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Bind the running loop so worker threads can schedule queue puts.
        broker.bind_loop(asyncio.get_running_loop())
        yield

    from .. import __version__
    app = FastAPI(title="BookwriterPro", version=__version__, lifespan=_lifespan)
    # Expose the service for embedders/tests (TestClient reaches it via app.state).
    app.state.service = service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        # No cookies/auth are used, so credentialed CORS buys nothing — keep it off
        # to shrink the surface.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # BOOKWRITER_ALLOW_REMOTE=1 is the explicit opt-in for non-local exposure
    # (serve.py requires an auth proxy + TLS in front). Host/Origin enforcement
    # would reject the proxied public hostname (and any 0.0.0.0 bind), so it
    # only applies in the default local-only mode.
    allow_remote = os.environ.get("BOOKWRITER_ALLOW_REMOTE") == "1"
    allowed_hosts = _allowed_hosts()
    if not allow_remote:
        # Reject foreign-Origin state changes (no-preflight CSRF) before they
        # reach the routes.
        app.add_middleware(_LocalOriginMiddleware, allowed_hostnames=allowed_hosts)
    app.add_middleware(_BodySizeLimitMiddleware)
    if not allow_remote:
        # Outermost: an unexpected Host header (DNS rebinding) never reaches the app.
        app.add_middleware(_HostAllowlistMiddleware, allowed_hosts=allowed_hosts)
    else:
        logger.warning(
            "BOOKWRITER_ALLOW_REMOTE=1: Host/Origin request hardening is OFF — "
            "the reverse proxy in front must authenticate requests and enforce "
            "its own Host/Origin checks."
        )

    # -- error handling -------------------------------------------------
    @app.exception_handler(ServiceError)
    async def _service_error(_req: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 body has detail as a list of objects, which the
        # frontend (which expects a string) renders as "[object Object]". Collapse
        # it to a readable single-string message.
        parts = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            msg = e.get("msg", "invalid")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "Invalid request."})

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback server-side; return a generic message so internal
        # details (paths, provider error bodies) never leak to the client.
        logger.exception("Unhandled error on %s", getattr(_req, "url", "?"))
        return JSONResponse(status_code=500, content={"detail": "Internal server error."})

    # ================================================================== #
    # API routes
    # ================================================================== #
    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        from ..provider import provider_name
        return {
            "status": "ok",
            "has_api_key": service.has_api_key(),
            "provider": provider_name(),
        }

    @app.get("/api/profiles")
    async def profiles() -> Dict[str, Any]:
        return service.profiles()

    @app.get("/api/providers")
    async def providers() -> Dict[str, Any]:
        from ..provider import provider_catalog
        from ..images import image_status
        from ..pdf import pdf_available
        cat = provider_catalog()
        cat["image"] = image_status()  # which image backend is active + usable
        cat["pdf"] = {"available": pdf_available()}  # is the optional [pdf] extra installed?
        return cat

    @app.get("/api/settings")
    async def get_settings() -> Dict[str, Any]:
        return service.get_settings()

    @app.put("/api/settings")
    async def save_settings(req: SettingsUpdate) -> Dict[str, Any]:
        return service.save_settings(req.values)

    @app.post("/api/settings/test")
    async def test_settings(req: VerifyRequest) -> Dict[str, Any]:
        # Network call — keep it off the event loop.
        return await asyncio.to_thread(service.verify_provider, req.kind, req.provider)

    @app.get("/api/books")
    async def list_books() -> Dict[str, Any]:
        return service.list_books()

    @app.post("/api/books")
    async def create_book(req: CreateBookRequest) -> Dict[str, Any]:
        # Planning is synchronous and may call the model; run off the event loop.
        return await asyncio.to_thread(service.create_book, req)

    @app.post("/api/books/import")
    async def import_book(req: ImportRequest) -> Dict[str, Any]:
        # Splits + reverse-engineers a bible (model) + records chapters; off-loop.
        return await asyncio.to_thread(service.import_book, req)

    @app.get("/api/books/{book_id}")
    async def get_book(book_id: str) -> Dict[str, Any]:
        return service.get_book(book_id)

    @app.put("/api/books/{book_id}/chapters/{n}")
    async def edit_chapter(book_id: str, n: int, req: ChapterEditRequest) -> Dict[str, Any]:
        # Manual edit; re-extraction (if requested) may call the model — off-loop.
        return await asyncio.to_thread(service.set_chapter_text, book_id, n, req)

    @app.post("/api/books/{book_id}/chapters/{n}/revise")
    async def revise_chapter(book_id: str, n: int, req: ReviseRequest) -> Dict[str, Any]:
        return await asyncio.to_thread(service.revise_chapter, book_id, n, req)

    @app.post("/api/books/{book_id}/outline")
    async def append_chapters(book_id: str, req: AppendChaptersRequest) -> Dict[str, Any]:
        return await asyncio.to_thread(service.append_chapters, book_id, req)

    @app.post("/api/books/{book_id}/write")
    async def write_book(book_id: str, req: WriteRequest) -> Dict[str, Any]:
        return service.start_write(book_id, req)

    @app.get("/api/books/{book_id}/chapters/{n}")
    async def get_chapter(book_id: str, n: int) -> Dict[str, Any]:
        return service.get_chapter(book_id, n)

    @app.get("/api/books/{book_id}/chapters/{n}/image")
    async def get_chapter_image(book_id: str, n: int) -> Response:
        path, media = service.get_chapter_image(book_id, n)
        return FileResponse(path, media_type=media)

    @app.get("/api/books/{book_id}/graph")
    async def get_graph(book_id: str) -> Dict[str, Any]:
        return service.get_graph(book_id)

    @app.get("/api/books/{book_id}/cost")
    async def get_cost(book_id: str) -> Dict[str, Any]:
        return service.get_cost(book_id)

    @app.get("/api/books/{book_id}/manuscript")
    async def get_manuscript(book_id: str, download: int = Query(0)) -> Any:
        # Full-book assembly reads every chapter and writes manuscript.md — keep
        # it off the event loop so SSE streams don't stall while it runs.
        data = await asyncio.to_thread(service.get_manuscript, book_id)
        if download:
            try:
                meta = service._require_meta(book_id)  # noqa: SLF001 - internal helper
                fname = (meta.get("title") or book_id) or book_id
            except ServiceError:
                fname = book_id
            return Response(
                content=data["markdown"],
                media_type="text/markdown",
                headers={"Content-Disposition": _content_disposition(f"{fname}.md")},
            )
        return data

    # -- KDP ------------------------------------------------------------
    @app.post("/api/books/{book_id}/kdp")
    async def prepare_kdp(book_id: str, req: KdpRequest) -> Dict[str, Any]:
        # Generation may call the model + writes the kit; run off the event loop.
        return await asyncio.to_thread(service.prepare_kdp, book_id, req)

    @app.get("/api/books/{book_id}/kdp")
    async def get_kdp(book_id: str) -> Dict[str, Any]:
        return {"metadata": service.get_kdp(book_id)}

    @app.get("/api/books/{book_id}/kdp/listing")
    async def get_kdp_listing(book_id: str) -> Response:
        return Response(
            content=service.kdp_listing(book_id),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/api/books/{book_id}/export/epub")
    async def export_epub(book_id: str) -> FileResponse:
        path = await asyncio.to_thread(service.epub_path, book_id)
        fname = service.epub_filename(book_id)
        return FileResponse(
            path,
            media_type="application/epub+zip",
            headers={"Content-Disposition": _content_disposition(fname)},
        )

    # -- AI cover / back cover / PDF exports ---------------------------
    @app.post("/api/books/{book_id}/cover/generate")
    async def generate_cover(book_id: str, req: CoverRequest) -> Dict[str, Any]:
        # Calls the image provider (network) + writes art; run off the loop.
        return await asyncio.to_thread(service.generate_cover, book_id, req)

    @app.api_route("/api/books/{book_id}/export/back-cover", methods=["GET", "HEAD"])
    async def export_back_cover(book_id: str) -> Response:
        svg = await asyncio.to_thread(service.back_cover_svg, book_id)
        fname = service.docx_filename(book_id).replace(".docx", "-back-cover.svg")
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Content-Disposition": _content_disposition(fname, "inline")},
        )

    @app.get("/api/books/{book_id}/export/pdf")
    async def export_pdf(book_id: str, part: str = Query("full")) -> Response:
        data, fname = await asyncio.to_thread(service.export_pdf, book_id, part)
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": _content_disposition(fname)},
        )

    # -- Print / pricing / marketing -----------------------------------
    @app.get("/api/books/{book_id}/export/docx")
    async def export_docx(book_id: str) -> FileResponse:
        path = await asyncio.to_thread(service.export_docx_path, book_id)
        fname = service.docx_filename(book_id)
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": _content_disposition(fname)},
        )

    @app.get("/api/books/{book_id}/print")
    async def get_print(book_id: str) -> Dict[str, Any]:
        return {"spec": service.print_spec(book_id)}

    @app.api_route("/api/books/{book_id}/export/print-cover", methods=["GET", "HEAD"])
    async def export_print_cover(book_id: str) -> Response:
        svg = await asyncio.to_thread(service.print_cover_svg, book_id)
        fname = service.docx_filename(book_id).replace(".docx", "-print-cover.svg")
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Content-Disposition": _content_disposition(fname, "inline")},
        )

    @app.post("/api/books/{book_id}/pricing")
    async def pricing(book_id: str, req: PricingRequest) -> Dict[str, Any]:
        return {"pricing": service.estimate_pricing(book_id, req)}

    @app.post("/api/books/{book_id}/marketing")
    async def marketing(book_id: str, req: MarketingRequest) -> Dict[str, Any]:
        # Generation may call the model + writes marketing.json; run off the loop.
        result = await asyncio.to_thread(service.generate_marketing, book_id, req)
        return {"marketing": result}

    @app.delete("/api/books/{book_id}")
    async def delete_book(book_id: str) -> Dict[str, Any]:
        return service.delete_book(book_id)

    # -- SSE ------------------------------------------------------------
    @app.get("/api/books/{book_id}/events")
    async def events(book_id: str, request: Request) -> Response:
        # 404 if the book does not exist.
        service._require_meta(book_id)  # noqa: SLF001 - raises ServiceError -> handled

        # Every event carries an ``id:`` (the broker's per-book seq), so an
        # EventSource reconnect sends Last-Event-ID and we can avoid replaying
        # what the client already has.
        raw_last = request.headers.get("last-event-id")
        last_seq = -1
        if raw_last is not None:
            try:
                last_seq = int(raw_last)
            except ValueError:
                last_seq = -1

        q, replay, finished = broker.subscribe(book_id)

        # Terminal semantics: a reconnecting client that already received the
        # finished job's terminal event gets 204, which the EventSource spec
        # defines as "stop reconnecting" — otherwise every idle Studio tab
        # would replay the full ring (megabytes of prose) every ~3 seconds.
        if raw_last is not None and finished:
            max_seq = replay[-1][0] if replay else -1
            if last_seq >= max_seq:
                broker.unsubscribe(book_id, q)
                return Response(status_code=204)

        async def gen():
            try:
                # Replay everything emitted so far for the current/last job
                # that the client has not already seen.
                saw_terminal = False
                for seq, ev in replay:
                    if seq <= last_seq:
                        continue
                    yield _sse(ev, seq)
                    if ev.get("type") in TERMINAL_TYPES:
                        saw_terminal = True
                # If the last job already finished and we replayed its terminal
                # event, we are done — close cleanly.
                if finished and saw_terminal:
                    return
                # Tail live events with a heartbeat so proxies keep the stream open.
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        seq, ev = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ":\n\n"  # comment heartbeat
                        continue
                    yield _sse(ev, seq)
                    if ev.get("type") in TERMINAL_TYPES:
                        return
            finally:
                broker.unsubscribe(book_id, q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ================================================================== #
    # Static frontend
    # ================================================================== #
    @app.get("/")
    async def index() -> Any:
        idx = os.path.join(_WEB_DIR, "index.html")
        if os.path.isfile(idx):
            return FileResponse(idx)
        return JSONResponse(
            status_code=200,
            content={"detail": "Frontend not built yet. API is live under /api."},
        )

    if os.path.isdir(_WEB_DIR):
        # Serve styles.css, app.js, and any other assets from /.
        app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")

    return app


def _sse(event: Dict[str, Any], seq: Optional[int] = None) -> str:
    data = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    if seq is None:
        return data
    return f"id: {seq}\n{data}"
