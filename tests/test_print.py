import io
import json
import os
import tempfile
import unittest
import zipfile
from xml.dom import minidom

from bookwriter.config import Settings
from bookwriter.costs import CostLedger
from bookwriter.mock import MockLLM
from bookwriter.pipeline import BookPipeline
from bookwriter.kdp import (
    KdpMetadata, generate_marketing, build_kdp_kit,
    MARKETING_SCHEMA, MAX_BLURB_VARIANTS, MAX_APLUS_MODULES, MAX_TAGLINES,
    MAX_BLURB_CHARS,
)
from bookwriter.print_export import (
    build_docx, print_spec, build_print_cover_svg,
    BLEED_IN, MIN_PAGE_COUNT, TWIPS_PER_INCH,
)
from bookwriter.royalties import (
    estimate_royalties, estimate_page_count,
    DELIVERY_FEE_PER_MB_US, MIN_DELIVERY_FEE,
    PB_SMALL_FLAT_COST_US, PB_LARGE_FIXED_COST_US, PB_LARGE_PER_PAGE_US,
    PAPERBACK_ROYALTY_RATE_LOW, PAPERBACK_ROYALTY_RATE_HIGH,
)


def _build_book(tmp, *, premise="a detective hunts a small town killer",
                chapters=3, genre=None):
    settings = Settings(project_dir=tmp).with_profile("balanced")
    pipe = BookPipeline(MockLLM(), settings)
    pipe.plan(premise=premise, chapters=chapters, words_per_chapter=150)
    if genre:
        pipe.graph.bible.genre = genre
    pipe.write_all()
    return pipe.graph, settings


def _meta(graph):
    return KdpMetadata(
        title=graph.bible.title or "Test Book",
        subtitle="A Subtitle",
        author_first="Jane", author_last="Doe",
        contributors=[{"first": "John", "last": "Smith"}],
        description="A gripping blurb that hooks you instantly.",
        keywords=["k1"], categories=["c1"],
    )


class TestDocx(unittest.TestCase):
    def test_docx_is_valid_zip_with_required_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            data = build_docx(graph, _meta(graph))
            self.assertIsInstance(data, bytes)
            zf = zipfile.ZipFile(io.BytesIO(data))
            self.assertIsNone(zf.testzip())
            names = zf.namelist()
            for required in ("[Content_Types].xml", "_rels/.rels",
                             "word/document.xml", "word/_rels/document.xml.rels",
                             "word/styles.xml"):
                self.assertIn(required, names)

    def test_all_xml_well_formed(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            zf = zipfile.ZipFile(io.BytesIO(build_docx(graph, _meta(graph))))
            for name in zf.namelist():
                minidom.parseString(zf.read(name))

    def test_page_size_matches_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            zf = zipfile.ZipFile(io.BytesIO(build_docx(graph, _meta(graph),
                                                       trim=(6.0, 9.0))))
            doc = zf.read("word/document.xml").decode("utf-8")
            # 6x9 inches -> w=8640 h=12960 twips
            self.assertIn(f'w:w="{6 * TWIPS_PER_INCH}"', doc)
            self.assertIn(f'w:h="{9 * TWIPS_PER_INCH}"', doc)

    def test_chapter_text_never_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp, chapters=4)
            zf = zipfile.ZipFile(io.BytesIO(build_docx(graph, _meta(graph))))
            doc_text = minidom.parseString(
                zf.read("word/document.xml")).documentElement.toxml()
            for n in sorted(graph.chapters):
                rec = graph.chapters[n]
                for w in rec.text.split():
                    if w.isalpha() and len(w) > 3:
                        self.assertIn(w, doc_text, f"chapter {n} lost word {w!r}")

    def test_title_author_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            meta = _meta(graph)
            zf = zipfile.ZipFile(io.BytesIO(build_docx(graph, meta)))
            doc = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("Jane Doe", doc)
            self.assertIn("Copyright", doc)
            # a page break before each chapter
            self.assertIn("pageBreakBefore", doc)

    def test_control_chars_do_not_corrupt_docx(self):
        # \x0b (Word soft line break) and other C0 controls routinely survive a
        # copy-paste import; they are XML-1.0-illegal and must be stripped.
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            first = sorted(graph.chapters)[0]
            graph.chapters[first].text = (
                "Alpha\x0bbeta continues the line.\x0c\x01\x1f"
                + chr(0xFFFE) + " Clean tail."   # U+FFFE noncharacter
            )
            data = build_docx(graph, _meta(graph))
            zf = zipfile.ZipFile(io.BytesIO(data))
            for name in zf.namelist():
                raw = zf.read(name)
                minidom.parseString(raw)  # raises on malformed XML
                self.assertNotIn(b"\x0b", raw, name)
            doc = zf.read("word/document.xml").decode("utf-8")
            # \x0b became a space: both halves survive, unglued
            self.assertIn("Alpha beta", doc)
            self.assertIn("Clean tail.", doc)

    def test_footer_page_number_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            zf = zipfile.ZipFile(io.BytesIO(build_docx(graph, _meta(graph))))
            names = zf.namelist()
            self.assertIn("word/footer1.xml", names)
            self.assertIn("word/settings.xml", names)
            footer = zf.read("word/footer1.xml").decode("utf-8")
            minidom.parseString(footer)
            self.assertIn("PAGE", footer)          # the page-number field
            self.assertIn("fldChar", footer)
            # the section references the footer and the parts are registered
            doc = zf.read("word/document.xml").decode("utf-8")
            self.assertIn('<w:footerReference w:type="default" r:id="rId2"/>', doc)
            rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
            self.assertIn('Target="footer1.xml"', rels)
            self.assertIn('Target="settings.xml"', rels)
            ctypes = zf.read("[Content_Types].xml").decode("utf-8")
            self.assertIn("/word/footer1.xml", ctypes)
            self.assertIn("/word/settings.xml", ctypes)

    def test_gutter_and_mirror_margins(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
        # small book (24-page floor) -> KDP's 0.375in minimum gutter
        zf = zipfile.ZipFile(io.BytesIO(build_docx(graph, _meta(graph))))
        doc = zf.read("word/document.xml").decode("utf-8")
        small_gutter = int(round(0.375 * TWIPS_PER_INCH))
        self.assertIn(f'w:gutter="{small_gutter}"', doc)
        settings = zf.read("word/settings.xml").decode("utf-8")
        minidom.parseString(settings)
        self.assertIn("mirrorMargins", settings)
        # very long book -> gutter grows (0.875in for 700+ pages)
        for rec in graph.chapters.values():
            rec.word_count = 100000
        zf2 = zipfile.ZipFile(io.BytesIO(build_docx(graph, _meta(graph))))
        doc2 = zf2.read("word/document.xml").decode("utf-8")
        big_gutter = int(round(0.875 * TWIPS_PER_INCH))
        self.assertIn(f'w:gutter="{big_gutter}"', doc2)


class TestPrintSpec(unittest.TestCase):
    def test_spine_grows_with_pages(self):
        # page count is words/300; use synthetic chapter word counts so the long
        # book clears the 24-page floor and the spine demonstrably grows.
        with tempfile.TemporaryDirectory() as tmp:
            short, _ = _build_book(tmp, chapters=2)
        with tempfile.TemporaryDirectory() as tmp2:
            long, _ = _build_book(tmp2, chapters=2)
        for rec in long.chapters.values():
            rec.word_count = 30000
        s_short = print_spec(short, _meta(short))
        s_long = print_spec(long, _meta(long))
        self.assertGreater(s_long["page_count_estimate"],
                           s_short["page_count_estimate"])
        self.assertGreater(s_long["spine_width_in"], s_short["spine_width_in"])

    def test_full_width_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
        spec = print_spec(graph, _meta(graph), trim=(6.0, 9.0))
        expected_w = (BLEED_IN * 2 + 6.0 * 2 + spec["spine_width_in"])
        self.assertAlmostEqual(spec["full_cover_width_in"], expected_w, places=3)
        self.assertAlmostEqual(spec["full_cover_height_in"], 9.0 + BLEED_IN * 2,
                               places=3)
        # px at 300 dpi
        self.assertEqual(spec["full_cover_width_px"],
                         round(spec["full_cover_width_in"] * 300))
        self.assertGreaterEqual(spec["page_count_estimate"], MIN_PAGE_COUNT)

    def test_min_page_count_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp, chapters=1)
        spec = print_spec(graph, _meta(graph))
        self.assertGreaterEqual(spec["page_count_estimate"], MIN_PAGE_COUNT)

    def test_cream_spine_thicker_than_white(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp, chapters=6)
        for rec in graph.chapters.values():
            rec.word_count = 20000
        white = print_spec(graph, _meta(graph), paper="white")
        cream = print_spec(graph, _meta(graph), paper="cream")
        self.assertGreater(cream["spine_width_in"], white["spine_width_in"])


class TestPrintCoverSvg(unittest.TestCase):
    def test_cover_is_well_formed_and_sized(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
        meta = _meta(graph)
        spec = print_spec(graph, meta)
        svg = build_print_cover_svg(graph, meta, spec)
        dom = minidom.parseString(svg)
        root = dom.documentElement
        self.assertEqual(root.tagName, "svg")
        self.assertEqual(int(root.getAttribute("width")),
                         spec["full_cover_width_px"])
        self.assertIn(meta.title, svg)
        self.assertIn(meta.author_full(), svg)

    def test_front_cover_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
        meta = _meta(graph)
        spec = print_spec(graph, meta)
        front = ('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
                 'width="1600" height="2560"><rect width="1600" height="2560" '
                 'fill="purple" id="frontmark"/></svg>')
        svg = build_print_cover_svg(graph, meta, spec, front_cover_svg=front)
        minidom.parseString(svg)  # still well-formed after embedding
        self.assertIn("frontmark", svg)


class TestRoyalties(unittest.TestCase):
    def test_70_percent_band(self):
        r = estimate_royalties(list_price=4.99, page_count=200, ebook_file_mb=1.0)
        self.assertEqual(r["ebook"]["plan"], "70%")
        self.assertTrue(r["ebook"]["eligible_for_70"])
        # KDP Amazon.com fee is $0.15/MB: 0.70 * (4.99 - 0.15) = 3.388 -> 3.39
        self.assertAlmostEqual(r["ebook"]["delivery_fee"], 0.15, places=2)
        self.assertAlmostEqual(r["ebook"]["royalty_per_sale"], 3.39, places=2)

    def test_delivery_fee_has_one_cent_minimum(self):
        # a tiny file still pays KDP's $0.01 minimum on the 70% plan
        r = estimate_royalties(list_price=4.99, page_count=200,
                               ebook_file_mb=0.01)
        self.assertAlmostEqual(r["ebook"]["delivery_fee"], MIN_DELIVERY_FEE,
                               places=2)
        self.assertAlmostEqual(r["ebook"]["royalty_per_sale"],
                               round(0.70 * (4.99 - MIN_DELIVERY_FEE), 2),
                               places=2)

    def test_delivery_fee_scales_with_size(self):
        # a 5 MB illustrated EPUB pays 5 x $0.15 = $0.75
        r = estimate_royalties(list_price=4.99, page_count=200, ebook_file_mb=5.0)
        self.assertAlmostEqual(r["ebook"]["delivery_fee"],
                               round(5.0 * DELIVERY_FEE_PER_MB_US, 2), places=2)

    def test_below_band_is_35(self):
        r = estimate_royalties(list_price=0.99, page_count=200)
        self.assertEqual(r["ebook"]["plan"], "35%")
        self.assertFalse(r["ebook"]["eligible_for_70"])
        self.assertAlmostEqual(r["ebook"]["royalty_per_sale"], 0.35, places=2)

    def test_above_band_is_35(self):
        r = estimate_royalties(list_price=14.99, page_count=200)
        self.assertEqual(r["ebook"]["plan"], "35%")
        self.assertAlmostEqual(r["ebook"]["royalty_per_sale"],
                               round(0.35 * 14.99, 2), places=2)

    def test_paperback_royalty_formula(self):
        r = estimate_royalties(list_price=12.99, page_count=300, paper="white")
        pb = r["paperback"]
        printing = pb["printing_cost"]
        # KDP's published 110+ page formula: $1.00 + 300 x $0.012 = $4.60
        self.assertAlmostEqual(
            printing,
            round(PB_LARGE_FIXED_COST_US + 300 * PB_LARGE_PER_PAGE_US, 2),
            places=2)
        self.assertAlmostEqual(printing, 4.60, places=2)
        # $12.99 is above the $9.99 threshold -> 60% rate
        self.assertEqual(pb["royalty_rate"], PAPERBACK_ROYALTY_RATE_HIGH)
        expected = round(0.60 * 12.99 - printing, 2)
        self.assertAlmostEqual(pb["royalty_per_sale"], expected, places=2)
        self.assertGreater(printing, 0)

    def test_paperback_small_book_flat_printing_cost(self):
        # 24-108 pages: flat $2.30, no per-page charge
        for pages in (24, 100, 108):
            r = estimate_royalties(list_price=9.99, page_count=pages)
            self.assertAlmostEqual(r["paperback"]["printing_cost"],
                                   PB_SMALL_FLAT_COST_US, places=2,
                                   msg=f"pages={pages}")
        # 110 pages: $1.00 + 110 x $0.012 = $2.32
        r = estimate_royalties(list_price=9.99, page_count=110)
        self.assertAlmostEqual(r["paperback"]["printing_cost"], 2.32, places=2)

    def test_paperback_rate_tiered_by_price(self):
        # Since KDP's June 2025 update: below $9.99 pays 50%, at/above pays 60%.
        low = estimate_royalties(list_price=8.99, page_count=200)["paperback"]
        self.assertEqual(low["royalty_rate"], PAPERBACK_ROYALTY_RATE_LOW)
        self.assertAlmostEqual(
            low["royalty_per_sale"],
            round(0.50 * 8.99 - low["printing_cost"], 2), places=2)
        # $9.99 exactly earns 60% (the boundary is inclusive)
        edge = estimate_royalties(list_price=9.99, page_count=200)["paperback"]
        self.assertEqual(edge["royalty_rate"], PAPERBACK_ROYALTY_RATE_HIGH)

    def test_paperback_royalty_floored_at_zero(self):
        # cheap price, huge page count -> printing exceeds the rate x price
        r = estimate_royalties(list_price=1.00, page_count=800)
        self.assertEqual(r["paperback"]["royalty_per_sale"], 0.0)
        self.assertTrue(r["paperback"]["below_cost"])

    def test_estimate_page_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp, chapters=4)
        pc = estimate_page_count(graph)
        self.assertGreaterEqual(pc, 24)

    def test_note_and_assumptions_present(self):
        r = estimate_royalties(list_price=4.99, page_count=200)
        self.assertEqual(r["note"], "estimates — confirm in KDP")
        self.assertTrue(r["assumptions"])


class TestMarketing(unittest.TestCase):
    def test_schema_strict(self):
        self.assertFalse(MARKETING_SCHEMA["additionalProperties"])
        for key in ("blurb_variants", "a_plus_modules", "author_bio", "taglines"):
            self.assertIn(key, MARKETING_SCHEMA["properties"])
            self.assertIn(key, MARKETING_SCHEMA["required"])

    def test_generate_marketing_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp, genre="thriller")
            meta = _meta(graph)
            mk = generate_marketing(MockLLM(), settings, CostLedger(), graph, meta)
            self.assertLessEqual(len(mk["blurb_variants"]), MAX_BLURB_VARIANTS)
            self.assertTrue(mk["blurb_variants"])
            self.assertLessEqual(len(mk["a_plus_modules"]), MAX_APLUS_MODULES)
            self.assertTrue(mk["a_plus_modules"])
            for mod in mk["a_plus_modules"]:
                self.assertIn("headline", mod)
                self.assertIn("body", mod)
            self.assertLessEqual(len(mk["taglines"]), MAX_TAGLINES)
            self.assertTrue(mk["author_bio"])
            for b in mk["blurb_variants"]:
                self.assertLessEqual(len(b), MAX_BLURB_CHARS)

    def test_generate_marketing_overlong_truncated(self):
        class FatLLM(MockLLM):
            def _marketing(self, user):
                d = super()._marketing(user)
                d["blurb_variants"] = ["word " * 2000, "x", "y", "z", "extra"]
                d["taglines"] = [f"line {i}" for i in range(20)]
                d["a_plus_modules"] = [{"headline": f"h{i}", "body": f"b{i}"}
                                       for i in range(10)]
                return d

        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp)
            mk = generate_marketing(FatLLM(), settings, CostLedger(), graph,
                                    _meta(graph))
            self.assertLessEqual(len(mk["blurb_variants"]), MAX_BLURB_VARIANTS)
            self.assertLessEqual(len(mk["taglines"]), MAX_TAGLINES)
            self.assertLessEqual(len(mk["a_plus_modules"]), MAX_APLUS_MODULES)
            for b in mk["blurb_variants"]:
                self.assertLessEqual(len(b), MAX_BLURB_CHARS)


class TestKitWithPrintAndMarketing(unittest.TestCase):
    def test_kit_writes_print_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, settings = _build_book(tmp, genre="mystery")
            meta = _meta(graph)
            mk = generate_marketing(MockLLM(), settings, CostLedger(), graph, meta)
            out = os.path.join(tmp, "kit")
            result = build_kdp_kit(graph, meta, out, marketing=mk)
            paths = result["paths"]
            # existing ebook artifacts still present
            for key in ("metadata", "epub", "cover", "listing", "checklist"):
                self.assertTrue(os.path.exists(paths[key]), f"missing {key}")
            # new print artifacts
            for key in ("docx", "print_spec", "print_cover", "marketing"):
                self.assertTrue(os.path.exists(paths[key]), f"missing {key}")
            # docx on disk is a valid zip with document.xml
            with open(paths["docx"], "rb") as f:
                zf = zipfile.ZipFile(io.BytesIO(f.read()))
                self.assertIsNone(zf.testzip())
                self.assertIn("word/document.xml", zf.namelist())
            # print-spec is valid json
            with open(paths["print_spec"], encoding="utf-8") as f:
                spec = json.load(f)
                self.assertIn("spine_width_in", spec)
            self.assertIn("print_spec", result)
            self.assertIn("marketing", result)

    def test_kit_without_marketing_skips_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph, _ = _build_book(tmp)
            meta = _meta(graph)
            out = os.path.join(tmp, "kit")
            result = build_kdp_kit(graph, meta, out)  # no marketing
            self.assertNotIn("marketing", result["paths"])
            self.assertFalse(os.path.exists(os.path.join(out, "marketing.json")))
            # but print artifacts always written
            self.assertTrue(os.path.exists(result["paths"]["docx"]))


if __name__ == "__main__":
    unittest.main()
