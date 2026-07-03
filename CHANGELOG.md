# Changelog

All notable changes to BookwriterPro are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- GitHub Actions CI: the offline suite runs on every push — a no-extras pass
  (server/MCP/PDF tests skip cleanly) and a full pass with every extra
  installed, on Ubuntu and Windows across Python 3.10 and 3.13. The README's
  hand-maintained test badge is replaced by the live CI status badge.
- This changelog.

### Changed
- Repo-wide audit wave: robustness, documentation-accuracy, and test-coverage
  fixes across the engine, server, MCP, and web surfaces.
- `docs/ARCHITECTURE.md` caught up with the shipped code: the multi-provider
  LLM seam (`provider.make_llm()`), the twelve previously undocumented modules
  (provider, runtime_config, the three extra LLM backends, images, importer,
  kdp, royalties, print_export, pdf, prompts), and a corrected layering rule.
- README renders correctly off-GitHub (PyPI): absolute image and file links,
  `grok`/`cli` added to the provider references, and a note that the
  OpenAI-compatible API backends need the `[openai]` extra.
- Packaging modernized: SPDX `license = "MIT"` (setuptools >= 77) and a
  `[tool.pytest.ini_options]` section (collect `tests/` only; silence the
  pytest-asyncio loop-scope deprecation warning).

## [1.1.0] - 2026-07-03

### Added
- **Grok (xAI) as a first-class API backend** — `BOOKWRITER_LLM_PROVIDER=grok`
  with `GROK_API_KEY` (or `XAI_API_KEY`), alongside the existing `grok-cli`
  subscription path.
- **Genre-rich create options** and genre-accurate procedural fiction covers.
- **Manuscript import** — paste or drop a `.txt`/`.md` draft; it is split into
  chapters with a reverse-engineered story bible + continuity graph, then
  editable, revisable, continuable (+N chapters), and publishable like any
  other book (UI, HTTP, MCP, and CLI).
- **Publish to KDP** — AI autofill for every listing field, AI cover and back
  cover, PDF exports (full book / interior / front / back), print spec, and
  royalty estimates.

### Fixed
- The chapter-length picker actually controls the generated word count.
- PDF export buttons surface backend errors instead of failing silently.
- MCP: clean credential errors, orphaned-book cleanup, and KDP tool parity
  with the HTTP service contract.
- Import/continuity robustness issues found by an adversarial audit.

### Security
- Server hardening: request validation UX, secret-safety (keys masked, never
  logged), and version/SSRF hardening.

## [1.0.0] - 2026-06-18

### Added
- Initial public release: the four-stage pipeline (Planner → Writer →
  Extractor → Checker) over a committed continuity knowledge graph, with
  prompt-cached bible spine, bounded rolling synopsis, model tiering, and a
  per-call cost ledger.
- Three surfaces over one engine: the no-build web studio (live cover forge,
  token-by-token streaming, 3D page-turn reader, character graph, command
  palette), a FastAPI HTTP/SSE API, and an MCP stdio server.
- Multi-provider LLMs (Anthropic / OpenAI / OpenRouter APIs and
  Claude/ChatGPT/Grok subscription CLIs), in-app Settings, chapter
  illustrations, KDP fast-publish (EPUB, DOCX interior, pricing, marketing),
  demo mode, and an offline test suite.

[Unreleased]: https://github.com/RealDealCPA-VR/BookwritterPro/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/RealDealCPA-VR/BookwritterPro/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/RealDealCPA-VR/BookwritterPro/releases/tag/v1.0.0
