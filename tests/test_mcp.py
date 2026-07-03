import asyncio
import json
import os
import tempfile
import unittest
from importlib import import_module
from importlib.util import find_spec

_HAS_MCP = bool(find_spec("mcp"))


def _tool_result(res):
    """Normalize FastMCP.call_tool output to the tool's return value.

    Newer mcp versions return (unstructured_content, structured_dict) for tools
    with an output schema; older ones return a sequence of TextContent whose
    text is the JSON-serialized return value. Generic Dict[str, Any] results are
    wrapped as {"result": ...} by FastMCP's output-schema layer — unwrap that."""
    if isinstance(res, tuple):
        out = res[1]
    elif isinstance(res, dict):
        out = res
    else:
        out = json.loads(getattr(res[0], "text", "{}"))
    if isinstance(out, dict) and set(out) == {"result"}:
        out = out["result"]
    return out


def _load_service():
    """Return the service object the MCP tools call.

    The MCP tools (bookwriter.mcp_server) are thin wrappers over a plain-Python
    service obtained from ``get_service()`` exposing create_book / write_book /
    get_manuscript. We exercise that exact object so the smoke test covers the
    same code path the tools do, without needing an MCP transport.

    Falls back to probing for a module that exposes the trio as functions.
    Returns None if nothing suitable is present yet (skip, don't error)."""
    try:
        mod = import_module("bookwriter.mcp_server")
    except Exception:
        mod = None
    if mod is not None and hasattr(mod, "get_service"):
        try:
            svc = mod.get_service()
        except Exception:
            svc = None
        if svc is not None and all(
            callable(getattr(svc, fn, None))
            for fn in ("create_book", "write_book", "get_manuscript")
        ):
            return svc

    candidates = (
        "bookwriter.server.service",
        "bookwriter.mcp_server",
        "bookwriter.service",
    )
    for name in candidates:
        try:
            cand = import_module(name)
        except Exception:
            continue
        if all(callable(getattr(cand, fn, None))
               for fn in ("create_book", "write_book", "get_manuscript")):
            return cand
    return None


@unittest.skipUnless(_HAS_MCP, "mcp not installed")
class TestMCPService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Probe availability without binding a data dir (the real service may
        # cache a singleton on first call); the per-test setUp resolves a fresh
        # service against the tempdir.
        if _load_service() is None:
            raise unittest.SkipTest("MCP service layer not available")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_data = os.environ.get("BOOKWRITER_DATA_DIR")
        self._prev_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["BOOKWRITER_DATA_DIR"] = self._tmp.name
        os.environ.pop("ANTHROPIC_API_KEY", None)
        # Reset any cached service singleton so it rebinds to this tempdir.
        try:
            import bookwriter.mcp_server as _m
            if hasattr(_m, "_service_singleton"):
                _m._service_singleton = None
        except Exception:
            pass
        svc = _load_service()
        if svc is None:
            self.skipTest("MCP service layer not available")
        self.svc = svc

    def tearDown(self):
        if self._prev_data is None:
            os.environ.pop("BOOKWRITER_DATA_DIR", None)
        else:
            os.environ["BOOKWRITER_DATA_DIR"] = self._prev_data
        if self._prev_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_key
        self._tmp.cleanup()

    @staticmethod
    def _book_id(result):
        """Service calls may return a BookSummary-ish dict (possibly nested
        under 'book') or an id string. Extract the id robustly."""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            if "book" in result and isinstance(result["book"], dict):
                return result["book"].get("id")
            return result.get("id")
        return getattr(getattr(result, "book", result), "id", None)

    def test_create_write_get_manuscript(self):
        created = self.svc.create_book(
            premise="A clockmaker discovers time runs backward at midnight.",
            chapters=2,
            words_per_chapter=120,
            title="MCP Smoke",
            genre="fantasy",
            mock=True,
        )
        book_id = self._book_id(created)
        self.assertTrue(book_id, f"no book id from create_book: {created!r}")

        # write_book drives the pipeline to completion synchronously for the
        # service/MCP path (MOCK mode, no network).
        self.svc.write_book(book_id)

        man = self.svc.get_manuscript(book_id)
        markdown = man.get("markdown") if isinstance(man, dict) else str(man)
        words = man.get("words") if isinstance(man, dict) else None
        self.assertTrue(markdown, "empty manuscript")
        if words is not None:
            self.assertGreater(words, 0)

    def test_local_summary_includes_words(self):
        # Directly exercise the local-fallback summary path (_LocalBookService),
        # which must include a `words` field matching the HTTP contract.
        import bookwriter.mcp_server as m
        created = self.svc.create_book(
            premise="A cartographer maps a city that rearranges itself nightly.",
            chapters=2, words_per_chapter=120, title="Words Smoke",
            genre="fantasy", mock=True,
        )
        book_id = self._book_id(created)
        self.svc.write_book(book_id)

        local = m._LocalBookService(self._tmp.name)
        # get_book nests the summary under "book"; list_books returns it flat.
        book = local.get_book(book_id)["book"]
        self.assertIn("words", book)
        self.assertGreater(book["words"], 0)
        listed = {b["id"]: b for b in local.list_books()}
        self.assertIn("words", listed[book_id])
        self.assertEqual(listed[book_id]["words"], book["words"])

    def test_live_ops_without_creds_raise_clean_permissionerror(self):
        # A non-mock book with no provider creds must raise PermissionError (a
        # clean message the tool wrappers catch) instead of a deep SDK auth crash.
        import json as _json
        import bookwriter.mcp_server as m
        created = self.svc.create_book(
            premise="A keeper and the dark water.", chapters=2,
            words_per_chapter=120, title="Creds Guard", mock=True,
        )
        book_id = self._book_id(created)
        self.svc.write_book(book_id)  # writes via mock
        # Flip the book to live (no ANTHROPIC_API_KEY is set in this test env).
        local = m._LocalBookService(self._tmp.name)
        meta_path = local._meta_path(book_id)
        with open(meta_path, encoding="utf-8") as f:
            meta = _json.load(f)
        meta["mock"] = False
        with open(meta_path, "w", encoding="utf-8") as f:
            _json.dump(meta, f)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        for call in (lambda: local.write_book(book_id),
                     lambda: local.revise_chapter(book_id, 1, instructions="x"),
                     lambda: local.append_chapters(book_id, count=1)):
            with self.assertRaises(PermissionError):
                call()

    # ---- build_server() registration + tool-wrapper behavior ------------

    def test_build_server_registers_tools(self):
        import bookwriter.mcp_server as m
        srv = m.build_server()
        tools = {t.name: t for t in asyncio.run(srv.list_tools())}
        expected = {
            "list_profiles", "list_books", "create_book", "write_book",
            "write_chapter", "get_status", "get_chapter", "get_graph",
            "get_cost", "get_manuscript", "prepare_kdp", "export_epub",
            "get_kdp_listing", "export_docx", "print_spec",
            "estimate_royalties", "generate_marketing", "generate_cover",
            "generate_back_cover", "export_pdf", "import_book",
            "edit_chapter", "revise_chapter", "add_chapters",
        }
        missing = expected - set(tools)
        self.assertFalse(missing, f"tools not registered: {sorted(missing)}")
        # HTTP-parity parameters must be exposed in the tool schemas.
        create_props = tools["create_book"].inputSchema.get("properties", {})
        for p in ("guidance", "provider", "model", "chapter_images", "mock"):
            self.assertIn(p, create_props)
        self.assertIn("restart",
                      tools["write_book"].inputSchema.get("properties", {}))
        self.assertIn("profile",
                      tools["import_book"].inputSchema.get("properties", {}))

    def test_tool_error_mapping_returns_error_dict(self):
        # Bogus and traversal-shaped book ids must come back as {"error": ...}
        # (the documented shape), never as an exception escaping the tool.
        import bookwriter.mcp_server as m
        srv = m.build_server()
        cases = [
            ("get_status", {"book_id": "no-such-book"}),
            ("get_chapter", {"book_id": "no-such-book", "number": 1}),
            ("get_graph", {"book_id": "no-such-book"}),
            ("get_cost", {"book_id": "no-such-book"}),
            ("get_manuscript", {"book_id": "no-such-book"}),
            ("get_status", {"book_id": "../evil"}),
            ("write_book", {"book_id": "no-such-book"}),
        ]
        for tool, args in cases:
            out = _tool_result(asyncio.run(srv.call_tool(tool, args)))
            self.assertIsInstance(out, dict, f"{tool} returned {out!r}")
            self.assertIn("error", out, f"{tool}({args}) -> {out!r}")

    def test_validate_book_id_rejects_traversal(self):
        import bookwriter.mcp_server as m
        for bad in ("../evil", "..", "a/b", "a\\b", "C:\\x", "/etc/passwd",
                    "", "UPPER", "a..b", ".hidden", None, 7):
            with self.assertRaises(m.BookNotFound, msg=repr(bad)):
                m._validate_book_id(bad)
        self.assertEqual(m._validate_book_id("my-book-3f2a1b"), "my-book-3f2a1b")

    def test_write_tools_map_busy_and_unexpected_errors(self):
        # The busy-book RuntimeError (MCP equivalent of the HTTP 409) and any
        # unexpected provider/SDK failure must surface as {"error": ...}.
        import bookwriter.mcp_server as m

        class _Busy:
            def write_book(self, book_id, only=None, restart=False):
                raise RuntimeError(
                    f"A write job is already running for {book_id!r}.")

        class _Boom:
            def write_book(self, book_id, only=None, restart=False):
                raise ConnectionError("socket closed mid-write")

        srv = m.build_server()
        prev = m._service_singleton
        try:
            m._service_singleton = _Busy()
            out = _tool_result(asyncio.run(
                srv.call_tool("write_book", {"book_id": "busy-book-abc123"})))
            self.assertIn("already running", out.get("error", ""))
            out = _tool_result(asyncio.run(srv.call_tool(
                "write_chapter", {"book_id": "busy-book-abc123", "number": 1})))
            self.assertIn("already running", out.get("error", ""))

            m._service_singleton = _Boom()
            out = _tool_result(asyncio.run(
                srv.call_tool("write_book", {"book_id": "boom-book-abc123"})))
            self.assertIn("write failed", out.get("error", ""))
        finally:
            m._service_singleton = prev

    # ---- behavioral parity fixes ----------------------------------------

    def test_export_epub_builds_on_demand(self):
        # export_epub must build the EPUB when prepare_kdp hasn't run (parity
        # with GET /api/books/{id}/export/epub) — on both service paths.
        import bookwriter.mcp_server as m
        created = self.svc.create_book(
            premise="A librarian inherits a door that opens on yesterday.",
            chapters=2, words_per_chapter=120, title="Epub Smoke", mock=True,
        )
        book_id = self._book_id(created)
        self.svc.write_book(book_id)

        path = self.svc.epub_path(book_id)
        self.assertTrue(path.endswith("manuscript.epub"), path)
        self.assertTrue(os.path.isfile(path))
        self.assertGreater(os.path.getsize(path), 0)

        # Local-fallback implementation must build on demand too.
        os.remove(path)
        local = m._LocalBookService(self._tmp.name)
        path2 = local.epub_path(book_id)
        self.assertTrue(os.path.isfile(path2))
        self.assertGreater(os.path.getsize(path2), 0)

    def test_create_book_threads_guidance_provider_model_images(self):
        # New create_book parameters must be persisted to meta.json exactly as
        # the HTTP CreateBookRequest would persist them.
        import bookwriter.mcp_server as m
        created = self.svc.create_book(
            premise="A tide-powered orchestra wakes a drowned city.",
            chapters=2, words_per_chapter=120, title="Param Smoke",
            genre="fantasy", guidance="Keep the tone wistful.",
            provider="anthropic", model="claude-test-model",
            chapter_images=True, mock=True,
        )
        book_id = self._book_id(created)
        self.assertTrue(book_id, f"no book id: {created!r}")
        local = m._LocalBookService(self._tmp.name)
        with open(local._meta_path(book_id), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta.get("provider"), "anthropic")
        self.assertEqual(meta.get("model"), "claude-test-model")
        self.assertTrue(meta.get("chapter_images"))

        # And the local-fallback service must accept/persist the same kwargs.
        created2 = local.create_book(
            premise="A cartographer of dreams loses her own.",
            chapters=2, words_per_chapter=120, title="Param Smoke Local",
            guidance="Sparse prose.", provider="openai", model="gpt-test",
            chapter_images=True, mock=True,
        )
        with open(local._meta_path(created2["id"]), encoding="utf-8") as f:
            meta2 = json.load(f)
        self.assertEqual(meta2.get("provider"), "openai")
        self.assertEqual(meta2.get("model"), "gpt-test")
        self.assertTrue(meta2.get("chapter_images"))

    def test_write_book_restart_rewrites(self):
        created = self.svc.create_book(
            premise="A ferryman charges memories instead of coins.",
            chapters=2, words_per_chapter=120, title="Restart Smoke", mock=True,
        )
        book_id = self._book_id(created)
        self.svc.write_book(book_id)
        # A restart run must be accepted and leave the book fully written.
        result = self.svc.write_book(book_id, restart=True)
        self.assertEqual(result["chapters_written"], result["chapters_total"])
        self.assertEqual(result["chapters_total"], 2)

    def test_import_book_degrades_to_structure_only_without_creds(self):
        # analyze=True with no provider credentials must fall back to a
        # structure-only import (parity with the HTTP service), not crash with
        # a raw SDK auth error.
        import bookwriter.mcp_server as m
        from bookwriter.provider import live_available
        if live_available():
            self.skipTest("live provider credentials present in this env")
        local = m._LocalBookService(self._tmp.name)
        text = (
            "# Chapter 1\n\nThe keeper counted the waves until the lamp "
            "failed.\n\n# Chapter 2\n\nAt dawn she rowed toward the wreck.\n"
        )
        res = local.import_book(text=text, title="Imported Smoke",
                                analyze=True, mock=False)
        self.assertTrue(res.get("id"))
        self.assertEqual(res.get("chapters_total"), 2)
        self.assertEqual(res.get("chapters_written"), 2)
        self.assertIn("bible", res)

    def test_prepare_kdp_reuses_cached_marketing(self):
        # generate_marketing caches kdp/marketing.json; a later prepare_kdp must
        # ship it in the kit (the docstring's promised reuse).
        import bookwriter.mcp_server as m
        created = self.svc.create_book(
            premise="A monastery brews ink that writes the future.",
            chapters=2, words_per_chapter=120, title="Marketing Smoke",
            mock=True,
        )
        book_id = self._book_id(created)
        self.svc.write_book(book_id)
        local = m._LocalBookService(self._tmp.name)
        marketing = local.generate_marketing(book_id)
        self.assertIsInstance(marketing, dict)
        kit = local.prepare_kdp(book_id, author_first="Ada", author_last="Lore")
        self.assertIn("marketing", kit["paths"])
        with open(kit["paths"]["marketing"], encoding="utf-8") as f:
            self.assertEqual(json.load(f), marketing)

    def test_prepare_kdp_reuses_http_cached_marketing(self):
        # Older HTTP builds cached their snapshot at <book>/marketing.json (the
        # service now writes kdp/marketing.json); a later MCP prepare_kdp must
        # still reuse that legacy copy (shared-state back-compat).
        import bookwriter.mcp_server as m
        created = self.svc.create_book(
            premise="A lighthouse keeper trades voices with the fog.",
            chapters=2, words_per_chapter=120, title="HTTP Marketing Smoke",
            mock=True,
        )
        book_id = self._book_id(created)
        self.svc.write_book(book_id)
        local = m._LocalBookService(self._tmp.name)
        marketing = {"blurb_variants": ["From the HTTP cache."],
                     "a_plus_modules": [], "author_bio": "Bio.",
                     "taglines": ["One line."]}
        with open(os.path.join(local._book_dir(book_id), "marketing.json"),
                  "w", encoding="utf-8") as f:
            json.dump(marketing, f)
        kit = local.prepare_kdp(book_id, author_first="Ada", author_last="Lore")
        self.assertIn("marketing", kit["paths"])
        with open(kit["paths"]["marketing"], encoding="utf-8") as f:
            self.assertEqual(json.load(f), marketing)


if __name__ == "__main__":
    unittest.main()
