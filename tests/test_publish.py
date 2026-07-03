"""Tests for the new Publish features: all-field KDP autofill, AI cover/back-cover
SVG composition, and PDF exports. All offline (no image/network calls)."""
import io
import os
import tempfile
import unittest
from importlib.util import find_spec

_HAS_PIL = bool(find_spec("PIL"))

from bookwriter.config import Settings
from bookwriter.costs import CostLedger
from bookwriter.mock import MockLLM
from bookwriter.pipeline import BookPipeline
from bookwriter.kdp import (
    KdpMetadata, generate_kdp_metadata, compose_cover_svg, back_cover_svg,
)
from bookwriter import pdf, images


def _make_graph(chapters=2):
    tmp = tempfile.mkdtemp()
    p = BookPipeline(MockLLM(), Settings(project_dir=tmp).with_profile("draft"))
    p.plan(premise="a lighthouse keeper and the thing in the water",
           chapters=chapters, words_per_chapter=120)
    p.write_all()
    return p.graph


def _png(w=64, h=96):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), (20, 30, 50)).save(b, "PNG")
    return b.getvalue()


class TestKdpAutofillFields(unittest.TestCase):
    def test_generate_includes_back_cover_and_bio(self):
        meta = generate_kdp_metadata(
            MockLLM(), Settings().with_profile("draft"), CostLedger(), _make_graph(),
            author_first="Vera", author_last="Solenne",
        )
        for attr in ("back_cover_blurb", "author_bio", "edition", "series_part"):
            self.assertTrue(hasattr(meta, attr), attr)
        # back-cover blurb is always populated (falls back to the description).
        self.assertTrue(meta.back_cover_blurb)
        # round-trips through to_dict/from_dict (used to persist kdp.json)
        rt = KdpMetadata.from_dict(meta.to_dict())
        self.assertEqual(rt.back_cover_blurb, meta.back_cover_blurb)
        self.assertEqual(rt.author_bio, meta.author_bio)


@unittest.skipUnless(_HAS_PIL, "Pillow not installed")
class TestCoverComposition(unittest.TestCase):
    def test_front_cover_embeds_art_and_title(self):
        meta = KdpMetadata(title="The Tidewatcher", author_first="Vera", author_last="Solenne",
                           subtitle="A coastal horror")
        svg = compose_cover_svg(meta, _png(), "png")
        self.assertIn("<svg", svg)
        self.assertIn("data:image/png;base64", svg)
        self.assertIn("THE TIDEWATCHER", svg)
        self.assertIn("Vera Solenne", svg)

    def test_back_cover_renders_blurb_and_bio(self):
        g = _make_graph()
        meta = KdpMetadata(title="T", author_first="Vera", author_last="Solenne",
                           back_cover_blurb="A keeper. A light. Something patient.",
                           author_bio="Vera writes coastal horror.")
        svg = back_cover_svg(g, meta, art_bytes=_png(), ext="png")
        self.assertIn("<svg", svg)
        self.assertIn("patient", svg)
        self.assertIn("About the author", svg)
        # also works with no art (solid background)
        self.assertIn("<svg", back_cover_svg(g, meta))


def _pdf_content_streams(data: bytes) -> bytes:
    """Concatenate all PDF stream bodies, decoded where possible (reportlab
    emits Flate, optionally wrapped in ASCII85)."""
    import base64
    import re
    import zlib
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        chunk = m.group(1)
        try:
            out += zlib.decompress(chunk)
            continue
        except zlib.error:
            pass
        try:
            decoded = base64.a85decode(chunk.strip(), adobe=True)
            try:
                out += zlib.decompress(decoded)
            except zlib.error:
                out += decoded
        except ValueError:
            out += chunk
    return out


class TestPdfInstallHint(unittest.TestCase):
    def test_install_hint_mentions_pypdf(self):
        # the [pdf] extra includes pypdf; the alternate pip command must too,
        # or users land on the degraded single-document fallback.
        self.assertIn("pypdf", pdf._INSTALL_HINT)


@unittest.skipUnless(pdf.pdf_available(), "reportlab not installed")
class TestPdfExports(unittest.TestCase):
    def setUp(self):
        self.g = _make_graph()
        self.meta = KdpMetadata(title="The Tidewatcher", author_first="Vera", author_last="Solenne",
                                back_cover_blurb="Hook.", author_bio="Bio.")

    def test_all_parts_produce_pdf(self):
        png = _png()
        for part in pdf.PDF_PARTS:
            data = pdf.build_pdf(part, self.g, self.meta, art_bytes=png, ext="png")
            self.assertEqual(data[:5], b"%PDF-", part)
            self.assertGreater(len(data), 800, part)

    def test_covers_work_without_art(self):
        for part in ("front-cover", "back-cover"):
            self.assertEqual(pdf.build_pdf(part, self.g, self.meta)[:5], b"%PDF-")

    def test_bad_part_raises(self):
        with self.assertRaises(ValueError):
            pdf.build_pdf("nope", self.g, self.meta)

    def test_full_pdf_single_fallback_visible_without_art(self):
        # The no-pypdf fallback must not render white title text on a white
        # page: with no art it paints the same dark ground as front_cover_pdf,
        # and the title page carries the author (previously dropped).
        from reportlab.lib.colors import HexColor
        try:
            from reportlab.lib.rl_accel import fp_str
        except ImportError:  # older reportlab exposed it via lib.utils
            from reportlab.lib.utils import fp_str
        data = pdf._full_pdf_single(self.g, self.meta)
        self.assertEqual(data[:5], b"%PDF-")
        streams = _pdf_content_streams(data)
        col = HexColor("#15171c")
        ground = f"{fp_str(col.red, col.green, col.blue)} rg".encode("ascii")
        self.assertIn(ground, streams)          # dark cover ground painted
        self.assertIn(b"Vera Solenne", streams)  # author on the title page

    def test_full_pdf_single_fallback_with_art(self):
        data = pdf._full_pdf_single(self.g, self.meta, art_bytes=_png(), ext="png")
        self.assertEqual(data[:5], b"%PDF-")
        self.assertIn(b"Vera Solenne", _pdf_content_streams(data))

    def test_full_pdf_single_long_title_stays_on_cover_page(self):
        # A long title/subtitle must shrink to fit page 1, not overflow the
        # frame and push white cover text onto the white second page.
        import re
        page_re = re.compile(rb"/Type\s*/Page\b")
        base_pages = len(page_re.findall(pdf._full_pdf_single(self.g, self.meta)))
        long_meta = KdpMetadata(
            title=("The Unabridged and Deeply Considered Chronicle of the "
                   "Tidewatcher of Greyhaven Point and Everything the Water "
                   "Ever Whispered to Her"),
            subtitle=("Being a Complete Account of the Light, the Keeper, and "
                      "the Patient Thing Beneath the Waves, in Three Movements"),
            author_first="Vera", author_last="Solenne",
            back_cover_blurb="Hook.", author_bio="Bio.")
        data = pdf._full_pdf_single(self.g, long_meta)
        self.assertEqual(len(page_re.findall(data)), base_pages)
        self.assertIn(b"Vera Solenne", _pdf_content_streams(data))


class TestCoverArtPrompt(unittest.TestCase):
    def test_prompt_is_text_free(self):
        pr = images.build_cover_prompt(_make_graph().bible)
        self.assertIn("no text", pr.lower())
        self.assertTrue(len(pr) <= 1100)

    def test_generate_cover_art_requires_backend(self):
        saved = os.environ.pop("PIXIO_API_KEY", None)
        try:
            with self.assertRaises(Exception):
                images.generate_cover_art(_make_graph().bible)
        finally:
            if saved is not None:
                os.environ["PIXIO_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
