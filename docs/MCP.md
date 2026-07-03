# BookwriterPro — MCP Server

BookwriterPro ships a **Model Context Protocol (MCP)** server so an AI agent
(Claude Desktop, Claude Code, or any MCP client) can plan and write full-length
books as a tool. It speaks the **stdio** transport and reuses the same
application layer (`bookwriter.server.service.BookService`) and the same on-disk
data directory as the HTTP/web UI — so books you create from the agent show up
in the web app, and vice versa.

## What it does

The server exposes twenty-four tools. Every tool returns plain JSON. Use `mock=True`
to run **fully offline with no API key** (deterministic placeholder prose) —
ideal for trying the tools before spending tokens.

| Tool | Purpose |
| --- | --- |
| `list_profiles()` | Quality profiles (premium/balanced/draft): which Claude model runs each stage + per-model prices. |
| `list_books()` | Every book in the shared data dir, with progress. |
| `create_book(premise, chapters=12, words_per_chapter=2000, title="", genre="", guidance="", profile="balanced", provider="", model="", chapter_images=False, mock=False)` | Plan a book **synchronously** (bible + characters + chapter outline). `guidance` adds planning instructions; `provider`/`model` pick a per-book LLM backend; `chapter_images` opts into per-chapter illustrations. Returns the new book `id` and the full planned `bible`. |
| `import_book(text, title="", genre="", guidance="", profile="balanced", analyze=True, mock=False)` | Import pre-written material: split into chapters, reverse-engineer the bible + continuity, record every chapter. With `analyze=True` but no credentials it degrades to a structure-only import. Returns the new book `id` + `bible`. |
| `edit_chapter(book_id, number, text, title="", reextract=False)` | Replace a chapter's prose (manual edit; no model). `reextract=True` re-runs continuity over the new text. |
| `revise_chapter(book_id, number, instructions="")` | AI-revise an existing chapter per instructions (or polish), consistent with the bible. |
| `add_chapters(book_id, count=3, guidance="")` | Continue the story: propose & append N new outline chapters (then `write_book`). |
| `write_book(book_id, only=None, restart=False)` | Write chapters **synchronously**; resumes from the last unwritten chapter (`restart=True` re-runs from chapter 1). Returns `chapters_written`, `cost`, `flags`. |
| `write_chapter(book_id, number)` | Write/rewrite a single chapter (sugar for `write_book(only=[number])`). |
| `get_status(book_id)` | `{chapters_total, chapters_written}`. |
| `get_chapter(book_id, number)` | A chapter's prose + metadata + its plan. |
| `get_graph(book_id)` | The continuity graph: characters, locations, items, threads, timeline, rolling synopsis. |
| `get_cost(book_id)` | Token-cost snapshot + human-readable report for the last write run. |
| `get_manuscript(book_id)` | The assembled full book as Markdown. |
| `prepare_kdp(book_id, author_first, author_last, ...)` | Generate KDP listing metadata + build the upload kit (EPUB, cover, listing, checklist) into `<book>/kdp/`. |
| `export_epub(book_id)` | Path to the KDP-ready EPUB (builds it on demand when `prepare_kdp` hasn't run; the book must be written). |
| `export_docx(book_id)` | Path to the print-ready 6×9 DOCX interior. |
| `print_spec(book_id, paper="white")` | Paperback print spec: estimated page count, spine width, full-wrap cover dimensions. |
| `estimate_royalties(book_id, list_price, marketplace="US", paper="white")` | Estimated eBook + paperback royalties per sale. |
| `generate_marketing(book_id)` | Back-cover blurbs, A+ modules, author bio, taglines (mock mode inherited from the book). |
| `get_kdp_listing(book_id)` | The copy-paste KDP listing text for an already-prepared kit. |
| `generate_cover(book_id, title="", subtitle="", author_first="", author_last="")` | Generate a catchy AI cover (artwork via the image backend + title/author typography). |
| `generate_back_cover(book_id)` | Render the back cover (blurb + author bio + imprint + barcode area). |
| `export_pdf(book_id, part="full")` | Export a PDF — `interior`, `front-cover`, `back-cover`, or `full` (needs the `[pdf]` extra). |

A typical agent flow:

1. `create_book(premise="...", chapters=12, mock=true)` → grab the returned `id`.
2. `write_book(book_id=id)` → blocks until all chapters are written, returns cost + flags.
3. `get_manuscript(book_id=id)` → the finished book in Markdown.

## Requirements

- Python 3.10+ (same as the rest of the project).
- The `mcp` package: `pip install mcp`. (The module imports `mcp` lazily, so it
  still imports without it; `python -m bookwriter.mcp_server` will print an
  install hint and exit if `mcp` is missing.)
- For **live** generation (not `mock`): credentials for whichever
  `BOOKWRITER_LLM_PROVIDER` you use — e.g. `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROK_API_KEY`, or a signed-in CLI
  (see `.env.example`). `mock=True` needs none.

## Run it (stdio)

```bash
# from the project root (your BookwritterPro checkout)
python -m bookwriter.mcp_server        # use python3 on macOS/Linux if needed
```

The server communicates over stdin/stdout — there is no banner; an MCP client
drives it. To sanity-check it starts, run the command and confirm it does not
exit (Ctrl-C to stop), or wire it into a client using the config below.

### Data directory (shared with the web app)

Books live under `<DATA>/<book_id>/`. The default is `.bookwriter_data` inside
the project root for a source checkout (`~/.bookwriter_data` for a pip-installed
package). Override it with the `BOOKWRITER_DATA_DIR` environment variable.
**Point the MCP server and the HTTP server at the same `BOOKWRITER_DATA_DIR`**
(or leave both at the default) so they share state.

## Claude Desktop configuration

Edit Claude Desktop's config file:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add (or merge) the `bookwriter` entry, replacing `<path-to>/BookwritterPro`
with your checkout (on Windows use double backslashes, e.g.
`C:\\path\\to\\BookwritterPro`):

```json
{
  "mcpServers": {
    "bookwriter": {
      "command": "python",
      "args": ["-m", "bookwriter.mcp_server"],
      "cwd": "<path-to>/BookwritterPro",
      "env": {
        "BOOKWRITER_DATA_DIR": "<path-to>/BookwritterPro/.bookwriter_data",
        "ANTHROPIC_API_KEY": "sk-ant-...replace-or-omit-for-mock-only..."
      }
    }
  }
}
```

Notes:
- `command` is your Python interpreter — `python3` on macOS/Linux, or the full
  path to `python.exe` on Windows if `python` isn't on the PATH Claude Desktop
  sees (GUI apps don't always inherit your shell PATH).
- `cwd` must be the project root so `python -m bookwriter.mcp_server` resolves
  the `bookwriter` package.
- Set the key for the provider you use (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`, `GROK_API_KEY`, ... plus `BOOKWRITER_LLM_PROVIDER` if
  not Anthropic) — or omit keys entirely if you only ever call tools with
  `mock=True`.
- Restart Claude Desktop after editing the config. The `bookwriter` tools then
  appear in the tools menu.

## Claude Code configuration

Claude Code reads MCP servers from `.mcp.json` (project scope) or your user
config. The fastest path is the CLI:

```bash
# replace <path-to>/BookwritterPro with your checkout; python3 on macOS/Linux
claude mcp add bookwriter \
  --scope user \
  --env BOOKWRITER_DATA_DIR=<path-to>/BookwritterPro/.bookwriter_data \
  -- python -m bookwriter.mcp_server
```

Or add it by hand to a project-root `.mcp.json` (same schema as Claude Desktop;
on Windows use double backslashes in the JSON paths):

```json
{
  "mcpServers": {
    "bookwriter": {
      "command": "python",
      "args": ["-m", "bookwriter.mcp_server"],
      "cwd": "<path-to>/BookwritterPro",
      "env": {
        "BOOKWRITER_DATA_DIR": "<path-to>/BookwritterPro/.bookwriter_data"
      }
    }
  }
}
```

Then run `claude` from the project directory and the tools are available.

## Troubleshooting

- **"The 'mcp' package is required…"** — `pip install mcp` into the same
  interpreter named in `command`.
- **`create_book` returns `{"error": "No credentials for LLM provider '...'; ..."}`** — set
  the key in the `env` block, or call with `mock=True`.
- **Tools don't appear** — verify `command` points at the right Python and `cwd`
  is the project root; check the client's MCP logs.
- **Books from the agent aren't in the web app (or vice versa)** — make sure both
  use the same `BOOKWRITER_DATA_DIR`.
