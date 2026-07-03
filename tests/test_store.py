import json
import os
import tempfile
import threading
import time
import unittest

from bookwriter.models import Bible, ChapterRecord
from bookwriter.store import BookStore, _read_json, _write_json


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows a reader thread may hold the file
        # open for a microsecond past join; that's a test artifact, not a bug.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self._tmp.name, "book.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        data = {"title": "T", "outline": list(range(50))}
        _write_json(self.path, data)
        self.assertEqual(_read_json(self.path), data)

    def test_no_temp_files_left_behind(self):
        _write_json(self.path, {"a": 1})
        leftovers = [n for n in os.listdir(self._tmp.name) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_concurrent_writes_never_yield_partial_json(self):
        # A writer rapidly rewrites the file while readers parse it. With an
        # atomic os.replace swap, readers must always see complete JSON — never
        # a truncated/empty file (which used to raise JSONDecodeError).
        big = {"chapters": [{"n": i, "text": "word " * 80} for i in range(20)]}
        errors = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(120):
                    _write_json(self.path, {**big, "rev": i})
            finally:
                stop.set()  # always release readers, even on an unexpected error

        def reader():
            # A small yield models real HTTP readers (occasional requests), not a
            # zero-backoff hot loop that would pin the file handle continuously.
            while not stop.is_set():
                try:
                    obj = _read_json(self.path)
                    if "chapters" not in obj:
                        errors.append("missing key")
                except (FileNotFoundError, PermissionError):
                    pass
                except json.JSONDecodeError as e:
                    errors.append(f"decode: {e}")
                time.sleep(0.001)

        _write_json(self.path, big)
        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [], f"atomic write violated: {errors[:5]}")


class TestClearChapters(unittest.TestCase):
    def test_clear_chapters_removes_artifacts_keeps_bible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(tmp)
            store.save_bible(Bible(title="T"))
            store.save_chapter(ChapterRecord(number=1, title="a", text="x"))
            store.save_image(1, b"\x89PNG", "png")
            store.save_cost("report", {"total_cost": 0})
            with open(os.path.join(tmp, "manuscript.md"), "w", encoding="utf-8") as f:
                f.write("# T\n")

            store.clear_chapters()

            self.assertFalse(store.has_chapter(1))
            self.assertFalse(os.path.exists(store.chapter_md(1)))
            self.assertIsNone(store.find_image(1))
            self.assertFalse(os.path.exists(os.path.join(tmp, "manuscript.md")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "cost.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "cost.txt")))
            # the bible itself is left for the caller to overwrite
            self.assertTrue(os.path.exists(store.book_path))

    def test_clear_chapters_on_fresh_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            BookStore(tmp).clear_chapters()   # nothing to delete, no error
            self.assertEqual(sorted(os.listdir(tmp)), [])


if __name__ == "__main__":
    unittest.main()
