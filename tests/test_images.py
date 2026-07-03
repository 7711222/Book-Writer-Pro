"""Tests for the pluggable chapter-image layer (images.py) and the pipeline hook.

All offline: HTTP is monkeypatched, so no Pixio/OpenAI calls and no network.
"""
import os
import tempfile
import unittest
from unittest import mock

from bookwriter import images
from bookwriter import runtime_config as rcmod
from bookwriter.config import Settings, QUALITY_PROFILES
from bookwriter.mock import MockLLM
from bookwriter.pipeline import BookPipeline


class _EnvGuard(unittest.TestCase):
    _VARS = [
        "BOOKWRITER_IMAGE_PROVIDER", "PIXIO_API_KEY", "OPENAI_API_KEY",
        "BOOKWRITER_IMAGE_URL", "BOOKWRITER_PIXIO_MODEL", "PIXIO_IMAGE_MODEL",
        "BOOKWRITER_IMAGE_BODY", "BOOKWRITER_IMAGE_RESULT_PATH", "BOOKWRITER_IMAGE_AUTH",
        "BOOKWRITER_IMAGE_ALLOW_PRIVATE",
    ]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        self._saved_overrides = {k: rcmod._overrides.get(k) for k in self._VARS}
        for k in self._VARS:
            os.environ.pop(k, None)
            rcmod._overrides.pop(k, None)  # runtime overrides shadow os.environ
        images.PixioImageProvider._discovered_model = None

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved_overrides.items():
            if v is None:
                rcmod._overrides.pop(k, None)
            else:
                rcmod._overrides[k] = v


class TestImageSelection(_EnvGuard):
    def test_default_provider_is_pixio(self):
        self.assertEqual(images.image_provider_name(), "pixio")

    def test_aliases(self):
        for raw, want in [("OpenAI", "openai"), ("dall-e", "openai"),
                          ("custom", "http"), ("PIXIO", "pixio")]:
            os.environ["BOOKWRITER_IMAGE_PROVIDER"] = raw
            self.assertEqual(images.image_provider_name(), want)

    def test_availability_gating(self):
        self.assertFalse(images.image_available("pixio"))
        os.environ["PIXIO_API_KEY"] = "pxio_live_x"
        self.assertTrue(images.image_available("pixio"))
        self.assertFalse(images.image_available("http"))
        os.environ["BOOKWRITER_IMAGE_URL"] = "https://x/y"
        self.assertTrue(images.image_available("http"))

    def test_status_shape(self):
        os.environ["PIXIO_API_KEY"] = "pxio_live_x"
        s = images.image_status()
        self.assertEqual(s, {"provider": "pixio", "available": True})

    def test_make_provider_dispatch(self):
        self.assertIsInstance(images.make_image_provider("pixio"), images.PixioImageProvider)
        self.assertIsInstance(images.make_image_provider("openai"), images.OpenAIImageProvider)
        self.assertIsInstance(images.make_image_provider("http"), images.HttpImageProvider)


class TestPixioProvider(_EnvGuard):
    def test_generate_discovers_model_polls_and_downloads(self):
        os.environ["PIXIO_API_KEY"] = "pxio_live_x"
        prov = images.PixioImageProvider()
        models = {"models": [
            {"id": "pixio/nano-banana/edit", "type": "image-to-image"},
            {"id": "pixio/flux/dev", "type": "text-to-image"},
        ]}
        gen_started = {"contentId": "gen-123"}
        polls = [
            {"status": "processing"},
            {"status": "succeeded", "outputUrl": "https://cdn.pixio.ai/out/x.png"},
        ]
        with mock.patch.object(images, "_get_json") as gj, \
             mock.patch.object(images, "_post_json", return_value=gen_started) as pj, \
             mock.patch.object(images, "_download", return_value=b"PNGDATA") as dl, \
             mock.patch.object(images.time, "sleep"):
            # First _get_json is the models list, then generation polls.
            gj.side_effect = [models] + polls
            data, ext = prov.generate("a lighthouse at dusk")
        self.assertEqual(data, b"PNGDATA")
        self.assertEqual(ext, "png")
        # Picked the text-to-image model, not the edit model.
        self.assertEqual(pj.call_args.args[1]["modelId"], "pixio/flux/dev")
        self.assertEqual(pj.call_args.args[1]["params"]["prompt"], "a lighthouse at dusk")
        dl.assert_called_once()

    def test_explicit_model_skips_discovery(self):
        os.environ["PIXIO_API_KEY"] = "pxio_live_x"
        os.environ["BOOKWRITER_PIXIO_MODEL"] = "pixio/flux/schnell"
        prov = images.PixioImageProvider()
        with mock.patch.object(images, "_get_json", return_value={"status": "succeeded", "outputUrl": "https://x/a.jpg"}) as gj, \
             mock.patch.object(images, "_post_json", return_value={"contentId": "c"}), \
             mock.patch.object(images, "_download", return_value=b"JJ"), \
             mock.patch.object(images.time, "sleep"):
            data, ext = prov.generate("x")
        self.assertEqual((data, ext), (b"JJ", "jpg"))
        # Only the poll GET happened — no /models discovery call.
        self.assertEqual(gj.call_count, 1)

    def test_no_key_raises(self):
        with self.assertRaises(RuntimeError):
            images.PixioImageProvider().generate("x")


class TestOpenAIAndHttpProviders(_EnvGuard):
    def test_openai_b64(self):
        os.environ["OPENAI_API_KEY"] = "sk-x"
        prov = images.OpenAIImageProvider()
        import base64
        b64 = base64.b64encode(b"IMGBYTES").decode()
        with mock.patch.object(images, "_post_json", return_value={"data": [{"b64_json": b64}]}):
            data, ext = prov.generate("a castle")
        self.assertEqual((data, ext), (b"IMGBYTES", "png"))

    def test_http_generic_with_result_path(self):
        os.environ["BOOKWRITER_IMAGE_URL"] = "https://api.example.com/img"
        os.environ["BOOKWRITER_IMAGE_BODY"] = '{"prompt": "{prompt}", "n": 1}'
        os.environ["BOOKWRITER_IMAGE_RESULT_PATH"] = "data.0.url"
        prov = images.HttpImageProvider()
        captured = {}

        def fake_post(url, payload, headers, timeout=180.0):
            captured["payload"] = payload
            return {"data": [{"url": "https://cdn/x.webp"}]}

        with mock.patch.object(images, "_post_json", side_effect=fake_post), \
             mock.patch.object(images, "_download", return_value=b"WB"):
            data, ext = prov.generate('a "quoted" prompt')
        self.assertEqual((data, ext), (b"WB", "webp"))
        # Prompt was substituted and JSON stayed valid despite the quotes.
        self.assertEqual(captured["payload"]["prompt"], 'a "quoted" prompt')
        self.assertEqual(captured["payload"]["n"], 1)

    def test_dig_helper(self):
        self.assertEqual(images._dig({"a": [{"b": 5}]}, "a.0.b"), 5)
        self.assertIsNone(images._dig({"a": []}, "a.3.b"))


# --------------------------------------------------------------------------- #
# Outbound-fetch hardening: SSRF guard + response-size cap (_check_url et al.)
# --------------------------------------------------------------------------- #

class _FakeResp:
    """Minimal stand-in for an http.client.HTTPResponse."""

    def __init__(self, body=b"", content_length=None, code=200):
        self._body = body
        self.headers = ({} if content_length is None
                        else {"Content-Length": str(content_length)})
        self._code = code

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestUrlGuards(_EnvGuard):
    # IP-literal URLs only — getaddrinfo resolves them without touching DNS.

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(RuntimeError) as ctx:
            images._check_url("file:///etc/passwd")
        self.assertIn("non-http", str(ctx.exception))

    def test_rejects_loopback_by_default(self):
        with self.assertRaises(RuntimeError) as ctx:
            images._check_url("http://127.0.0.1:8188/generate")
        self.assertIn("BOOKWRITER_IMAGE_ALLOW_PRIVATE", str(ctx.exception))

    def test_rejects_cloud_metadata_address(self):
        with self.assertRaises(RuntimeError):
            images._check_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_private_range(self):
        with self.assertRaises(RuntimeError):
            images._check_url("http://10.0.0.5/img.png")

    def test_private_allowed_with_optin_flag(self):
        os.environ["BOOKWRITER_IMAGE_ALLOW_PRIVATE"] = "1"
        images._check_url("http://127.0.0.1:8188/generate")  # must not raise

    def test_optin_flag_still_refuses_non_http(self):
        os.environ["BOOKWRITER_IMAGE_ALLOW_PRIVATE"] = "1"
        with self.assertRaises(RuntimeError):
            images._check_url("file:///etc/passwd")

    def test_public_address_allowed(self):
        images._check_url("https://93.184.216.34/img.png")  # must not raise

    def test_redirect_targets_are_rechecked(self):
        import urllib.request
        handler = images._SafeRedirectHandler()
        req = urllib.request.Request("https://93.184.216.34/a")
        with self.assertRaises(RuntimeError):
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://169.254.169.254/latest/meta-data/")

    def test_http_provider_refuses_private_result_url(self):
        # The endpoint answers, but points the "image" at an internal address.
        os.environ["BOOKWRITER_IMAGE_URL"] = "https://api.example.com/img"
        prov = images.HttpImageProvider()
        with mock.patch.object(images, "_post_json",
                               return_value={"url": "http://169.254.169.254/secret"}):
            with self.assertRaises(RuntimeError) as ctx:
                prov.generate("a castle")
        self.assertIn("private/internal", str(ctx.exception))


class TestResponseSizeCap(_EnvGuard):
    def test_declared_content_length_over_cap_rejected(self):
        with mock.patch.object(images, "MAX_FETCH_BYTES", 10):
            with self.assertRaises(RuntimeError) as ctx:
                images._read_capped(_FakeResp(b"x", content_length=11), "https://x/y")
        self.assertIn("cap", str(ctx.exception))

    def test_oversized_body_without_length_rejected(self):
        with mock.patch.object(images, "MAX_FETCH_BYTES", 10):
            with self.assertRaises(RuntimeError):
                images._read_capped(_FakeResp(b"x" * 11), "https://x/y")

    def test_body_at_cap_ok(self):
        with mock.patch.object(images, "MAX_FETCH_BYTES", 10):
            self.assertEqual(images._read_capped(_FakeResp(b"x" * 10), "u"), b"x" * 10)

    def test_download_reads_through_capped_request(self):
        with mock.patch.object(images, "_opener") as op:
            op.open.return_value = _FakeResp(b"IMGBYTES")
            body = images._download("https://93.184.216.34/x.png")
        self.assertEqual(body, b"IMGBYTES")
        op.open.assert_called_once()


class TestChapterPrompt(unittest.TestCase):
    def test_prompt_has_no_text_directive(self):
        from bookwriter.models import Bible, ChapterPlan, Character, Location
        bible = Bible(title="T", genre="literary horror", tone="dread",
                      characters=[Character(id="hero", name="Wren", appearance="tall, weathered")],
                      locations=[Location(id="home", name="The Light", description="a dying coast")])
        plan = ChapterPlan(number=1, title="The First Sign", purpose="hook the reader",
                           pov_character="hero", location_ids=["home"])
        p = images.build_chapter_prompt(bible, plan)
        self.assertIn("The First Sign", p)
        self.assertIn("Wren", p)
        self.assertIn("The Light", p)
        self.assertIn("literary horror", p)
        self.assertIn("No text", p)


class _FakeImageProvider:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def generate(self, prompt, **kw):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("boom")
        return b"\x89PNG-fake", "png"


class TestPipelineImageHook(unittest.TestCase):
    def _plan_and_settings(self, tmp, chapter_images):
        s = Settings(project_dir=tmp)
        s.profile = QUALITY_PROFILES["draft"]
        s.run_continuity_check = False
        s.chapter_images = chapter_images
        return s

    def test_image_generated_and_event_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov = _FakeImageProvider()
            events = []
            pipe = BookPipeline(MockLLM(), self._plan_and_settings(tmp, True),
                                on_event=events.append, image_provider=prov)
            pipe.plan(premise="a lighthouse mystery", chapters=2)
            pipe.write_all()
            done = [e for e in events if e.get("type") == "chapter_done"]
            self.assertTrue(done and all(e["image"] for e in done))
            self.assertTrue(prov.calls)  # the provider was actually invoked
            # Images persisted to disk for each chapter.
            self.assertTrue(pipe.store.has_image(1))

    def test_disabled_skips_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov = _FakeImageProvider()
            pipe = BookPipeline(MockLLM(), self._plan_and_settings(tmp, False),
                                image_provider=prov)
            pipe.plan(premise="x", chapters=1)
            pipe.write_all()
            self.assertEqual(prov.calls, [])
            self.assertFalse(pipe.store.has_image(1))

    def test_image_failure_is_best_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov = _FakeImageProvider(fail=True)
            events = []
            pipe = BookPipeline(MockLLM(), self._plan_and_settings(tmp, True),
                                on_event=events.append, image_provider=prov)
            pipe.plan(premise="x", chapters=1)
            pipe.write_all()  # must not raise
            done = [e for e in events if e.get("type") == "chapter_done"]
            self.assertTrue(done and not any(e["image"] for e in done))
            self.assertFalse(pipe.store.has_image(1))


try:
    from bookwriter.server.service import BookService  # noqa: F401
    _HAS_SERVER = True
except Exception:
    _HAS_SERVER = False


@unittest.skipUnless(_HAS_SERVER, "server extras not installed")
class TestManuscriptImageMarkers(unittest.TestCase):
    def test_chapter_image_marker_injected(self):
        from bookwriter.server.service import BookService
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(project_dir=os.path.join(tmp, "bk"))
            s.profile = QUALITY_PROFILES["draft"]
            s.run_continuity_check = False
            s.chapter_images = True
            pipe = BookPipeline(MockLLM(), s, image_provider=_FakeImageProvider())
            pipe.plan(premise="x", chapters=1)
            pipe.write_all()
            md = "# Book\n\n## Chapter 1: The Start\n\nSome body text."
            out = BookService._with_chapter_images("bk", pipe.store, pipe.graph, md)
            self.assertIn("![Chapter 1 illustration](/api/books/bk/chapters/1/image)", out)
            # The original heading and prose are preserved.
            self.assertIn("## Chapter 1: The Start", out)
            self.assertIn("Some body text.", out)


class TestEpubImageEmbedding(unittest.TestCase):
    def test_epub_embeds_chapter_images(self):
        import io
        import zipfile
        from bookwriter.kdp import build_epub, KdpMetadata
        with tempfile.TemporaryDirectory() as tmp:
            prov = _FakeImageProvider()
            s = Settings(project_dir=tmp)
            s.profile = QUALITY_PROFILES["draft"]
            s.run_continuity_check = False
            s.chapter_images = True
            pipe = BookPipeline(MockLLM(), s, image_provider=prov)
            pipe.plan(premise="a lighthouse mystery", chapters=2)
            pipe.write_all()

            images = pipe.store.collect_images([p.number for p in pipe.graph.bible.outline])
            self.assertTrue(images)  # images were collected from disk
            meta = KdpMetadata(title="T", author_first="A", author_last="B")
            epub = build_epub(pipe.graph, meta, images=images)

            with zipfile.ZipFile(io.BytesIO(epub)) as zf:
                names = zf.namelist()
                # An image file is embedded and referenced by a chapter XHTML.
                imgs = [n for n in names if n.startswith("OEBPS/img-")]
                self.assertTrue(imgs, f"no embedded images in {names}")
                opf = zf.read("OEBPS/content.opf").decode("utf-8")
                self.assertIn('media-type="image/png"', opf)
                chap1 = zf.read("OEBPS/chap-01.xhtml").decode("utf-8")
                self.assertIn("<img", chap1)

    def test_epub_without_images_is_unchanged_shape(self):
        import io
        import zipfile
        from bookwriter.kdp import build_epub, KdpMetadata
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(project_dir=tmp)
            s.profile = QUALITY_PROFILES["draft"]
            s.run_continuity_check = False
            pipe = BookPipeline(MockLLM(), s)
            pipe.plan(premise="x", chapters=1)
            pipe.write_all()
            epub = build_epub(pipe.graph, KdpMetadata(title="T", author_first="A", author_last="B"))
            with zipfile.ZipFile(io.BytesIO(epub)) as zf:
                self.assertFalse([n for n in zf.namelist() if n.startswith("OEBPS/img-")])


if __name__ == "__main__":
    unittest.main()
