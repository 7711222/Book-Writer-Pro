"""Command-line interface.

    python -m bookwriter plan      --premise "..." --chapters 12 --project ./mybook
    python -m bookwriter write     --project ./mybook
    python -m bookwriter generate  --premise-file premise.txt --project ./mybook
    python -m bookwriter report    --project ./mybook
    python -m bookwriter kdp       --project ./mybook --author-first A --author-last B
    python -m bookwriter price     --project ./mybook --list-price 4.99
    python -m bookwriter profiles

Add ``--mock`` to any generating command to run the whole pipeline offline
(no API key, simulated tokens) — useful for trying the flow and seeing the cost
report shape before spending anything.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Optional

from .config import Settings, QUALITY_PROFILES, MODEL_PRICES
from .llm import LLM
from .pipeline import BookPipeline
from .store import BookStore

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def load_env_file(path: Optional[str] = None) -> int:
    """Load ``KEY=VALUE`` lines from a .env file into ``os.environ``.

    The file is *path* if given, else ``$BOOKWRITER_ENV_FILE``, else ``./.env``
    in the current directory; a missing file is a silent no-op. The parser is
    deliberately tiny (stdlib only): blank lines and ``#`` comments are
    skipped, an optional ``export `` prefix is dropped, values may be single-
    or double-quoted, and unquoted values may carry a trailing ``# comment``.
    Variables already present in the environment are NEVER overridden — what
    you export in your shell always wins. Returns how many variables were set.
    """
    path = path or os.environ.get("BOOKWRITER_ENV_FILE") or ".env"
    try:
        # utf-8-sig: tolerate the UTF-8 BOM that Notepad/PowerShell often add,
        # which would otherwise glue U+FEFF onto the first key and skip it.
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return 0
    loaded = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            # quoted: take everything up to the matching quote, verbatim
            closing = value.find(value[0], 1)
            if closing != -1:
                value = value[1:closing]
        else:
            # unquoted: allow a trailing inline comment ("KEY=val  # note")
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _make_llm(args) -> LLM:
    from .provider import make_llm
    return make_llm(mock=args.mock)


def _make_settings(args) -> Settings:
    s = Settings(project_dir=args.project).with_profile(args.profile)
    if getattr(args, "no_cache", False):
        s.use_cache = False
    if getattr(args, "no_check", False):
        s.run_continuity_check = False
    return s


def _parse_only(value: Optional[str]) -> Optional[List[int]]:
    if not value:
        return None
    out: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                lo, hi = int(a), int(b)
                if lo > hi:                 # normalize reversed ranges (3-1 -> 1-3)
                    lo, hi = hi, lo
                out.extend(range(lo, hi + 1))
            else:
                out.append(int(part))
        except ValueError:
            raise ValueError(
                f"invalid --only selection {part!r}; use e.g. '1,3,5' or '2-7'"
            )
    return out or None


def _read_premise(args) -> str:
    if args.premise_file:
        with open(args.premise_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    if args.premise:
        return args.premise
    raise SystemExit("error: provide --premise or --premise-file")


def cmd_plan(args) -> int:
    settings = _make_settings(args)
    pipe = BookPipeline(_make_llm(args), settings, progress=print)
    pipe.plan(
        premise=_read_premise(args), chapters=args.chapters,
        words_per_chapter=args.words, title=args.title, genre=args.genre,
        extra_guidance=args.guidance or "",
    )
    print(f"\nPlan saved to {os.path.join(args.project, 'book.json')}")
    print(pipe.ledger.report())
    return 0


def cmd_write(args) -> int:
    settings = _make_settings(args)
    pipe = BookPipeline(_make_llm(args), settings, progress=print)
    if not pipe.load():
        raise SystemExit("error: no plan found in project; run 'plan' first")
    pipe.write_all(resume=not args.restart, only=_parse_only(args.only))
    print("\n" + pipe.ledger.report())
    return 0


def cmd_generate(args) -> int:
    settings = _make_settings(args)
    pipe = BookPipeline(_make_llm(args), settings, progress=print)
    pipe.plan(
        premise=_read_premise(args), chapters=args.chapters,
        words_per_chapter=args.words, title=args.title, genre=args.genre,
        extra_guidance=args.guidance or "",
    )
    pipe.write_all(resume=True, only=_parse_only(args.only))
    print("\n" + pipe.ledger.report())
    return 0


def cmd_report(args) -> int:
    store = BookStore(args.project)
    path = os.path.join(args.project, "cost.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            print(f.read())
    else:
        print("No cost report yet. Run a generation first.")
    graph = store.load_graph()
    if graph:
        written = len(graph.chapters)
        total = len(graph.bible.outline)
        print(f"\nProgress: {written}/{total} chapters written.")
    return 0


def cmd_kdp(args) -> int:
    from .costs import CostLedger
    from .kdp import generate_kdp_metadata, generate_marketing, build_kdp_kit

    settings = _make_settings(args)
    store = BookStore(args.project)
    graph = store.load_graph()
    if graph is None:
        raise SystemExit("error: no plan found in project; run 'generate' first")
    if not graph.chapters:
        print("warning: no chapters written yet; the EPUB will be empty. "
              "Run 'write' first for a complete kit.")

    ledger = CostLedger()
    llm = _make_llm(args)
    meta = generate_kdp_metadata(
        llm, settings, ledger, graph,
        author_first=args.author_first,
        author_last=args.author_last,
        language=args.language or "English",
        subtitle=args.subtitle or "",
        series=args.series or "",
        edition=args.edition or "",
    )
    # Marketing copy (blurbs / A+ modules / bio / taglines) for the kit. Skip
    # only if explicitly disabled; it shares the same LLM/ledger as the metadata.
    marketing = None
    if not getattr(args, "no_marketing", False):
        marketing = generate_marketing(llm, settings, ledger, graph, meta)

    out_dir = os.path.join(args.project, "kdp")
    kit = build_kdp_kit(
        graph, meta, out_dir,
        trim=(6.0, 9.0), paper=args.paper or "white",
        marketing=marketing,
    )

    print(f"\nKDP kit written to {out_dir}")
    print(f"  Title:      {meta.full_title()}")
    print(f"  Author:     {meta.author_full()}")
    print(f"  Keywords:   {len(meta.keywords)} / 7")
    print(f"  Categories: {len(meta.categories)} / 3")
    print(f"  EPUB:       {kit['paths']['epub']}")
    print(f"  Interior:   {kit['paths']['docx']}")
    print(f"  Print spec: {kit['paths']['print_spec']}")
    print(f"  Print cvr:  {kit['paths']['print_cover']}")
    if "marketing" in kit["paths"]:
        print(f"  Marketing:  {kit['paths']['marketing']}")
    print(f"  Listing:    {kit['paths']['listing']}")
    print(f"  Checklist:  {kit['paths']['checklist']}")
    spec = kit.get("print_spec", {})
    if spec:
        print(f"  Pages ~{spec.get('page_count_estimate')}, "
              f"spine {spec.get('spine_width_in')}in "
              f"({spec.get('paper')} paper)")
    return 0


def cmd_price(args) -> int:
    from .royalties import estimate_page_count, estimate_royalties

    store = BookStore(args.project)
    graph = store.load_graph()
    if graph is None:
        raise SystemExit("error: no plan found in project; run 'generate' first")

    pages = estimate_page_count(graph)
    # Use the real built EPUB's size for the delivery-fee math when one
    # exists (from a prior `kdp` run); else keep the 1.0 MB default.
    ebook_file_mb = 1.0
    epub = os.path.join(args.project, "kdp", "manuscript.epub")
    if os.path.isfile(epub):
        ebook_file_mb = os.path.getsize(epub) / 1e6
    est = estimate_royalties(
        list_price=args.list_price,
        marketplace=args.marketplace or "US",
        page_count=pages,
        paper=args.paper or "white",
        ebook_file_mb=ebook_file_mb,
    )
    eb = est["ebook"]
    pb = est["paperback"]
    cur = "$"
    print(f"Royalty estimate for {args.project} "
          f"(list ${args.list_price:.2f}, {args.marketplace}, {args.paper} paper)\n")
    print(f"  EBOOK     plan {eb['plan']:<4} "
          f"royalty/sale {cur}{eb['royalty_per_sale']:.2f} "
          f"(delivery fee {cur}{eb['delivery_fee']:.2f})")
    alt = eb.get("alternate_plan", {})
    if alt:
        elig = "" if alt.get("eligible", True) else " [ineligible]"
        print(f"            alt {alt.get('plan',''):<4} "
              f"royalty/sale {cur}{alt.get('royalty_per_sale', 0):.2f}{elig}")
    print(f"  PAPERBACK ~{pb['page_count']} pages, "
          f"print cost {cur}{pb['printing_cost']:.2f}, "
          f"royalty/sale {cur}{pb['royalty_per_sale']:.2f}"
          f"{'  [below print cost!]' if pb['below_cost'] else ''}")
    print("\nAssumptions:")
    for a in est["assumptions"]:
        print(f"  - {a}")
    print(f"\n({est['note']})")
    return 0


def _cli_kdp_meta(project: str, graph):
    """Saved KDP metadata (from a prior `kdp` run) or a minimal one (no LLM)."""
    import json as _json
    from .kdp import KdpMetadata
    mj = os.path.join(project, "kdp", "metadata.json")
    if os.path.isfile(mj):
        with open(mj, encoding="utf-8") as f:
            return KdpMetadata.from_dict(_json.load(f))
    return KdpMetadata(title=graph.bible.title or "Untitled", author_first="", author_last="")


def _cli_cover_art(project: str):
    d = os.path.join(project, "kdp")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.startswith("cover-art."):
                with open(os.path.join(d, name), "rb") as f:
                    return f.read(), name.rsplit(".", 1)[-1].lower()
    return None, None


def cmd_cover(args) -> int:
    from .images import image_available, generate_cover_art
    from .kdp import compose_cover_svg

    store = BookStore(args.project)
    graph = store.load_graph()
    if graph is None:
        raise SystemExit("error: no plan found in project; run 'generate' first")
    if not image_available():
        raise SystemExit("error: no image backend configured — set PIXIO_API_KEY "
                         "(or BOOKWRITER_IMAGE_PROVIDER) to generate an AI cover")
    art, ext = generate_cover_art(graph.bible)
    ext = (ext or "png").lower().lstrip(".")
    d = os.path.join(args.project, "kdp")
    os.makedirs(d, exist_ok=True)
    for name in list(os.listdir(d)):
        if name.startswith("cover-art."):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
    art_path = os.path.join(d, f"cover-art.{ext}")
    with open(art_path, "wb") as f:
        f.write(art)
    meta = _cli_kdp_meta(args.project, graph)
    svg_path = os.path.join(d, "cover.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(compose_cover_svg(meta, art, ext))
    print(f"AI cover generated:\n  art:   {art_path}\n  cover: {svg_path}")
    return 0


def cmd_pdf(args) -> int:
    from . import pdf as _pdf

    store = BookStore(args.project)
    graph = store.load_graph()
    if graph is None:
        raise SystemExit("error: no plan found in project; run 'generate' first")
    if not _pdf.pdf_available():
        raise SystemExit("error: " + _pdf._INSTALL_HINT)
    part = args.part
    if part in ("interior", "full") and not graph.chapters:
        raise SystemExit("error: no chapters written; run 'write' first")
    meta = _cli_kdp_meta(args.project, graph)
    art, ext = _cli_cover_art(args.project)
    data = _pdf.build_pdf(part, graph, meta, art_bytes=art, ext=ext or "png")
    out = args.out or os.path.join(args.project, "kdp", f"{part}.pdf")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    print(f"PDF ({part}) written to {out}  ({len(data):,} bytes)")
    return 0


def cmd_import(args) -> int:
    from .importer import build_graph_from_text
    from .costs import CostLedger
    from .provider import live_available

    try:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise SystemExit(f"error: cannot read {args.file!r}: {e}")
    if not text.strip():
        raise SystemExit("error: the file is empty")

    settings = _make_settings(args)
    store = BookStore(args.project)
    # Analyze (reverse-engineer the bible) when a model is available; otherwise
    # do a structure-only import so it never fails.
    analyze = not getattr(args, "no_analyze", False)
    if args.mock:
        llm = _make_llm(args)
    elif live_available():
        llm = _make_llm(args)
    else:
        llm, analyze = None, False
    graph = build_graph_from_text(
        llm, settings, CostLedger(), text=text,
        title=args.title or None, genre=args.genre or None,
        analyze=analyze, run_extract=analyze,
    )
    store.save_graph(graph)
    for rec in graph.chapters.values():
        store.save_chapter(rec)
    store.assemble_manuscript(graph)
    print(f"Imported {len(graph.chapters)} chapter(s) into {args.project}")
    print(f"  Title: {graph.bible.title}")
    print("  Now: `bookwriter report` / `write` to extend, or `kdp` to publish.")
    return 0


def cmd_revise(args) -> int:
    from .writer import revise_chapter
    from .costs import CostLedger

    store = BookStore(args.project)
    graph = store.load_graph()
    if graph is None:
        raise SystemExit("error: no plan found in project; run 'generate' or 'import' first")
    plan = graph.bible.plan(args.chapter)
    rec = graph.chapters.get(args.chapter)
    if plan is None or rec is None:
        raise SystemExit(f"error: chapter {args.chapter} hasn't been written yet")
    settings = _make_settings(args)
    new_rec = revise_chapter(_make_llm(args), settings, CostLedger(), graph, plan,
                             rec.text, instructions=args.instructions or "")
    graph.chapters[args.chapter] = new_rec
    graph.record_chapter(new_rec, new_rec.synopsis_line, settings.synopsis_line_chars)
    store.save_chapter(new_rec)
    store.save_graph(graph)
    store.assemble_manuscript(graph)
    print(f"Revised chapter {args.chapter}: {new_rec.title} ({new_rec.word_count} words)")
    return 0


def cmd_continue(args) -> int:
    from .planner import extend_outline
    from .costs import CostLedger

    store = BookStore(args.project)
    graph = store.load_graph()
    if graph is None:
        raise SystemExit("error: no plan found in project; run 'generate' or 'import' first")
    settings = _make_settings(args)
    new_plans = extend_outline(_make_llm(args), settings, CostLedger(), graph,
                               count=args.count, guidance=args.guidance or "")
    graph.bible.outline.extend(new_plans)
    graph.bible.target_chapters = len(graph.bible.outline)
    store.save_graph(graph)
    print(f"Added {len(new_plans)} chapter(s) to the outline:")
    for p in new_plans:
        print(f"  {p.number}. {p.title}")
    print("Now run `bookwriter write` to generate them.")
    return 0


def cmd_profiles(_args) -> int:
    print("Quality profiles (stage -> model):\n")
    for name, p in QUALITY_PROFILES.items():
        print(f"  {name}")
        for stage in ("plan", "write", "extract", "check"):
            sm = getattr(p, stage)
            price = MODEL_PRICES.get(sm.model)
            cost = f"${price.input}/{price.output} per 1M" if price else "?"
            print(f"    {stage:<8} {sm.model:<20} effort={sm.effort:<7} ({cost})")
        print()
    print("Pricing note: cache reads cost ~0.1x input; the bible prefix is cached,")
    print("so per-chapter context is paid at ~10% after the first chapter.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bookwriter", description="Token-cost-optimized book generator.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp, *, generating=True):
        sp.add_argument("--project", default="./book", help="project directory (default ./book)")
        if generating:
            sp.add_argument("--profile", default="balanced", choices=list(QUALITY_PROFILES))
            sp.add_argument("--mock", action="store_true", help="run offline with a mock model")
            sp.add_argument("--no-cache", action="store_true", help="disable prompt caching of the bible")
            sp.add_argument("--no-check", action="store_true", help="skip the continuity-check stage")

    def add_plan_args(sp):
        sp.add_argument("--premise", help="one-line or paragraph premise")
        sp.add_argument("--premise-file", help="read premise from a file")
        sp.add_argument("--chapters", type=int, default=None, help="number of chapters")
        sp.add_argument("--words", type=int, default=2000, help="target words per chapter")
        sp.add_argument("--title", default=None)
        sp.add_argument("--genre", default=None)
        sp.add_argument("--guidance", default=None, help="extra planning guidance")

    sp = sub.add_parser("plan", help="design the bible + outline")
    add_common(sp); add_plan_args(sp); sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("write", help="write chapters from an existing plan")
    add_common(sp)
    sp.add_argument("--only", help="chapter selection, e.g. '1,3,5-7'")
    sp.add_argument("--restart", action="store_true", help="rewrite even already-written chapters")
    sp.set_defaults(func=cmd_write)

    sp = sub.add_parser("generate", help="plan then write the whole book")
    add_common(sp); add_plan_args(sp)
    sp.add_argument("--only", help="restrict writing to a chapter selection")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("report", help="show the latest cost report + progress")
    add_common(sp, generating=False); sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("kdp", help="build the Amazon KDP upload kit (metadata + EPUB)")
    add_common(sp)
    sp.add_argument("--author-first", required=True, help="primary author first name (pen names OK)")
    sp.add_argument("--author-last", required=True, help="primary author last name")
    sp.add_argument("--subtitle", default="", help="optional subtitle (stored separately; KDP adds the colon)")
    sp.add_argument("--series", default="", help="optional series name")
    sp.add_argument("--edition", default="", help="optional edition number")
    sp.add_argument("--language", default="English", help="book language (default English)")
    sp.add_argument("--paper", default="white", choices=["white", "cream"],
                    help="paperback paper stock for print spec/cover (default white)")
    sp.add_argument("--no-marketing", action="store_true",
                    help="skip generating marketing copy (blurbs/A+/bio/taglines)")
    sp.set_defaults(func=cmd_kdp)

    sp = sub.add_parser("price", help="estimate ebook + paperback KDP royalties for a list price")
    add_common(sp, generating=False)
    sp.add_argument("--list-price", type=float, required=True,
                    help="retail list price, e.g. 4.99")
    sp.add_argument("--marketplace", default="US", help="marketplace code (default US)")
    sp.add_argument("--paper", default="white", choices=["white", "cream"],
                    help="paperback paper stock (default white)")
    sp.set_defaults(func=cmd_price)

    sp = sub.add_parser("cover", help="generate a catchy AI cover (art + typography) via the image backend")
    add_common(sp, generating=False)
    sp.set_defaults(func=cmd_cover)

    sp = sub.add_parser("pdf", help="export the book as a PDF (interior / front-cover / back-cover / full)")
    add_common(sp, generating=False)
    sp.add_argument("--part", default="full",
                    choices=["interior", "front-cover", "back-cover", "full"],
                    help="which PDF to build (default full)")
    sp.add_argument("--out", default="", help="output path (default <project>/kdp/<part>.pdf)")
    sp.set_defaults(func=cmd_pdf)

    sp = sub.add_parser("import", help="import a pre-written manuscript (.txt/.md) into a book")
    add_common(sp)
    sp.add_argument("--file", required=True, help="path to the manuscript text/markdown file")
    sp.add_argument("--title", default=None, help="book title (else inferred)")
    sp.add_argument("--genre", default=None, help="genre hint")
    sp.add_argument("--no-analyze", action="store_true",
                    help="structure-only import (skip reverse-engineering the bible)")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("revise", help="AI-revise an existing chapter")
    add_common(sp)
    sp.add_argument("--chapter", type=int, required=True, help="chapter number to revise")
    sp.add_argument("--instructions", default="", help="how to revise (default: polish)")
    sp.set_defaults(func=cmd_revise)

    sp = sub.add_parser("continue", help="propose & append more chapters (then `write`)")
    add_common(sp)
    sp.add_argument("--count", type=int, default=3, help="how many chapters to add")
    sp.add_argument("--guidance", default="", help="direction for where the story goes next")
    sp.set_defaults(func=cmd_continue)

    sp = sub.add_parser("profiles", help="list quality profiles and pricing")
    sp.set_defaults(func=cmd_profiles)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    load_env_file()  # ./.env (or BOOKWRITER_ENV_FILE); shell exports always win
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except SystemExit:
        raise  # argparse / explicit exits already carry a code
    except Exception as e:  # noqa: BLE001 - turn any failure into a tidy non-zero exit
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
