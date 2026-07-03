import base64
import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from xml.dom import minidom

from bookwriter.config import Settings
from bookwriter.costs import CostLedger
from bookwriter.mock import MockLLM
from bookwriter.pipeline import BookPipeline
from bookwriter.kdp import (
    KDP_SCHEMA, KdpMetadata, generate_kdp_metadata, build_epub, build_kdp_kit,
    MAX_DESCRIPTION_CHARS, MAX_KEYWORDS, MAX_KEYWORD_CHARS, MAX_CATEGORIES,
    STORYTELLER_KEYWORD, _lang_code,
)

# A valid 1x1 PNG (for cover-art embedding tests; no Pillow dependency).
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _build_book(tmp, *, premise="a detective hunts a small town killer",
                chapters=3, genre=None):
    """Run the offline pipeline to get a real StoryGraph with chapters."""
    settings = Settings(project_dir=tmp).with_profile("balanced")
    pipe = BookPipeline(MockLLM(), settings)
    pipe.plan(premise=premise, chapters=chapters, words_per_chapter=150)
    if genre:
        pipe.graph.bible.genre = genre
    pipe.write_all()
    return pipe.graph, settings


class TestKdpSchema(unittest.TestCase):
    def test_schema_is_strict_and_has_required(self):
        self.assertEqual(KDP_SCHEMA["additionalProperties"], False)
        for key in ("description", "keywords", "categories", "subtitle",
                    "reading_age_min", "reading_age_max", "series_suggestion"):
            self.assertIn(key, KDP_SCHEMA["properties"])
            self.assertIn(key, KDP_SCHEMA["required"])


class TestGenerateMetadata(unittest.TestCase):
    def test_limits_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp, genre="mystery")
            meta = generate_kdp_metadata(
                MockLLM(), settings, CostLedger(), graph,
                author_first="Jane", author_last="Doe",
            )
            self.assertIsInstance(meta, KdpMetadata)
            # keywords
            self.assertLessEqual(len(meta.keywords), MAX_KEYWORDS)
            self.assertTrue(meta.keywords)
            for k in meta.keywords:
                self.assertLessEqual(len(k), MAX_KEYWORD_CHARS)
            # no dupes
            self.assertEqual(len(meta.keywords), len(set(k.lower() for k in meta.keywords)))
            # categories
            self.assertLessEqual(len(meta.categories), MAX_CATEGORIES)
            self.assertTrue(meta.categories)
            # description
            self.assertLessEqual(len(meta.description), MAX_DESCRIPTION_CHARS)
            self.assertTrue(meta.description)
            # adult -> blank reading age
            self.assertEqual(meta.reading_age_min, "")
            self.assertEqual(meta.reading_age_max, "")
            # author preserved verbatim (not generated)
            self.assertEqual(meta.author_first, "Jane")
            self.assertEqual(meta.author_last, "Doe")
            self.assertEqual(meta.publishing_rights, "owned")
            self.assertEqual(meta.primary_marketplace, "Amazon.com")

    def test_description_truncated_to_4000(self):
        class FatLLM(MockLLM):
            def _kdp(self, user):
                d = super()._kdp(user)
                d["description"] = "word " * 2000  # ~10000 chars
                d["keywords"] = ["x" * 80] + ["only one"]  # over-length + valid
                d["categories"] = [f"Cat {i}" for i in range(10)]
                return d

        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp)
            meta = generate_kdp_metadata(
                FatLLM(), settings, CostLedger(), graph,
                author_first="A", author_last="B",
            )
            self.assertLessEqual(len(meta.description), MAX_DESCRIPTION_CHARS)
            self.assertLessEqual(len(meta.categories), MAX_CATEGORIES)
            for k in meta.keywords:
                self.assertLessEqual(len(k), MAX_KEYWORD_CHARS)

    def test_keywords_drop_title_and_author(self):
        class LeakLLM(MockLLM):
            def _kdp(self, user):
                d = super()._kdp(user)
                d["keywords"] = ["the threshold story", "wren calloway thriller",
                                 "clean search phrase"]
                return d

        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp)
            graph.bible.title = "The Threshold"
            meta = generate_kdp_metadata(
                LeakLLM(), settings, CostLedger(), graph,
                author_first="Wren", author_last="Calloway",
            )
            low = [k.lower() for k in meta.keywords]
            self.assertNotIn("the threshold story", low)
            self.assertNotIn("wren calloway thriller", low)
            self.assertIn("clean search phrase", low)

    def test_title_subtitle_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp)
            graph.bible.title = "The Long Road : A Journey Home"
            meta = generate_kdp_metadata(
                MockLLM(), settings, CostLedger(), graph,
                author_first="A", author_last="B",
            )
            self.assertEqual(meta.title, "The Long Road")
            self.assertEqual(meta.subtitle, "A Journey Home")
            self.assertEqual(meta.full_title(), "The Long Road: A Journey Home")

    def test_metadata_roundtrip(self):
        meta = KdpMetadata(
            title="T", author_first="A", author_last="B",
            subtitle="Sub", keywords=["k1", "k2"], categories=["c1"],
            contributors=[{"first": "C", "last": "D"}],
        )
        d = meta.to_dict()
        again = KdpMetadata.from_dict(d)
        self.assertEqual(again.to_dict(), d)
        self.assertEqual(again.full_title(), "T: Sub")
        self.assertEqual(again.all_creators(), ["A B", "C D"])


class TestEpub(unittest.TestCase):
    def _meta(self, graph):
        return KdpMetadata(
            title=graph.bible.title or "Test Book",
            subtitle="A Subtitle",
            author_first="Jane", author_last="Doe",
            contributors=[{"first": "John", "last": "Smith"}],
            description="A blurb.",
            keywords=["k1"], categories=["c1"],
        )

    def test_epub_is_valid_zip_with_stored_mimetype(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            data = build_epub(graph, self._meta(graph))
            self.assertIsInstance(data, bytes)
            zf = zipfile.ZipFile(io.BytesIO(data))
            self.assertIsNone(zf.testzip())
            names = zf.namelist()
            # mimetype must be the FIRST entry, STORED, exact content
            self.assertEqual(names[0], "mimetype")
            info = zf.getinfo("mimetype")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertEqual(zf.read("mimetype"), b"application/epub+zip")
            # required structural files present
            for required in ("META-INF/container.xml", "OEBPS/content.opf",
                             "OEBPS/nav.xhtml", "OEBPS/toc.ncx",
                             "OEBPS/title.xhtml", "OEBPS/cover.svg",
                             "OEBPS/cover.xhtml"):
                self.assertIn(required, names)
            # one xhtml per chapter
            chap_files = [n for n in names if n.startswith("OEBPS/chap-")]
            self.assertEqual(len(chap_files), len(graph.chapters))

    def test_opf_has_required_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            meta = self._meta(graph)
            zf = zipfile.ZipFile(io.BytesIO(build_epub(graph, meta)))
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn("<dc:title>", opf)
            self.assertIn(meta.full_title(), opf)
            self.assertIn("<dc:creator", opf)
            self.assertIn("Jane Doe", opf)
            self.assertIn("John Smith", opf)        # contributor
            self.assertIn("<dc:language>", opf)
            self.assertIn("<dc:identifier", opf)
            self.assertIn("urn:uuid:", opf)
            self.assertIn("cover-image", opf)

    def test_all_xml_well_formed(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            zf = zipfile.ZipFile(io.BytesIO(build_epub(graph, self._meta(graph))))
            for name in zf.namelist():
                if name == "mimetype" or name.endswith(".css"):
                    continue
                # raises on malformed XML
                minidom.parseString(zf.read(name))

    def test_chapter_text_never_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp, chapters=4)
            zf = zipfile.ZipFile(io.BytesIO(build_epub(graph, self._meta(graph))))
            for n in sorted(graph.chapters):
                rec = graph.chapters[n]
                # gather all chapter xhtml text content
                xhtml = "".join(
                    zf.read(name).decode("utf-8")
                    for name in zf.namelist() if name.startswith("OEBPS/chap-")
                )
                # every non-trivial word of the prose must survive escaping/split
                src_words = rec.text.split()
                doc_text = "".join(
                    minidom.parseString(zf.read(name)).documentElement.toxml()
                    for name in zf.namelist() if name.startswith("OEBPS/chap-")
                )
                for w in src_words:
                    if w.isalpha() and len(w) > 3:
                        self.assertIn(w, doc_text,
                                      f"chapter {n} lost word {w!r}")

    def test_custom_cover_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            svg = ('<?xml version="1.0" encoding="UTF-8"?>'
                   '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                   '<rect width="10" height="10" fill="red"/></svg>')
            zf = zipfile.ZipFile(io.BytesIO(build_epub(graph, self._meta(graph),
                                                       cover_svg=svg)))
            self.assertEqual(zf.read("OEBPS/cover.svg").decode("utf-8"), svg)

    def test_control_chars_do_not_corrupt_epub(self):
        # \x0b (Word soft line break) and friends are XML-1.0-illegal; pasted
        # prose must not produce an EPUB that Kindle/epubcheck reject.
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            first = sorted(graph.chapters)[0]
            graph.chapters[first].text = (
                "Alpha\x0bbeta continues the line.\x0c\x01\x1f"
                + chr(0xFFFE) + " Clean tail."   # U+FFFE noncharacter
            )
            meta = self._meta(graph)
            meta.subtitle = "A\x0bSubtitle"
            zf = zipfile.ZipFile(io.BytesIO(build_epub(graph, meta)))
            for name in zf.namelist():
                if name == "mimetype" or name.endswith(".css"):
                    continue
                raw = zf.read(name)
                minidom.parseString(raw)  # raises on malformed XML
                self.assertNotIn(b"\x0b", raw, name)
            chap = zf.read(f"OEBPS/chap-{1:02d}.xhtml").decode("utf-8")
            # \x0b became a space: both halves survive, unglued
            self.assertIn("Alpha beta", chap)
            self.assertIn("Clean tail.", chap)

    def test_language_maps_to_bcp47(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            meta = self._meta(graph)
            meta.language = "Spanish"
            zf = zipfile.ZipFile(io.BytesIO(build_epub(graph, meta)))
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn("<dc:language>es</dc:language>", opf)
            self.assertIn('xml:lang="es"', opf)
            chap = zf.read("OEBPS/chap-01.xhtml").decode("utf-8")
            self.assertIn('lang="es"', chap)
            self.assertNotIn('xml:lang="en"', chap)

    def test_lang_code_mapping(self):
        self.assertEqual(_lang_code("English"), "en")
        self.assertEqual(_lang_code("spanish"), "es")
        self.assertEqual(_lang_code("Japanese"), "ja")
        self.assertEqual(_lang_code("pt"), "pt")     # bare code passes through
        self.assertEqual(_lang_code("Klingon"), "en")  # unknown -> en
        self.assertEqual(_lang_code(""), "en")

    def test_dcterms_modified_is_real_and_overridable(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            meta = self._meta(graph)
            # explicit override (deterministic builds)
            zf = zipfile.ZipFile(io.BytesIO(
                build_epub(graph, meta, modified="2020-05-05T00:00:00Z")))
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn(
                '<meta property="dcterms:modified">2020-05-05T00:00:00Z</meta>', opf)
            # default: a real UTC timestamp, not the old frozen constant
            zf2 = zipfile.ZipFile(io.BytesIO(build_epub(graph, meta)))
            opf2 = zf2.read("OEBPS/content.opf").decode("utf-8")
            m = re.search(
                r'<meta property="dcterms:modified">([^<]+)</meta>', opf2)
            self.assertIsNotNone(m)
            stamp = m.group(1)
            self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertNotEqual(stamp, "2026-01-01T00:00:00Z")

    def test_art_embedded_as_raster_cover_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            zf = zipfile.ZipFile(io.BytesIO(build_epub(
                graph, self._meta(graph), art_bytes=_TINY_PNG, art_ext="png")))
            names = zf.namelist()
            self.assertIn("OEBPS/cover.png", names)
            self.assertNotIn("OEBPS/cover.svg", names)
            self.assertEqual(zf.read("OEBPS/cover.png"), _TINY_PNG)
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn('href="cover.png" media-type="image/png" '
                          'properties="cover-image"', opf)
            cover = zf.read("OEBPS/cover.xhtml").decode("utf-8")
            self.assertIn('src="cover.png"', cover)
            self.assertNotIn("cover.svg", cover)


class TestKit(unittest.TestCase):
    def test_kit_writes_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp, genre="mystery")
            meta = generate_kdp_metadata(
                MockLLM(), settings, CostLedger(), graph,
                author_first="Jane", author_last="Doe",
            )
            out = os.path.join(tmp, "kdp")
            result = build_kdp_kit(graph, meta, out)
            paths = result["paths"]
            for key in ("metadata", "epub", "cover", "listing", "checklist"):
                self.assertTrue(os.path.exists(paths[key]), f"missing {key}")
            # metadata.json round-trips to the same dict
            with open(paths["metadata"], encoding="utf-8") as f:
                self.assertEqual(json.load(f), meta.to_dict())
            self.assertEqual(result["metadata"], meta.to_dict())
            # epub on disk is a valid zip
            with open(paths["epub"], "rb") as f:
                zf = zipfile.ZipFile(io.BytesIO(f.read()))
                self.assertIsNone(zf.testzip())
            # listing labels every page-1 field
            with open(paths["listing"], encoding="utf-8") as f:
                listing = f.read()
            for label in ("Language:", "Book Title:", "Subtitle:", "Series:",
                          "Edition Number:", "Primary Author:", "Contributors:",
                          "Description", "Publishing Rights:",
                          "Sexually Explicit", "Reading age:",
                          "Primary marketplace:", "Categories", "Keywords"):
                self.assertIn(label, listing)
            self.assertIn(STORYTELLER_KEYWORD, listing)
            # checklist mentions upload + ISBN + storyteller tip
            with open(paths["checklist"], encoding="utf-8") as f:
                checklist = f.read()
            self.assertIn("manuscript.epub", checklist)
            self.assertIn("ISBN", checklist)
            self.assertIn(STORYTELLER_KEYWORD, checklist)
            # KDP rejects SVG covers — the checklist must never say to upload one
            self.assertNotIn("upload `cover.svg`", checklist)
            self.assertIn("KDP does NOT accept SVG", checklist)

    def _kit_meta(self, graph):
        return KdpMetadata(
            title=graph.bible.title or "Test Book",
            author_first="Jane", author_last="Doe",
            description="A blurb.", keywords=["k1"], categories=["c1"],
        )

    def test_kit_with_art_writes_raster_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            out = os.path.join(tmp, "kdp")
            result = build_kdp_kit(graph, self._kit_meta(graph), out,
                                   art_bytes=_TINY_PNG, art_ext="png")
            paths = result["paths"]
            # the raster cover is copied into the kit...
            self.assertIn("cover_image", paths)
            self.assertTrue(paths["cover_image"].endswith("cover.png"))
            with open(paths["cover_image"], "rb") as f:
                self.assertEqual(f.read(), _TINY_PNG)
            # ...embedded directly in the EPUB (no base64-inflated SVG cover)...
            with open(paths["epub"], "rb") as f:
                zf = zipfile.ZipFile(io.BytesIO(f.read()))
            self.assertIn("OEBPS/cover.png", zf.namelist())
            self.assertNotIn("OEBPS/cover.svg", zf.namelist())
            # ...and the checklist points at it, not at the SVG
            with open(paths["checklist"], encoding="utf-8") as f:
                checklist = f.read()
            self.assertIn("cover.png", checklist)
            self.assertNotIn("upload `cover.svg`", checklist)

    def test_kit_discovers_cover_art_on_disk(self):
        # generate-cover saves kdp/cover-art.<ext>; the kit picks it up without
        # the caller passing bytes.
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            out = os.path.join(tmp, "kdp")
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "cover-art.png"), "wb") as f:
                f.write(_TINY_PNG)
            result = build_kdp_kit(graph, self._kit_meta(graph), out)
            self.assertIn("cover_image", result["paths"])
            with open(result["paths"]["epub"], "rb") as f:
                zf = zipfile.ZipFile(io.BytesIO(f.read()))
            self.assertIn("OEBPS/cover.png", zf.namelist())
            self.assertEqual(zf.read("OEBPS/cover.png"), _TINY_PNG)

    def test_kit_without_art_keeps_svg_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            out = os.path.join(tmp, "kdp")
            result = build_kdp_kit(graph, self._kit_meta(graph), out)
            self.assertNotIn("cover_image", result["paths"])
            with open(result["paths"]["epub"], "rb") as f:
                zf = zipfile.ZipFile(io.BytesIO(f.read()))
            self.assertIn("OEBPS/cover.svg", zf.namelist())


if __name__ == "__main__":
    unittest.main()
