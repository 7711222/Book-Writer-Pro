"""On-disk persistence — the 'committed JSON' that survives across runs.

Layout under ``project_dir``:

    book.json            the Bible (stable spine, updated as the graph grows)
    state.json           evolving graph state (timeline, rolling synopsis)
    chapters/NN.json     ChapterRecord (text + fingerprint + synopsis line)
    chapters/NN.md       human-readable chapter prose
    manuscript.md        assembled full book
    cost.json            last run's cost ledger snapshot

Saving after every chapter makes generation resumable: a crashed or interrupted
run reloads the graph and continues from the next unwritten chapter.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Optional

from .graph import StoryGraph
from .models import Bible, ChapterRecord


class BookStore:
    def __init__(self, project_dir: str):
        self.dir = project_dir
        self.chapters_dir = os.path.join(project_dir, "chapters")

    # ---- paths --------------------------------------------------------
    @property
    def book_path(self):
        return os.path.join(self.dir, "book.json")

    @property
    def state_path(self):
        return os.path.join(self.dir, "state.json")

    def chapter_json(self, n: int):
        return os.path.join(self.chapters_dir, f"{n:02d}.json")

    def chapter_md(self, n: int):
        return os.path.join(self.chapters_dir, f"{n:02d}.md")

    @property
    def images_dir(self):
        return os.path.join(self.dir, "images")

    def image_path(self, n: int, ext: str = "png"):
        return os.path.join(self.images_dir, f"{n:02d}.{ext}")

    # ---- ensure dirs --------------------------------------------------
    def ensure(self):
        os.makedirs(self.chapters_dir, exist_ok=True)

    # ---- chapter images ----------------------------------------------
    _IMG_EXTS = ("png", "jpg", "jpeg", "webp", "gif")

    def save_image(self, n: int, data: bytes, ext: str = "png") -> str:
        os.makedirs(self.images_dir, exist_ok=True)
        ext = (ext or "png").lower().lstrip(".")
        if ext not in self._IMG_EXTS:
            ext = "png"
        # Remove any stale image for this chapter in another format first.
        for e in self._IMG_EXTS:
            p = self.image_path(n, e)
            if e != ext and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        path = self.image_path(n, ext)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def find_image(self, n: int) -> Optional[str]:
        for e in self._IMG_EXTS:
            p = self.image_path(n, e)
            if os.path.exists(p):
                return p
        return None

    def has_image(self, n: int) -> bool:
        return self.find_image(n) is not None

    def collect_images(self, numbers) -> dict:
        """{chapter_number: (bytes, ext)} for every chapter that has an image —
        the shape build_epub() / build_kdp_kit() accept for embedding."""
        out = {}
        for n in numbers:
            p = self.find_image(n)
            if p:
                with open(p, "rb") as f:
                    out[n] = (f.read(), p.rsplit(".", 1)[-1].lower())
        return out

    # ---- bible + state ------------------------------------------------
    def save_bible(self, bible: Bible):
        self.ensure()
        _write_json(self.book_path, bible.to_dict())

    def load_bible(self) -> Optional[Bible]:
        if not os.path.exists(self.book_path):
            return None
        return Bible.from_dict(_read_json(self.book_path))

    def save_graph(self, graph: StoryGraph):
        self.save_bible(graph.bible)
        _write_json(self.state_path, graph.state_to_dict())

    def load_graph(self) -> Optional[StoryGraph]:
        bible = self.load_bible()
        if bible is None:
            return None
        graph = StoryGraph(bible)
        if os.path.exists(self.state_path):
            graph.load_state(_read_json(self.state_path))
        # reload chapter records
        for p in bible.outline:
            cj = self.chapter_json(p.number)
            if os.path.exists(cj):
                graph.chapters[p.number] = ChapterRecord.from_dict(_read_json(cj))
        return graph

    # ---- chapters -----------------------------------------------------
    def save_chapter(self, rec: ChapterRecord):
        self.ensure()
        _write_json(self.chapter_json(rec.number), rec.to_dict())
        _write_text(
            self.chapter_md(rec.number),
            f"# Chapter {rec.number}: {rec.title}\n\n{rec.text}\n",
        )

    def has_chapter(self, n: int) -> bool:
        return os.path.exists(self.chapter_json(n))

    def clear_chapters(self) -> None:
        """Delete every generated chapter artifact: chapters/*.json + *.md,
        chapter images, manuscript.md and the cost snapshot. Called when a
        project is RE-planned, so load_graph/resume can never stitch a previous
        book's prose into the new outline (book.json/state.json themselves are
        overwritten by the subsequent save_graph)."""
        if os.path.isdir(self.chapters_dir):
            for name in os.listdir(self.chapters_dir):
                if name.endswith((".json", ".md")):
                    _unlink_quiet(os.path.join(self.chapters_dir, name))
        if os.path.isdir(self.images_dir):
            for name in os.listdir(self.images_dir):
                stem, _, ext = name.rpartition(".")
                if ext.lower() in self._IMG_EXTS and stem.isdigit():
                    _unlink_quiet(os.path.join(self.images_dir, name))
        for name in ("manuscript.md", "cost.json", "cost.txt"):
            _unlink_quiet(os.path.join(self.dir, name))

    # ---- assembly -----------------------------------------------------
    def assemble_manuscript(self, graph: StoryGraph) -> str:
        b = graph.bible
        out = [f"# {b.title}\n"]
        if b.logline:
            out.append(f"*{b.logline}*\n")
        for p in b.outline:
            rec = graph.chapters.get(p.number)
            if not rec:
                continue
            out.append(f"\n## Chapter {p.number}: {rec.title}\n")
            out.append(rec.text)
        text = "\n".join(out)
        _write_text(os.path.join(self.dir, "manuscript.md"), text)
        return text

    def save_cost(self, report: str, data: dict):
        _write_json(os.path.join(self.dir, "cost.json"), data)
        _write_text(os.path.join(self.dir, "cost.txt"), report)


def _unlink_quiet(path: str) -> None:
    """Best-effort delete (missing file / transient Windows lock is fine)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _atomic_replace(tmp: str, path: str) -> None:
    """os.replace(tmp, path) with a short retry for Windows.

    On Windows, replacing a file that a reader currently has open can raise
    PermissionError (WinError 5/32). Readers here open-read-close quickly, so a
    brief backoff almost always wins the race. The swap remains atomic: readers
    see the old or the new complete file, never a partial one.
    """
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.005)


def _atomic_write(path: str, write_fn) -> None:
    # Serialize to a temp file in the same directory, then atomically replace
    # the target. Concurrent readers (e.g. the HTTP API reading
    # book.json/state.json while the background write job runs) therefore never
    # observe a truncated/partial file.
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write_fn(f)
        _atomic_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: str, data) -> None:
    _atomic_write(
        path,
        lambda f: json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True),
    )


def _write_text(path: str, text: str) -> None:
    """Atomic text write (same swap strategy as _write_json)."""
    _atomic_write(path, lambda f: f.write(text))


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
