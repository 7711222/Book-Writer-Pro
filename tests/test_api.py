import json
import os
import tempfile
import time
import unittest
from importlib import import_module
from importlib.util import find_spec

_DEPS = bool(find_spec("fastapi")) and bool(find_spec("httpx"))


def _load_create_app():
    """Import the FastAPI app factory. Returns None if the server module
    isn't present yet (so tests skip rather than error at import)."""
    try:
        mod = import_module("bookwriter.server.api")
    except Exception:
        return None
    return getattr(mod, "create_app", None)


@unittest.skipUnless(_DEPS, "server deps (fastapi, httpx) not installed")
class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_app = _load_create_app()
        if create_app is None:
            raise unittest.SkipTest("bookwriter.server.api.create_app not available")
        cls.create_app = staticmethod(create_app)

    def setUp(self):
        from fastapi.testclient import TestClient

        # Fresh tempdir data root + no API key so MOCK mode is the only path.
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_data = os.environ.get("BOOKWRITER_DATA_DIR")
        self._prev_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["BOOKWRITER_DATA_DIR"] = self._tmp.name
        os.environ.pop("ANTHROPIC_API_KEY", None)

        app = type(self).create_app()
        self.client = TestClient(app)

    def tearDown(self):
        try:
            self.client.close()
        except Exception:
            pass
        if self._prev_data is None:
            os.environ.pop("BOOKWRITER_DATA_DIR", None)
        else:
            os.environ["BOOKWRITER_DATA_DIR"] = self._prev_data
        if self._prev_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_key
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------

    def _create_book(self, **overrides):
        body = {
            "premise": "A lighthouse keeper learns the fog erases memories.",
            "chapters": 3,
            "words_per_chapter": 150,
            "title": "Fog Test",
            "genre": "literary mystery",
            "mock": True,
        }
        body.update(overrides)
        r = self.client.post("/api/books", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _read_terminal(self, book_id, timeout=60.0):
        """Consume the SSE stream (replay + tail) until a terminal event."""
        deadline = time.time() + timeout
        with self.client.stream("GET", f"/api/books/{book_id}/events") as resp:
            self.assertEqual(resp.status_code, 200)
            for line in resp.iter_lines():
                if time.time() > deadline:
                    self.fail("no terminal SSE event before timeout")
                if not line:
                    continue
                text = line if isinstance(line, str) else line.decode("utf-8")
                if text.startswith(":"):  # heartbeat comment
                    continue
                if text.startswith("data:"):
                    payload = text[len("data:"):].strip()
                    if not payload:
                        continue
                    evt = json.loads(payload)
                    if evt.get("type") in ("done", "error"):
                        return evt
        self.fail("no terminal SSE event received")

    def _drive_write_to_completion(self, book_id, timeout=60.0):
        r = self.client.post(f"/api/books/{book_id}/write", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("status"), "started")
        terminal = self._read_terminal(book_id, timeout=timeout)
        self.assertEqual(terminal.get("type"), "done", f"write failed: {terminal}")

    # ---- tests ---------------------------------------------------------

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("status"), "ok")
        self.assertIn("has_api_key", body)
        self.assertIsInstance(body["has_api_key"], bool)
        # No key set in this env.
        self.assertFalse(body["has_api_key"])

    def test_profiles(self):
        r = self.client.get("/api/profiles")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("default"), "balanced")
        profiles = body.get("profiles")
        self.assertIsInstance(profiles, list)
        names = {p["name"] for p in profiles}
        self.assertEqual(names, {"premium", "balanced", "draft"})
        bal = next(p for p in profiles if p["name"] == "balanced")
        for stage in ("plan", "write", "extract", "check"):
            self.assertIn(stage, bal["stages"])
        # Contract: plan/write/extract are BARE model strings; check is an object.
        for stage in ("plan", "write", "extract"):
            self.assertIsInstance(bal["stages"][stage], str)
            self.assertTrue(bal["stages"][stage])
        self.assertIsInstance(bal["stages"]["check"], dict)
        self.assertIn("model", bal["stages"]["check"])
        self.assertIn("effort", bal["stages"]["check"])
        self.assertIn("prices", bal)

    def test_create_requires_key_without_mock(self):
        r = self.client.post(
            "/api/books",
            json={"premise": "x", "chapters": 2, "mock": False},
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("detail", r.json())

    def test_create_book_mock(self):
        data = self._create_book()
        book = data["book"]
        # Title is non-empty; the server may surface either the user-supplied
        # title or the planner's title, so we don't pin the exact string.
        self.assertTrue(book["title"])
        # Genre is present; in mock mode the planner may substitute its own.
        self.assertIn("genre", book)
        self.assertTrue(book["mock"])
        self.assertEqual(book["chapters_total"], 3)
        self.assertEqual(book["chapters_written"], 0)
        self.assertIn("id", book)
        self.assertIn("created_at", book)
        # Bible is the Bible.to_dict() shape with an outline.
        bible = data["bible"]
        self.assertIn("outline", bible)
        self.assertEqual(len(bible["outline"]), 3)

    def test_get_book(self):
        book_id = self._create_book()["book"]["id"]
        r = self.client.get(f"/api/books/{book_id}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["book"]["id"], book_id)
        self.assertIn("bible", body)
        chapters = body["chapters"]
        self.assertEqual(len(chapters), 3)
        c1 = chapters[0]
        for k in ("number", "title", "act", "written", "word_count"):
            self.assertIn(k, c1)
        self.assertFalse(c1["written"])
        # cost is null before any write.
        self.assertIn("cost", body)

    def test_get_book_404(self):
        r = self.client.get("/api/books/does-not-exist-000000")
        self.assertEqual(r.status_code, 404, r.text)

    def test_list_books(self):
        id_a = self._create_book(title="Alpha")["book"]["id"]
        id_b = self._create_book(title="Beta")["book"]["id"]
        r = self.client.get("/api/books")
        self.assertEqual(r.status_code, 200, r.text)
        books = r.json()["books"]
        self.assertGreaterEqual(len(books), 2)
        ids = {b["id"] for b in books}
        self.assertIn(id_a, ids)
        self.assertIn(id_b, ids)
        for b in books:
            for k in ("id", "title", "chapters_total", "chapters_written"):
                self.assertIn(k, b)

    def test_list_books_ignores_settings_file(self):
        # Regression: settings.json (and any non-book file) lives in the data dir.
        # It must NOT make /api/books 404 — that broke the whole library for any
        # user who had ever saved settings.
        id_a = self._create_book(title="Alpha")["book"]["id"]
        # The settings store writes <data_dir>/settings.json; simulate it.
        with open(os.path.join(self._tmp.name, "settings.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        # And a stray non-book directory.
        os.makedirs(os.path.join(self._tmp.name, "not_a_book.tmp"), exist_ok=True)
        r = self.client.get("/api/books")
        self.assertEqual(r.status_code, 200, r.text)
        ids = {b["id"] for b in r.json()["books"]}
        self.assertIn(id_a, ids)
        self.assertNotIn("settings.json", ids)

    def test_write_to_completion_and_artifacts(self):
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)

        # Book now reports all chapters written + a cost snapshot.
        body = self.client.get(f"/api/books/{book_id}").json()
        self.assertEqual(body["book"]["chapters_written"], 3)
        self.assertTrue(all(c["written"] for c in body["chapters"]))
        self.assertIsNotNone(body["cost"])
        self.assertGreater(body["cost"]["total_cost"], 0)

        # Chapter endpoint returns prose + plan.
        rc = self.client.get(f"/api/books/{book_id}/chapters/1")
        self.assertEqual(rc.status_code, 200, rc.text)
        ch = rc.json()
        self.assertEqual(ch["number"], 1)
        self.assertTrue(ch["written"])
        self.assertGreater(ch["word_count"], 0)
        self.assertTrue(ch["text"])
        self.assertIn("plan", ch)

        # Graph populated by the extractor.
        rg = self.client.get(f"/api/books/{book_id}/graph")
        self.assertEqual(rg.status_code, 200, rg.text)
        graph = rg.json()
        for k in ("characters", "locations", "items", "threads", "timeline", "synopsis"):
            self.assertIn(k, graph)

        # Cost report.
        rcost = self.client.get(f"/api/books/{book_id}/cost")
        self.assertEqual(rcost.status_code, 200, rcost.text)
        cost = rcost.json()
        self.assertIn("snapshot", cost)
        self.assertIn("report", cost)
        self.assertGreater(cost["snapshot"]["total_cost"], 0)

        # Manuscript (JSON + download).
        rm = self.client.get(f"/api/books/{book_id}/manuscript")
        self.assertEqual(rm.status_code, 200, rm.text)
        man = rm.json()
        self.assertGreater(man["words"], 0)
        self.assertTrue(man["markdown"])

        rd = self.client.get(f"/api/books/{book_id}/manuscript?download=1")
        self.assertEqual(rd.status_code, 200, rd.text)
        self.assertIn("text/markdown", rd.headers.get("content-type", ""))

    def test_write_conflict_when_running(self):
        # Deterministic: reserve the job slot directly on the app's broker,
        # then a second write AND a delete must both return exactly 409.
        book_id = self._create_book()["book"]["id"]
        svc = self.client.app.state.service
        self.assertTrue(svc.broker.start_job(book_id))
        try:
            r = self.client.post(f"/api/books/{book_id}/write", json={})
            self.assertEqual(r.status_code, 409, r.text)
            rd = self.client.delete(f"/api/books/{book_id}")
            self.assertEqual(rd.status_code, 409, rd.text)
        finally:
            svc.broker.publish(book_id, {"type": "done"})  # release the slot
        # Slot released -> delete succeeds now.
        self.assertEqual(self.client.delete(f"/api/books/{book_id}").status_code, 200)

    def test_write_failure_emits_sanitized_error_and_releases_slot(self):
        # The write job's error branch publishes a terminal SSE 'error' WITHOUT
        # the raw exception text, and releases the job slot for future writes.
        from unittest import mock
        from bookwriter.pipeline import BookPipeline
        book_id = self._create_book()["book"]["id"]
        with mock.patch.object(BookPipeline, "write_all",
                               side_effect=RuntimeError("boom C:/secret/path")):
            r = self.client.post(f"/api/books/{book_id}/write", json={})
            self.assertEqual(r.status_code, 200, r.text)
            terminal = self._read_terminal(book_id)
        self.assertEqual(terminal.get("type"), "error", terminal)
        self.assertEqual(terminal.get("message"), "Generation failed.")
        self.assertNotIn("boom", json.dumps(terminal))
        # Slot + cross-process lock were released: a new write starts and finishes.
        self._drive_write_to_completion(book_id)

    def test_broker_drop_with_live_subscribers(self):
        # drop() keeps the channel while subscribers remain (clearing the ring),
        # and removes it entirely once they are gone.
        from bookwriter.server.broker import EventBroker
        b = EventBroker()
        b.start_job("bk")
        q, replay, finished = b.subscribe("bk")
        b.publish("bk", {"type": "delta", "number": 1, "text": "x"})
        b.publish("bk", {"type": "done"})
        b.drop("bk")
        self.assertIn("bk", b._channels)
        self.assertEqual(len(b._channels["bk"].ring), 0)
        b.unsubscribe("bk", q)
        b.drop("bk")
        self.assertNotIn("bk", b._channels)

    def test_path_traversal_book_id_rejected(self):
        # A traversal id must never escape the data root; the service rejects
        # malformed ids with 404 before any filesystem op.
        from bookwriter.server.service import BookService, ServiceError
        svc = BookService(self._tmp.name)
        for bad in ("..", "../secret", "..\\secret", "a/b", "A_B", "x" * 100):
            with self.assertRaises(ServiceError) as cm:
                svc.get_book(bad)
            self.assertEqual(cm.exception.status, 404)
            with self.assertRaises(ServiceError) as cm2:
                svc.delete_book(bad)
            self.assertEqual(cm2.exception.status, 404)

    def test_minted_ids_are_valid(self):
        # The ids we actually mint pass the validator (round-trip sanity).
        from bookwriter.server.service import validate_book_id
        book_id = self._create_book(title="Round Trip")["book"]["id"]
        self.assertEqual(validate_book_id(book_id), book_id)

    def test_delete_drops_broker_channel(self):
        # Deleting a book releases its in-memory event ring (no leak).
        from bookwriter.server.service import BookService
        svc = BookService(self._tmp.name)
        book_id = svc.create_book(self._req(title="Ephemeral"))["book"]["id"]
        svc.broker.start_job(book_id)
        svc.broker.publish(book_id, {"type": "delta", "number": 1, "text": "x"})
        svc.broker.publish(book_id, {"type": "done"})  # terminal: clears running
        self.assertIn(book_id, svc.broker._channels)
        svc.delete_book(book_id)
        self.assertNotIn(book_id, svc.broker._channels)

    def _req(self, **overrides):
        from bookwriter.server.schemas import CreateBookRequest
        body = {
            "premise": "A small town where the church bell rings on its own.",
            "chapters": 2, "words_per_chapter": 120, "title": "X",
            "genre": "mystery", "mock": True,
        }
        body.update(overrides)
        return CreateBookRequest(**body)

    def test_delete_book(self):
        book_id = self._create_book()["book"]["id"]
        r = self.client.delete(f"/api/books/{book_id}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("status"), "deleted")
        self.assertEqual(self.client.get(f"/api/books/{book_id}").status_code, 404)

    def test_autofill_returns_all_fields(self):
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)
        md = self.client.post(f"/api/books/{book_id}/kdp",
                              json={"author_first": "Vera", "author_last": "Solenne"}).json()["metadata"]
        for k in ("description", "keywords", "categories", "back_cover_blurb",
                  "author_bio", "edition", "series_part"):
            self.assertIn(k, md)
        self.assertTrue(md["back_cover_blurb"])

    def test_pdf_exports(self):
        from bookwriter import pdf as _pdf
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)
        for part in ("interior", "front-cover", "back-cover", "full"):
            r = self.client.get(f"/api/books/{book_id}/export/pdf", params={"part": part})
            if not _pdf.pdf_available():
                self.assertEqual(r.status_code, 501)
                continue
            self.assertEqual(r.status_code, 200, part)
            self.assertEqual(r.content[:5], b"%PDF-", part)
        # unknown part -> 400
        self.assertEqual(
            self.client.get(f"/api/books/{book_id}/export/pdf", params={"part": "nope"}).status_code,
            400 if _pdf.pdf_available() else 501)

    def test_back_cover_svg(self):
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)
        r = self.client.get(f"/api/books/{book_id}/export/back-cover")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/svg+xml", r.headers.get("content-type", ""))
        self.assertIn("<svg", r.text)

    def test_import_edit_revise_append(self):
        ms = ("## Chapter 1: The Light\n" + ("beam " * 40) +
              "\n\n## Chapter 2: The Tide\n" + ("water " * 40))
        # IMPORT (mock so the bible is reverse-engineered offline)
        r = self.client.post("/api/books/import",
                             json={"text": ms, "title": "Imported", "genre": "horror", "mock": True})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        bid = d["book"]["id"]
        self.assertEqual(d["book"]["chapters_total"], 2)
        self.assertEqual(d["book"]["chapters_written"], 2)
        self.assertEqual([p["title"] for p in d["bible"]["outline"]],
                         ["The Light", "The Tide"])
        # EDIT chapter 1 (manual, no model)
        r = self.client.put(f"/api/books/{bid}/chapters/1",
                            json={"text": "A hand-edited opening.", "title": "Light (edited)"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["title"], "Light (edited)")
        self.assertEqual(self.client.get(f"/api/books/{bid}/chapters/1").json()["text"],
                         "A hand-edited opening.")
        # REVISE chapter 2 (mock)
        r = self.client.post(f"/api/books/{bid}/chapters/2/revise",
                            json={"instructions": "tighten", "mock": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(r.json()["word_count"], 0)
        # APPEND 2 chapters (mock)
        r = self.client.post(f"/api/books/{bid}/outline", json={"count": 2, "mock": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["added"]), 2)
        self.assertEqual(self.client.get(f"/api/books/{bid}").json()["book"]["chapters_total"], 4)

    def test_import_empty_rejected(self):
        r = self.client.post("/api/books/import", json={"text": "   ", "mock": True})
        self.assertIn(r.status_code, (400, 422))

    # ---- security hardening ---------------------------------------------

    def test_host_allowlist_blocks_dns_rebinding(self):
        # A foreign Host header (DNS rebinding) never reaches the app.
        r = self.client.get("/api/health", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 400)
        # Local + test hosts pass, with or without a port; IPv6 loopback
        # literals ("[::1]:8000") must parse correctly too.
        for host in ("localhost", "127.0.0.1", "testserver", "localhost:8000",
                     "[::1]", "[::1]:8000"):
            r = self.client.get("/api/health", headers={"Host": host})
            self.assertEqual(r.status_code, 200, host)

    def test_allow_remote_disables_local_host_origin_checks(self):
        # BOOKWRITER_ALLOW_REMOTE=1 (explicit opt-in; auth proxy required in
        # front) must keep the app usable via a public hostname: no Host 400s,
        # no Origin 403s from the remote UI's own POSTs.
        from unittest import mock
        from fastapi.testclient import TestClient
        with mock.patch.dict(os.environ, {"BOOKWRITER_ALLOW_REMOTE": "1"}):
            remote_app = type(self).create_app()
        with TestClient(remote_app) as client:
            r = client.get("/api/health", headers={"Host": "books.example.com"})
            self.assertEqual(r.status_code, 200, r.text)
            r = client.post(
                "/api/books",
                json={"premise": "x", "chapters": 2, "mock": True},
                headers={"Host": "books.example.com",
                         "Origin": "https://books.example.com"},
            )
            self.assertEqual(r.status_code, 200, r.text)

    def test_foreign_origin_state_change_rejected(self):
        body = {"premise": "x", "chapters": 2, "mock": True}
        r = self.client.post("/api/books", json=body,
                             headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("detail", r.json())
        # 'null' Origin (sandboxed iframe / file://) is rejected too.
        r = self.client.post("/api/books", json=body, headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403, r.text)
        # Reads are not blocked by the origin check (CORS governs readability).
        rg = self.client.get("/api/health", headers={"Origin": "http://evil.example.com"})
        self.assertEqual(rg.status_code, 200)
        # A local page may still POST.
        rl = self.client.post("/api/books", json=body,
                              headers={"Origin": "http://localhost:8000"})
        self.assertEqual(rl.status_code, 200, rl.text)

    def test_body_size_limit(self):
        # Declared Content-Length over the cap -> 413 before buffering.
        big = b"x" * (12 * 1024 * 1024 + 1)
        r = self.client.post("/api/books/import", content=big,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413, r.text)
        self.assertEqual(r.json()["detail"], "Request body too large.")

        # Chunked/streamed body (no Content-Length) aborts at the same cap.
        def chunks():
            for _ in range(13):
                yield b"y" * (1024 * 1024)

        r2 = self.client.post("/api/books/import", content=chunks(),
                              headers={"Content-Type": "application/json"})
        self.assertEqual(r2.status_code, 413, r2.text)
        self.assertEqual(r2.json()["detail"], "Request body too large.")

    def test_planning_error_detail_not_leaked(self):
        # Internal exception text (paths, provider bodies) stays server-side;
        # the client gets a short generic message.
        from unittest import mock
        from bookwriter.pipeline import BookPipeline
        with mock.patch.object(BookPipeline, "plan",
                               side_effect=RuntimeError("SECRET C:/private/keys.txt")):
            r = self.client.post("/api/books",
                                 json={"premise": "p", "chapters": 2, "mock": True})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(r.json()["detail"], "Planning failed.")
        self.assertNotIn("SECRET", r.text)

    # ---- SSE terminal semantics -------------------------------------------

    def test_sse_ids_and_reconnect_204_after_terminal(self):
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)

        # Fresh connect: full replay carries id: fields and closes after the
        # terminal event.
        last_id = None
        terminal = None
        with self.client.stream("GET", f"/api/books/{book_id}/events") as resp:
            self.assertEqual(resp.status_code, 200)
            for line in resp.iter_lines():
                text = line if isinstance(line, str) else line.decode("utf-8")
                if text.startswith("id:"):
                    last_id = int(text[len("id:"):].strip())
                elif text.startswith("data:"):
                    payload = text[len("data:"):].strip()
                    if payload:
                        terminal = json.loads(payload)
        self.assertIsNotNone(last_id, "SSE events carry no id: fields")
        self.assertEqual((terminal or {}).get("type"), "done")

        # An EventSource reconnect that already saw the terminal event gets 204
        # (spec: stop reconnecting) instead of replaying the whole ring forever.
        r = self.client.get(f"/api/books/{book_id}/events",
                            headers={"Last-Event-ID": str(last_id)})
        self.assertEqual(r.status_code, 204)

        # A reconnect that missed the tail replays ONLY the missed events.
        payloads = []
        with self.client.stream("GET", f"/api/books/{book_id}/events",
                                headers={"Last-Event-ID": str(last_id - 1)}) as resp2:
            self.assertEqual(resp2.status_code, 200)
            for line in resp2.iter_lines():
                text = line if isinstance(line, str) else line.decode("utf-8")
                if text.startswith("data:") and text[len("data:"):].strip():
                    payloads.append(json.loads(text[len("data:"):].strip()))
        self.assertEqual(len(payloads), 1, payloads)
        self.assertEqual(payloads[0].get("type"), "done")

    # ---- publish / export endpoints ----------------------------------------

    def test_publish_export_endpoints(self):
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)

        # Saved KDP metadata is null and the listing 404s before any prepare.
        r = self.client.get(f"/api/books/{book_id}/kdp")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["metadata"])
        self.assertEqual(
            self.client.get(f"/api/books/{book_id}/kdp/listing").status_code, 404)

        # No chapter image exists for this book -> 404, not 500.
        self.assertEqual(
            self.client.get(f"/api/books/{book_id}/chapters/1/image").status_code, 404)

        # EPUB builds on demand (minimal-metadata fallback path) and is a zip.
        r = self.client.get(f"/api/books/{book_id}/export/epub")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content[:2], b"PK")
        self.assertIn("application/epub+zip", r.headers.get("content-type", ""))

        # DOCX print interior is a zip too.
        r = self.client.get(f"/api/books/{book_id}/export/docx")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content[:2], b"PK")

        # Print spec carries the KDP geometry fields.
        r = self.client.get(f"/api/books/{book_id}/print")
        self.assertEqual(r.status_code, 200, r.text)
        spec = r.json()["spec"]
        self.assertIn("spine_width_in", spec)
        self.assertIn("page_count_estimate", spec)

        # $4.99 is inside the 70% ebook royalty window.
        r = self.client.post(f"/api/books/{book_id}/pricing", json={"list_price": 4.99})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pricing"]["ebook"]["plan"], "70%")

        # Marketing (mock book -> offline LLM) + persisted kdp/marketing.json
        # (the kit location the MCP server shares).
        r = self.client.post(f"/api/books/{book_id}/marketing", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("blurb_variants", r.json()["marketing"])
        self.assertTrue(os.path.isfile(
            os.path.join(self._tmp.name, book_id, "kdp", "marketing.json")))

        # After a prepare, the saved metadata + listing become retrievable.
        r = self.client.post(f"/api/books/{book_id}/kdp",
                             json={"author_first": "Vera", "author_last": "Solenne"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"/api/books/{book_id}/kdp")
        self.assertEqual(r.status_code, 200)
        md = r.json()["metadata"]
        self.assertIsInstance(md, dict)
        self.assertIn("description", md)
        r = self.client.get(f"/api/books/{book_id}/kdp/listing")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r.headers.get("content-type", ""))
        self.assertTrue(r.text.strip())

    def test_unicode_title_download_headers(self):
        # Cyrillic titles used to 500 the manuscript download (latin-1 header
        # encode) and degrade every export filename to 'book.*'.
        book_id = self._create_book(
            title="Книга теней", chapters=2, words_per_chapter=120)["book"]["id"]

        r = self.client.get(f"/api/books/{book_id}/manuscript", params={"download": 1})
        self.assertEqual(r.status_code, 200, r.text)
        cd = r.headers.get("content-disposition", "")
        cd.encode("ascii")  # the header itself must stay ASCII-safe
        self.assertIn('filename="book.md"', cd)          # ASCII fallback
        self.assertIn("filename*=UTF-8''%D0%9A", cd)     # RFC 5987 'К'

        self._drive_write_to_completion(book_id)
        r = self.client.get(f"/api/books/{book_id}/export/epub")
        self.assertEqual(r.status_code, 200, r.text)
        cd = r.headers.get("content-disposition", "")
        self.assertIn('filename="book.epub"', cd)
        self.assertIn("filename*=UTF-8''%D0%9A", cd)

    # ---- cross-process write lock -------------------------------------------

    def test_write_lock_same_process_contention(self):
        from bookwriter.locks import BookLockedError, book_write_lock
        d = os.path.join(self._tmp.name, "lock-unit")
        with book_write_lock(d, timeout=0):
            with self.assertRaises(BookLockedError):
                with book_write_lock(d, timeout=0):
                    pass
        # Released on exit: reacquirable.
        with book_write_lock(d, timeout=0):
            pass

    def test_cross_process_write_lock_blocks_http_writers(self):
        # Simulate the MCP server (a separate process on the same data dir)
        # holding a book's write lock: HTTP write/edit/delete must 409, and the
        # lock must clear when the process dies.
        import subprocess
        import sys
        book_id = self._create_book()["book"]["id"]
        book_dir = os.path.join(self._tmp.name, book_id)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            "import sys, time\n"
            "from bookwriter.locks import book_write_lock\n"
            "with book_write_lock(sys.argv[1], timeout=0):\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(60)\n"
        )
        env = dict(os.environ, PYTHONPATH=repo_root)
        p = subprocess.Popen([sys.executable, "-c", script, book_dir],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env, cwd=repo_root)
        try:
            line = (p.stdout.readline() or "").strip()
            self.assertEqual(line, "locked", f"lock helper failed: {line!r}")
            r = self.client.post(f"/api/books/{book_id}/write", json={})
            self.assertEqual(r.status_code, 409, r.text)
            r = self.client.put(f"/api/books/{book_id}/chapters/1",
                                json={"text": "edited while locked"})
            self.assertEqual(r.status_code, 409, r.text)
            r = self.client.delete(f"/api/books/{book_id}")
            self.assertEqual(r.status_code, 409, r.text)
        finally:
            p.kill()
            p.wait(timeout=10)
        # The OS released the lock with the process: writing works again.
        self._drive_write_to_completion(book_id)

    def test_generate_cover_requires_image_backend(self):
        # With no image backend configured -> a clear 400, not a 500/network call.
        book_id = self._create_book()["book"]["id"]
        self._drive_write_to_completion(book_id)
        saved = {k: os.environ.pop(k, None) for k in ("PIXIO_API_KEY", "OPENAI_API_KEY",
                                                      "BOOKWRITER_IMAGE_URL")}
        os.environ["BOOKWRITER_IMAGE_PROVIDER"] = "pixio"
        try:
            r = self.client.post(f"/api/books/{book_id}/cover/generate", json={})
            self.assertEqual(r.status_code, 400, r.text)
        finally:
            os.environ.pop("BOOKWRITER_IMAGE_PROVIDER", None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
