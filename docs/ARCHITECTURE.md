# BookwriterPro — Architecture

BookwriterPro is a local engine that plans and writes whole books while keeping
characters and plot consistent and driving the **total token cost per book** as
low as possible. This document describes the system end to end: the core
pipeline, the token-cost design, the HTTP API, the SSE prose stream, the MCP
server, and the no-build frontend.

The layering rule that governs the whole codebase:

> The **core package** (`bookwriter/*` outside `server/`) imports only the
> standard library and, lazily, the optional SDKs of the backend actually in
> use (`anthropic`, `openai`, `reportlab`). It must import on a bare Python
> 3.10+ with no third-party installs. `fastapi` is imported **only inside
> `bookwriter/server/`**, `uvicorn` only lazily inside the `serve.py` entry
> point, and `mcp` only lazily inside `mcp_server.py` — never from
> `bookwriter/__init__.py` or any core module. (`httpx` is a test-only
> dependency shipped with the `[server]` extra; nothing in the package itself
> imports it.) This keeps the engine embeddable and its test suite
> installable-free, and makes the server/MCP surfaces strictly additive.

---

## 1. System overview

```
                            ┌───────────────────────────────────────────────┐
                            │                  CLIENTS                        │
                            │   Browser (web/)   CLI (cli.py)   MCP host       │
                            └───────┬───────────────┬───────────────┬─────────┘
                                    │ HTTP + SSE    │ in-process    │ MCP stdio
                                    ▼               │               ▼
        ┌─────────────────────────────────┐        │      ┌──────────────────┐
        │  HTTP API  bookwriter/server/    │        │      │  MCP server      │
        │  api.py  create_app() -> FastAPI │        │      │  mcp_server.py   │
        │   • static frontend at "/"        │       │      │  (tools wrap the │
        │   • JSON API under "/api"          │      │      │   same pipeline) │
        │   • SSE  /api/books/{id}/events     │     │      └────────┬─────────┘
        │   • in-memory event Broker          │     │               │
        │   • background write-job thread      │    │               │
        └──────────────────┬───────────────────┘    │               │
                           │           on_event callbacks            │
                           ▼                         ▼               ▼
        ┌───────────────────────────────────────────────────────────────────┐
        │                       CORE ENGINE  (import-light)                   │
        │                                                                     │
        │   pipeline.BookPipeline(llm, settings, progress, on_event, …)       │
        │        .plan(premise,…) -> Bible      .write_all(resume,only)        │
        │                                                                     │
        │   ┌──────────┐   ┌──────────┐   ┌───────────┐   ┌──────────────┐    │
        │   │ Planner  │──▶│  Writer  │──▶│ Extractor │──▶│  Checker     │    │
        │   │ planner. │   │ writer.  │   │ extractor.│   │  checker.    │    │
        │   └────┬─────┘   └────┬─────┘   └─────┬─────┘   └──────┬───────┘    │
        │        │              │ delta stream  │ state delta    │ flags      │
        │        ▼              ▼               ▼                ▼            │
        │   ┌───────────────────────────────────────────────────────────┐   │
        │   │  StoryGraph (graph.py)  bible · chapters · timeline ·       │   │
        │   │  rolling synopsis · per-chapter SHA fingerprints            │   │
        │   └───────────────────────────────┬───────────────────────────┘   │
        │   CostLedger (costs.py) ◀── every call logged: tokens, cache, $    │
        │        │                                                            │
        └────────┼────────────────────────────────────────────────────────-─┘
                 ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │  BookStore (store.py)  — committed JSON + Markdown on disk         │
        │  <project_dir>/ book.json · state.json · chapters/NN.{json,md} ·   │
        │                  manuscript.md · cost.{txt,json}                   │
        └──────────────────────────────────────────────────────────────────┘

        ┌──────────────────────────────────────────────────────────────────┐
        │  LLM seam — provider.make_llm() selects the backend                │
        │    AnthropicLLM   (llm.py)         Anthropic API, lazy `anthropic` │
        │    OpenAICompatLLM (llm_openai.py) openai · openrouter · grok(xAI) │
        │    ClaudeCliLLM / GenericCliLLM    claude-cli · codex · grok-cli · │
        │      (llm_claude_cli.py, llm_cli.py)  cli — subscription CLIs      │
        │    MockLLM        (mock.py)        offline, no key, simulated      │
        │                                    tokens — demo mode + tests      │
        └──────────────────────────────────────────────────────────────────┘
```

The engine is a four-stage multi-agent pipeline (Planner → Writer → Extractor →
Checker) over a single committed knowledge graph, modeled on Understand-Anything:
build a shared graph of the artifact, update it with deterministic fingerprints,
and hand each downstream worker only a pre-resolved slice of context instead of
the raw material.

---

## 2. Core pipeline (`bookwriter/`)

| Module | Responsibility |
|---|---|
| `config.py` | `Settings`, `QUALITY_PROFILES` (`premium`/`balanced`/`draft`), `DEFAULT_PROFILE="balanced"`, `MODEL_PRICES`, per-stage `StageModel(model, effort, thinking)`. The cost/quality dial lives here. |
| `llm.py` | `LLM` protocol; `AnthropicLLM` (lazy-imports `anthropic`). |
| `llm_openai.py` | `OpenAICompatLLM` — the same `LLM` protocol against any OpenAI-style Chat Completions endpoint; backs the `openai`, `openrouter`, and `grok` (xAI) providers (lazy-imports `openai`). |
| `llm_claude_cli.py` | `ClaudeCliLLM` — shells out to a signed-in `claude -p` (Claude Code), so a Claude subscription generates with no API key. |
| `llm_cli.py` | `GenericCliLLM` — vendor-agnostic subprocess backend behind the `codex`, `grok-cli`, and custom `cli` providers. |
| `provider.py` | The backend switchboard: canonical provider ids + aliases, strong/mid/cheap tier mapping to per-provider model ids, the `make_llm()` factory, and the `live_available()` credential gate. |
| `runtime_config.py` | Persisted setting/credential overlay on top of `os.environ` (`rc.getenv`) — the backing store for the in-app Settings panel. |
| `prompts.py` | System prompts and strict JSON schemas for the structured pipeline stages. |
| `mock.py` | `MockLLM` — offline, deterministic, simulated tokens and placeholder prose. Powers demo mode and the test suite. |
| `planner.py` | Turns a premise into a `Bible` (characters, locations, items, threads, chapter `outline`). |
| `writer.py` | Writes one chapter from a pre-resolved context slice; streams prose deltas. |
| `extractor.py` | Cheap-model pass that emits a structured state delta from finished prose. |
| `checker.py` | Optional cheap-model continuity check; emits flags (does not auto-fix). |
| `graph.py` | `StoryGraph`: `.bible`, `.chapters{n: ChapterRecord}`, `.timeline`, `.synopsis`, `.state_to_dict()`. |
| `models.py` | `Bible` (`.outline`, `.characters`, `.locations`, `.items`, `.threads`), `ChapterPlan`, `ChapterRecord`; `to_dict()/from_dict()`. |
| `costs.py` | `CostLedger` — per-call token + dollar accounting, cache-savings, `$/1k words`. |
| `store.py` | `BookStore(project_dir)`: load/save graph & bible, `has_chapter(n)`, `assemble_manuscript`, `chapter_md(n)`, `book_path`. |
| `pipeline.py` | `BookPipeline` orchestrator — `.plan()`, `.load()`, `.write_all()`, exposes `.graph` and `.ledger`, emits `on_event` dicts. |
| `images.py` | Pluggable chapter-illustration providers (`pixio` default · `openai` · generic `http`), selected by `BOOKWRITER_IMAGE_PROVIDER`. Best-effort: a failed image never blocks prose. |
| `importer.py` | Turns an existing `.txt`/`.md` manuscript into a first-class book: chapter split plus a reverse-engineered bible/continuity graph. |
| `kdp.py` | Amazon KDP packaging: stdlib EPUB builder, AI listing metadata (description, keywords, categories, …), marketing copy, checklist. |
| `print_export.py` | Paperback artifacts, pure stdlib: `.docx` interior, print spec (page count, spine width, wrap dimensions), print-cover SVG. |
| `royalties.py` | Deterministic KDP royalty estimator — eBook 35%/70% plans (delivery fee included) and paperback (printing cost included). |
| `pdf.py` | PDF exports (interior / front cover / back cover / full book) via the optional `[pdf]` extra (reportlab); raises `PdfUnavailable` with an install hint otherwise. |
| `cli.py` / `__main__.py` | `bookwriter` CLI (`profiles`, `plan`, `write`, `generate`, `report`, `import`, `revise`, `continue`, `kdp`, `price`, `cover`, `pdf`). |

### `BookPipeline` surface

```python
BookPipeline(llm, settings, progress=None, on_event=None, stream_prose=False)
  .plan(premise, chapters=None, words_per_chapter=2000,
        title=None, genre=None, extra_guidance="") -> Bible
  .load() -> bool                       # rehydrate an existing project_dir
  .write_all(resume=True, only=None) -> CostLedger
  .graph    # StoryGraph
  .ledger   # CostLedger
```

### `on_event` event types (shared by SSE and MCP progress)

| `type` | Payload |
|---|---|
| `plan_done` | `{title, chapters, characters, bible, cost}` |
| `chapter_start` | `{number, title, act, word_target}` |
| `delta` | `{number, text}` — incremental prose (only when `stream_prose=True`) |
| `chapter_done` | `{number, title, words, text, synopsis, flags, image, fingerprint, cost}` |
| `manuscript_done` | `{words, cost}` |

`cost` snapshot shape:
`{total_cost, words, by_stage, tokens:{input,output,cache_write,cache_read}, cache_savings}`.

---

## 3. The token-cost design (the whole point)

Naïve book generation re-sends every prior chapter into every new call, so cost
grows quadratically. BookwriterPro keeps per-chapter cost roughly **flat**:

1. **Prompt-cache the stable bible.** The full bible is identical on every
   chapter call, so it is sent as a `cache_control` prefix — written once
   (~1.25–2× input), then **read at ~0.1×** on every later chapter.
2. **Bounded rolling synopsis.** Story-so-far is compressed to ~1–2 sentences
   per chapter (`synopsis_line_chars` cap) instead of growing with the book.
3. **Structured deltas, not re-reads.** Continuity updates come from a compact
   JSON delta the cheap model extracts; old prose is never re-read.
4. **Model tiering per stage.** Prose runs on a capable model; the mechanical
   extract/check stages run on the cheapest one.
5. **Measured, not assumed.** Every call is logged to `CostLedger`, which reports
   `$/1k words` and the exact dollars saved by caching.

Quality profiles set the dial explicitly (model IDs and prices are authoritative
in `config.py`):

| Profile | plan | write | extract / check |
|---|---|---|---|
| `premium` | Opus 4.8 (high) | **Opus 4.8** | Haiku 4.5 |
| `balanced` *(default)* | Opus 4.8 (high) | **Sonnet 4.6** | Haiku 4.5 |
| `draft` | Sonnet 4.6 | **Sonnet 4.6** (low effort) | Haiku 4.5 |

---

## 4. HTTP API (`bookwriter/server/api.py`)

`create_app()` returns a `FastAPI` app that serves the vanilla frontend from
`bookwriter/web/` at `/` (with `styles.css`, `app.js`, etc.) and mounts the JSON
API under `/api`. CORS is enabled for localhost.

**Data root.** Books live under a data directory — default
`<repo>/.bookwriter_data` in a source checkout, `~/.bookwriter_data` for a
pip-installed wheel, overridable via the
`BOOKWRITER_DATA_DIR` env var. Each book is `<data>/<id>/` (the `BookStore`
`project_dir`) plus a `meta.json` `{id,title,created_at,profile,mock,genre,logline}`.
The **book id** is `slug(title) + "-" + 6-char hash`.

### Endpoint table (core routes)

This lists the core generation routes. The server also registers `GET /api/providers`,
`GET|PUT /api/settings`, `POST /api/settings/test`, `GET /api/books/{id}/chapters/{n}/image`,
the KDP/publishing routes (`POST|GET /api/books/{id}/kdp`, `/kdp/listing`,
`/export/epub`, `/export/docx`, `/print`, `POST /pricing`, `POST /marketing`) — see
`bookwriter/server/api.py` for the complete set (and `/docs` for live OpenAPI).

| Method | Path | Purpose / notable behavior |
|---|---|---|
| GET | `/api/health` | `{status:"ok", has_api_key:bool}` |
| GET | `/api/profiles` | `{default:"balanced", profiles:[{name, stages:{plan,write,extract,check:{model,effort}}, prices:{model:{input,output}}}]}` |
| GET | `/api/books` | `{books:[BookSummary]}` |
| POST | `/api/books` | Plans **synchronously**. Body `CreateBookRequest{premise(req), chapters?, words_per_chapter=2000, title?, genre?, guidance?, profile="balanced", mock=false, use_cache=true, run_continuity_check=true}` → `{book:BookSummary, bible:<dict>}`. `mock=false` with no credentials for the selected provider → **400** `{detail:"No credentials for LLM provider '<p>'; enable demo mode (mock) or set ANTHROPIC_API_KEY."}` |
| GET | `/api/books/{id}` | `{book:BookSummary, bible:<dict>, chapters:[{number,title,act,written,word_count}], cost:<snapshot|null>}` |
| POST | `/api/books/{id}/write` | Starts a **background** write job (`stream_prose=True`). Body `WriteRequest{only?:int[], restart?:bool}` → `{status:"started"}`. **409** if a job is already running for this id. |
| GET | `/api/books/{id}/events` | **SSE** (`text/event-stream`). Streams `on_event` dicts as JSON `data:` lines, plus a terminal `{type:"done"}` (or `{type:"error",message}`). Late subscribers get a **replay** of events from the current/last job, then tail. Periodic `:\n\n` heartbeat. |
| GET | `/api/books/{id}/chapters/{n}` | `{number,title,text,word_count,synopsis_line,fingerprint,written,plan:<ChapterPlan dict>}` |
| GET | `/api/books/{id}/graph` | `{characters,locations,items,threads,timeline,synopsis}` |
| GET | `/api/books/{id}/cost` | `{snapshot:<cost snapshot>, report:<cost.txt string or "">}` |
| GET | `/api/books/{id}/manuscript` | `{markdown,words}`; `?download=1` → `text/markdown` file response |
| DELETE | `/api/books/{id}` | `{status:"deleted"}` (rmtrees the book dir) |

`BookSummary = {id, title, logline, genre, chapters_total, chapters_written, created_at, profile, mock}`.

---

## 5. SSE streaming & concurrency

```
 POST /write ──▶ start background THREAD ──▶ BookPipeline.write_all(stream_prose=True)
                                                  │ on_event(dict)
                                                  ▼
                                   ┌──────────────────────────────┐
                                   │  Broker (in-memory, per book) │
                                   │   ring buffer  +  subscriber  │
                                   │   asyncio.Queue list          │
                                   └───────┬───────────────┬──────┘
            loop.call_soon_threadsafe(...) │               │ replay then tail
                                           ▼               ▼
                              GET /events (async generator drains a Queue)
                                           │
                                  data: {…json…}\n\n   +   periodic  :\n\n  heartbeat
                                           │
                                  terminal  data:{"type":"done"}  / {"type":"error",…}
```

- **Exactly one running job per book id.** A second `POST /write` while a job runs
  → `409`.
- The write job runs in a **background thread** (the pipeline is synchronous and
  may call the network). Its `on_event` callback pushes into the thread-safe
  **Broker**.
- The Broker keeps a **bounded ring buffer** per book id so late `/events`
  subscribers **replay** what already happened in the current/last job, then tail
  live events via their own `asyncio.Queue`.
- The SSE async generator is fed from the worker thread via
  `loop.call_soon_threadsafe`, emits each event as a JSON `data:` line, sends a
  `:\n\n` comment heartbeat periodically, and closes after the terminal
  `done`/`error` event.

---

## 6. MCP server (`bookwriter/mcp_server.py`)

An optional Model Context Protocol surface exposes the same engine as tools to an
MCP host (e.g. a Claude client). It is the top-level module `bookwriter/mcp_server.py`
(run via `python -m bookwriter.mcp_server`) and depends on the `mcp` package
(`pip install -r requirements-server.txt` or `.[mcp]`), imported **lazily inside
`build_server()`/`main()`** so the core package imports without it. Tools wrap the
identical `BookPipeline` operations used by the HTTP
API — create/plan a book, write chapters, fetch the bible / graph / chapters /
cost / manuscript — so the engine has a single source of truth and the MCP layer
is a thin adapter over the core, never a fork of its logic. Progress is surfaced
through the same `on_event` dicts described in §2.

---

## 7. Frontend (`bookwriter/web/`) — no build step

`index.html` loads `/styles.css` and `/app.js`. It is **vanilla** HTML/CSS/JS
with no bundler or build step. `app.js` talks **only** to the `/api` endpoints
and consumes `/api/books/{id}/events` via the browser `EventSource` API. It must
work with **zero backend changes**.

**Demo mode** (`mock=true`) is a first-class toggle: with `MockLLM` the entire
app — plan, stream a book chapter by chapter, browse the graph, read the cost
report — is fully usable with **no API key and no spend**.

---

## 8. Launching

```bash
# Install the server extra (core needs none of this)
pip install -r requirements-server.txt        # or:  pip install ".[server]"

# Run the web app on 127.0.0.1:8000  (env: BOOKWRITER_HOST / BOOKWRITER_PORT)
python -m bookwriter.serve                     # or the console script:
bookwriter-serve
```

`bookwriter/serve.py:main()` runs uvicorn against `create_app()`. The data root
defaults to `.bookwriter_data` under the project and is overridable with
`BOOKWRITER_DATA_DIR`.

---

## 9. On-disk layout per book

```
<data>/<id>/
  meta.json         {id,title,created_at,profile,mock,genre,logline}
  book.json         the bible (grows as the extractor adds discovered entities)
  state.json        timeline + rolling synopsis
  chapters/NN.json  chapter record (text, word_count, fingerprint, synopsis_line)
  chapters/NN.md    chapter prose (human-readable)
  manuscript.md     assembled full book
  cost.txt / cost.json   cost report for the run
```

Generation is **resumable**: the store saves after every chapter, so an
interrupted run (or a re-subscribed `/events` client) picks up at the next
unwritten chapter.
