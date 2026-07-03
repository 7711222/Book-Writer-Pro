import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from bookwriter.cli import load_env_file, main

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@contextlib.contextmanager
def _quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _generate(tmp, chapters=2, words=120):
    """Run a full mock generate into *tmp* (fast, offline)."""
    with _quiet():
        rc = main(["generate", "--premise", "a test", "--chapters", str(chapters),
                   "--words", str(words), "--project", tmp, "--mock"])
    assert rc == 0, "mock generate failed"


def _plan_only(tmp, chapters=2):
    with _quiet():
        rc = main(["plan", "--premise", "a test", "--chapters", str(chapters),
                   "--project", tmp, "--mock"])
    assert rc == 0, "mock plan failed"


class TestCLI(unittest.TestCase):
    def test_profiles(self):
        with _quiet() as buf:
            rc = main(["profiles"])
        self.assertEqual(rc, 0)
        self.assertIn("balanced", buf.getvalue())

    def test_generate_mock_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet():
                rc = main(["generate", "--premise", "a test", "--chapters", "2",
                           "--words", "120", "--project", tmp, "--mock"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "manuscript.md")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "cost.json")))

    def test_plan_then_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet():
                self.assertEqual(main(["plan", "--premise", "x", "--chapters", "2",
                                       "--project", tmp, "--mock"]), 0)
                rc = main(["report", "--project", tmp])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "book.json")))


class TestEnvFile(unittest.TestCase):
    """The stdlib .env loader (load_env_file) and its wiring into main()."""

    def test_parses_comments_blanks_quotes_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    "# a comment\n"
                    "\n"
                    "PLAIN=value\n"
                    "export EXPORTED=yes\n"
                    'DOUBLE="two words"\n'
                    "SINGLE='keep # this'\n"
                    'QUOTED_COMMENT="v"  # note\n'
                    "TRAILING=bare   # inline comment\n"
                    "SPACED = padded \n"
                    "not a kv line\n"
                    "1BADKEY=skipped\n"
                )
            with mock.patch.dict(os.environ):
                for k in ("PLAIN", "EXPORTED", "DOUBLE", "SINGLE",
                          "QUOTED_COMMENT", "TRAILING", "SPACED", "1BADKEY"):
                    os.environ.pop(k, None)
                n = load_env_file(path)
                self.assertEqual(n, 7)
                self.assertEqual(os.environ["PLAIN"], "value")
                self.assertEqual(os.environ["EXPORTED"], "yes")
                self.assertEqual(os.environ["DOUBLE"], "two words")
                self.assertEqual(os.environ["SINGLE"], "keep # this")
                self.assertEqual(os.environ["QUOTED_COMMENT"], "v")
                self.assertEqual(os.environ["TRAILING"], "bare")
                self.assertEqual(os.environ["SPACED"], "padded")
                self.assertNotIn("1BADKEY", os.environ)

    def test_utf8_bom_does_not_hide_the_first_key(self):
        # Notepad/PowerShell on Windows often write a UTF-8 BOM; the first
        # key must still load instead of being silently skipped.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("BW_TEST_BOM=first\n")
            with mock.patch.dict(os.environ):
                os.environ.pop("BW_TEST_BOM", None)
                self.assertEqual(load_env_file(path), 1)
                self.assertEqual(os.environ["BW_TEST_BOM"], "first")

    def test_never_overrides_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("BW_TEST_PRESET=from-dotenv\n")
            with mock.patch.dict(os.environ, {"BW_TEST_PRESET": "from-shell"}):
                self.assertEqual(load_env_file(path), 0)
                self.assertEqual(os.environ["BW_TEST_PRESET"], "from-shell")

    def test_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_env_file(os.path.join(tmp, "no-such.env")), 0)

    def test_bookwriter_env_file_selects_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "custom.env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("BW_TEST_CUSTOM=picked-up\n")
            with mock.patch.dict(os.environ, {"BOOKWRITER_ENV_FILE": path}):
                os.environ.pop("BW_TEST_CUSTOM", None)
                self.assertEqual(load_env_file(), 1)
                self.assertEqual(os.environ["BW_TEST_CUSTOM"], "picked-up")

    def test_env_example_verbatim_copy_is_inert(self):
        # Every value in .env.example must be commented out: copying it as-is
        # must set nothing (especially no placeholder API keys).
        example = os.path.join(_REPO_ROOT, ".env.example")
        self.assertTrue(os.path.isfile(example))
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(load_env_file(example), 0)
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
            self.assertNotIn("PIXIO_API_KEY", os.environ)

    def test_main_loads_dotenv_from_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                f.write("BW_TEST_SENTINEL=loaded\n")
            cwd = os.getcwd()
            with mock.patch.dict(os.environ):
                os.environ.pop("BW_TEST_SENTINEL", None)
                os.environ.pop("BOOKWRITER_ENV_FILE", None)
                os.chdir(tmp)
                try:
                    with _quiet():
                        rc = main(["profiles"])
                finally:
                    os.chdir(cwd)
                self.assertEqual(rc, 0)
                self.assertEqual(os.environ.get("BW_TEST_SENTINEL"), "loaded")


class TestKdpAndPrice(unittest.TestCase):
    def test_kdp_kit_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            _generate(tmp)
            with _quiet() as buf:
                rc = main(["kdp", "--project", tmp, "--author-first", "A",
                           "--author-last", "B", "--mock"])
            self.assertEqual(rc, 0)
            self.assertIn("KDP kit written", buf.getvalue())
            self.assertTrue(os.path.exists(os.path.join(tmp, "kdp", "manuscript.epub")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "kdp", "metadata.json")))

    def test_price_reports_ebook_and_paperback(self):
        with tempfile.TemporaryDirectory() as tmp:
            _generate(tmp)
            with _quiet() as buf:
                rc = main(["price", "--project", tmp, "--list-price", "4.99"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("EBOOK", out)
            self.assertIn("PAPERBACK", out)

    def test_price_without_plan_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet(), self.assertRaises(SystemExit) as cm:
                main(["price", "--project", tmp, "--list-price", "4.99"])
            self.assertIn("no plan", str(cm.exception.code))


class TestImport(unittest.TestCase):
    _MS = ("# My Imported Book\n\n"
           "## Chapter 1: The Door\n\n"
           "It began, as these things do, with a knock nobody heard.\n\n"
           "## Chapter 2: The Key\n\n"
           "By morning the house had made up its mind about them.\n")

    def test_import_manuscript_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = os.path.join(tmp, "ms.md")
            with open(ms, "w", encoding="utf-8") as f:
                f.write(self._MS)
            proj = os.path.join(tmp, "book")
            with _quiet() as buf:
                rc = main(["import", "--file", ms, "--project", proj, "--mock"])
            self.assertEqual(rc, 0)
            self.assertIn("Imported 2 chapter(s)", buf.getvalue())
            self.assertTrue(os.path.exists(os.path.join(proj, "book.json")))
            self.assertTrue(os.path.exists(os.path.join(proj, "chapters", "01.md")))
            self.assertTrue(os.path.exists(os.path.join(proj, "manuscript.md")))

    def test_import_missing_file_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet(), self.assertRaises(SystemExit) as cm:
                main(["import", "--file", os.path.join(tmp, "nope.md"),
                      "--project", tmp, "--mock"])
            self.assertIn("cannot read", str(cm.exception.code))

    def test_import_empty_file_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = os.path.join(tmp, "empty.md")
            with open(ms, "w", encoding="utf-8") as f:
                f.write("   \n\n")
            with _quiet(), self.assertRaises(SystemExit) as cm:
                main(["import", "--file", ms, "--project", tmp, "--mock"])
            self.assertIn("empty", str(cm.exception.code))


class TestReviseContinueWrite(unittest.TestCase):
    def test_revise_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            _generate(tmp)
            with _quiet() as buf:
                rc = main(["revise", "--project", tmp, "--chapter", "1", "--mock"])
            self.assertEqual(rc, 0)
            self.assertIn("Revised chapter 1", buf.getvalue())
            self.assertTrue(os.path.exists(os.path.join(tmp, "chapters", "01.md")))

    def test_revise_unwritten_chapter_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            _plan_only(tmp)  # planned but nothing written
            with _quiet(), self.assertRaises(SystemExit) as cm:
                main(["revise", "--project", tmp, "--chapter", "1", "--mock"])
            self.assertIn("hasn't been written", str(cm.exception.code))

    def test_continue_extends_outline(self):
        with tempfile.TemporaryDirectory() as tmp:
            _generate(tmp, chapters=2)
            book_json = os.path.join(tmp, "book.json")
            with open(book_json, encoding="utf-8") as f:
                before = len(json.load(f)["outline"])
            with _quiet():
                rc = main(["continue", "--project", tmp, "--count", "2", "--mock"])
            self.assertEqual(rc, 0)
            with open(book_json, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["outline"]), before + 2)
            self.assertEqual(data["target_chapters"], before + 2)

    def test_write_without_plan_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet(), self.assertRaises(SystemExit) as cm:
                main(["write", "--project", tmp, "--mock"])
            self.assertIn("no plan", str(cm.exception.code))

    def test_write_restart_rewrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            _plan_only(tmp)
            with _quiet():
                rc = main(["write", "--project", tmp, "--mock", "--restart"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "manuscript.md")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "chapters", "02.md")))


class TestPdfAndCover(unittest.TestCase):
    def test_pdf_without_plan_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet(), self.assertRaises(SystemExit) as cm:
                main(["pdf", "--project", tmp])
            self.assertIn("no plan", str(cm.exception.code))

    def test_pdf_without_chapters_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            _plan_only(tmp)
            with mock.patch("bookwriter.pdf.pdf_available", return_value=True):
                with _quiet(), self.assertRaises(SystemExit) as cm:
                    main(["pdf", "--project", tmp, "--part", "interior"])
            self.assertIn("no chapters", str(cm.exception.code))

    def test_pdf_unavailable_shows_install_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            _generate(tmp)
            with mock.patch("bookwriter.pdf.pdf_available", return_value=False):
                with _quiet(), self.assertRaises(SystemExit) as cm:
                    main(["pdf", "--project", tmp])
            self.assertIn("reportlab", str(cm.exception.code))

    def test_cover_without_image_backend_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            _generate(tmp)
            from bookwriter import runtime_config as rc_mod
            with mock.patch.dict(os.environ), \
                 mock.patch.dict(rc_mod._overrides, clear=True):
                for k in ("PIXIO_API_KEY", "OPENAI_API_KEY",
                          "BOOKWRITER_IMAGE_URL", "BOOKWRITER_IMAGE_PROVIDER"):
                    os.environ.pop(k, None)
                # keep main()'s .env loading from resurrecting keys from a real ./.env
                os.environ["BOOKWRITER_ENV_FILE"] = os.path.join(tmp, "no.env")
                with _quiet(), self.assertRaises(SystemExit) as cm:
                    main(["cover", "--project", tmp])
            self.assertIn("PIXIO_API_KEY", str(cm.exception.code))


class TestErrorHandling(unittest.TestCase):
    def test_unexpected_error_exits_1(self):
        # A missing --premise-file raises OSError inside the command, which
        # main()'s generic handler turns into a tidy rc=1 on stderr.
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet() as buf:
                rc = main(["plan", "--premise-file", os.path.join(tmp, "nope.txt"),
                           "--project", tmp, "--mock"])
            self.assertEqual(rc, 1)
            self.assertIn("error:", buf.getvalue())

    def test_invalid_only_selection_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            _plan_only(tmp)
            with _quiet() as buf:
                rc = main(["write", "--project", tmp, "--mock", "--only", "abc"])
            self.assertEqual(rc, 1)
            self.assertIn("invalid --only", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
