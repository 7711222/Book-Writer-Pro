/* ===========================================================================
   Bookwriter Pro — command palette, global keyboard shortcuts, shortcuts help,
   and a skip-to-content link. Self-contained; loaded BEFORE app.js.

   Exposes:
     window.Palette.open()  / .close()  / .toggle()   — the Cmd/Ctrl-K palette
     window.ShortcutsHelp.open() / .close()           — the "?" help modal
     window.BWNav  — small navigation helpers other modules may reuse

   Depends on nothing from app.js at load time. It reaches for app.js globals
   (Router, toast, srStatus) lazily/defensively, so load order can't break it.
   Reads the books list from GET /api/books at open time (with a short cache).
   Honours prefers-reduced-motion and keeps full keyboard accessibility.
   =========================================================================== */
"use strict";

(function () {
  "use strict";

  /* ----------------------------- utilities ------------------------------- */
  const prefersReduced = () =>
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const isMac = () => /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || "");

  const escHtml = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Lazily resolve app.js helpers so this file never hard-fails on load order.
  const announce = (text) => {
    try { if (typeof window.srStatus === "function") return window.srStatus(text); } catch {}
    const el = document.getElementById("sr-status");
    if (el) el.textContent = String(text || "");
  };
  const notify = (msg, opts) => {
    try { if (typeof window.toast === "function") return window.toast(msg, opts); } catch {}
  };

  // Navigate via the app Router when present (so teardown/SSE close cleanly),
  // else fall back to setting location.hash directly.
  function goHash(hash) {
    try {
      if (window.Router && typeof window.Router.go === "function") { window.Router.go(hash); return; }
    } catch {}
    if (location.hash === hash) {
      // Force a resolve if the Router exists but go() wasn't usable.
      try { if (window.Router && window.Router.resolve) window.Router.resolve(); } catch {}
    } else {
      location.hash = hash;
    }
  }

  function toggleTheme() {
    // Reuse the app's theme toggle so pressed-state + persistence stay in sync.
    const btn = document.getElementById("theme-toggle");
    if (btn) { btn.click(); return; }
    // Defensive fallback (should never be needed).
    const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("bw-theme", next); } catch {}
  }

  // Is the user currently typing somewhere we must not hijack keys from?
  function isTypingTarget(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // The id of the currently-open book, parsed from the hash (#/b/<id>...), or null.
  function currentBookId() {
    const raw = (location.hash || "").replace(/^#/, "");
    const parts = raw.split("/").filter(Boolean);
    if (parts[0] === "b" && parts[1]) return parts[1];
    return null;
  }

  /* ----------------------------- book cache ------------------------------ */
  // Reuse app.js state if it ever exposes a books cache; otherwise fetch.
  const BookCache = { list: null, at: 0 };
  async function fetchBooks() {
    // Short TTL so repeat opens are instant but stay fresh after creating a book.
    const fresh = BookCache.list && (Date.now() - BookCache.at < 8000);
    if (fresh) return BookCache.list;
    try {
      const res = await fetch("/api/books");
      if (!res.ok) throw new Error("books " + res.status);
      const data = await res.json();
      BookCache.list = (data && data.books) || [];
      BookCache.at = Date.now();
    } catch {
      // Keep whatever we had; an empty list is a fine fallback.
      if (!BookCache.list) BookCache.list = [];
    }
    return BookCache.list;
  }

  /* --------------------------- fuzzy matcher ----------------------------- */
  // Subsequence fuzzy match. Returns {score, ranges} or null. Higher = better.
  // Rewards consecutive runs and word-boundary starts (a quietly good editorial
  // search feel without a dependency).
  function fuzzy(query, text) {
    const q = query.toLowerCase().trim();
    const t = String(text || "");
    const tl = t.toLowerCase();
    if (!q) return { score: 0, ranges: [] };
    let qi = 0, score = 0, run = 0;
    const ranges = [];
    let prevWasBoundary = true;
    for (let i = 0; i < tl.length && qi < q.length; i++) {
      if (tl[i] === q[qi]) {
        let bonus = 1;
        if (run > 0) bonus += run * 2;                 // consecutive
        const isBoundary = i === 0 || /[\s\-_/.]/.test(tl[i - 1]);
        if (isBoundary || prevWasBoundary) bonus += 3;  // word start
        score += bonus;
        ranges.push(i);
        run++; qi++;
      } else {
        run = 0;
      }
      prevWasBoundary = /[\s\-_/.]/.test(tl[i]);
    }
    if (qi < q.length) return null;                    // not all chars matched
    // Prefer shorter targets and earlier first hit.
    score += Math.max(0, 14 - (ranges[0] || 0));
    score -= Math.max(0, t.length - q.length) * 0.05;
    return { score, ranges };
  }

  // Wrap matched character indices in <mark> for the result label.
  function highlight(text, ranges) {
    const t = String(text || "");
    if (!ranges || !ranges.length) return escHtml(t);
    const set = new Set(ranges);
    let out = "";
    for (let i = 0; i < t.length; i++) {
      const ch = escHtml(t[i]);
      out += set.has(i) ? `<mark>${ch}</mark>` : ch;
    }
    return out;
  }

  /* --------------------------- action sources ---------------------------- */
  // SVG glyphs (16px) for the result rows — same line-art language as the app.
  const ICON = {
    book: '<path d="M5 4h9a3 3 0 0 1 3 3v13H8a3 3 0 0 0-3 3V4z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
    plus: '<path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    theme: '<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.5 5.5l1.5 1.5M17 17l1.5 1.5M18.5 5.5L17 7M7 17l-1.5 1.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    graph: '<circle cx="6" cy="7" r="2.4" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="18" cy="9" r="2.4" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="11" cy="18" r="2.4" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 8l8 1M8 9l3 7M16 11l-4 5" stroke="currentColor" stroke-width="1.4"/>',
    paper: '<path d="M7 3h7l4 4v14H7zM14 3v4h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M9.5 12h6M9.5 15h6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
    library: '<path d="M5 4h3v16H5zM10 4h3v16h-3zM16 5l3 .8-3.5 14L12.5 19z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>',
  };

  // Build the full action list for the current context. `books` is the cached
  // GET /api/books list (BookSummary[]). Static commands first, then books.
  function buildActions(books) {
    const actions = [];
    const bookId = currentBookId();

    actions.push({
      id: "new",
      title: "New book",
      hint: "Begin a manuscript",
      group: "Actions",
      icon: ICON.plus,
      keys: ["n"],
      run: () => goHash("#/new"),
    });

    if (bookId) {
      actions.push(
        { id: "open-studio", title: "Open studio", hint: "This book", group: "This book", icon: ICON.book, run: () => goHash(`#/b/${bookId}`) },
        { id: "open-graph", title: "Open story graph", hint: "This book", group: "This book", icon: ICON.graph, run: () => goHash(`#/b/${bookId}/graph`) },
        { id: "open-manuscript", title: "Open manuscript", hint: "This book", group: "This book", icon: ICON.paper, run: () => goHash(`#/b/${bookId}/manuscript`) }
      );
    }

    actions.push({
      id: "library",
      title: "Back to library",
      hint: "Your shelf",
      group: "Actions",
      icon: ICON.library,
      keys: ["g", "l"],
      run: () => goHash("#/"),
    });

    actions.push({
      id: "theme",
      title: "Toggle theme",
      hint: "Light / dark",
      group: "Actions",
      icon: ICON.theme,
      run: () => toggleTheme(),
    });

    for (const b of (books || [])) {
      const title = b.title || "Untitled";
      actions.push({
        id: "book-" + b.id,
        title,
        hint: b.genre || "Manuscript",
        group: "Books",
        icon: ICON.book,
        // Searchable text includes the genre/logline so a genre query finds books.
        search: `${title} ${b.genre || ""} ${b.logline || ""}`,
        run: () => goHash(`#/b/${b.id}`),
      });
    }
    return actions;
  }

  /* ============================== PALETTE ================================= */
  const Palette = {
    el: null,         // overlay root
    input: null,
    listEl: null,
    actions: [],      // current full action set
    results: [],      // filtered + scored
    activeIndex: 0,
    lastFocused: null,
    open: openPalette,
    close: closePalette,
    toggle() { (Palette.el ? closePalette : openPalette)(); },
  };

  function buildDOM() {
    const overlay = document.createElement("div");
    overlay.className = "cmdk-overlay";
    overlay.id = "cmdk-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Command palette");

    overlay.innerHTML =
      '<div class="cmdk-panel" role="document">' +
        '<div class="cmdk-search">' +
          '<svg class="cmdk-search-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">' +
            '<circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="1.8"/>' +
            '<path d="M16.5 16.5 21 21" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>' +
          '<input id="cmdk-input" type="text" class="cmdk-input" autocomplete="off" autocapitalize="off" ' +
            'spellcheck="false" placeholder="Search books, jump anywhere…" ' +
            'role="combobox" aria-expanded="true" aria-controls="cmdk-list" ' +
            'aria-autocomplete="list" aria-activedescendant="" />' +
          '<kbd class="cmdk-esc">Esc</kbd>' +
        '</div>' +
        '<ul id="cmdk-list" class="cmdk-list" role="listbox" aria-label="Results"></ul>' +
        '<div class="cmdk-foot">' +
          '<span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>' +
          '<span><kbd>↵</kbd> open</span>' +
          '<span><kbd>?</kbd> shortcuts</span>' +
        '</div>' +
      '</div>';

    overlay.addEventListener("mousedown", (e) => {
      // Click-outside (on the dimmed backdrop) closes.
      if (e.target === overlay) closePalette();
    });

    Palette.el = overlay;
    Palette.input = overlay.querySelector("#cmdk-input");
    Palette.listEl = overlay.querySelector("#cmdk-list");

    Palette.input.addEventListener("input", () => { filterAndRender(); });
    Palette.input.addEventListener("keydown", onPaletteKeydown);
    return overlay;
  }

  async function openPalette() {
    if (Palette.el) { Palette.input.focus(); return; }
    Palette.lastFocused = document.activeElement;

    const overlay = buildDOM();
    document.body.appendChild(overlay);
    document.body.classList.add("cmdk-lock");

    // Load actions (books fetched fresh-ish). Render immediately with whatever
    // we have, then refine once the fetch resolves.
    Palette.actions = buildActions(BookCache.list || []);
    filterAndRender();

    const reveal = () => { overlay.classList.add("is-open"); };
    if (prefersReduced()) reveal();
    else requestAnimationFrame(reveal);

    Palette.input.focus();
    announce("Command palette open");

    const books = await fetchBooks();
    if (!Palette.el) return; // closed while loading
    Palette.actions = buildActions(books);
    filterAndRender();
  }

  function closePalette() {
    const overlay = Palette.el;
    if (!overlay) return;
    Palette.el = null;
    // Keep the scroll lock if another locking overlay is still open beneath
    // (help modal, or the Create/Settings/Import dialogs in app.js — the latter
    // all share the .create-overlay root class). Overlays mid-fade keep their
    // root class ~280ms, so skip anything .is-closing.
    if (!document.querySelector(".help-overlay:not(.is-closing), .create-overlay:not(.is-closing)")) {
      document.body.classList.remove("cmdk-lock");
    }

    const finish = () => { overlay.remove(); };
    if (prefersReduced()) finish();
    else {
      overlay.classList.remove("is-open");
      overlay.classList.add("is-closing");
      let done = false;
      const onEnd = () => { if (done) return; done = true; finish(); };
      overlay.addEventListener("transitionend", onEnd, { once: true });
      setTimeout(onEnd, 240); // fallback if transitionend never fires
    }

    // Restore focus to wherever it was before opening.
    const prev = Palette.lastFocused;
    Palette.lastFocused = null;
    if (prev && typeof prev.focus === "function" && document.contains(prev)) {
      try { prev.focus(); } catch {}
    }
  }

  function filterAndRender() {
    const q = (Palette.input.value || "").trim();
    let results;
    if (!q) {
      results = Palette.actions.map((a) => ({ a, ranges: [] }));
    } else {
      results = [];
      for (const a of Palette.actions) {
        // Match against the visible title; allow genre/logline via `search`.
        const m = fuzzy(q, a.title);
        const m2 = a.search ? fuzzy(q, a.search) : null;
        const best = m && (!m2 || m.score >= m2.score) ? m
          : m2 ? { score: m2.score, ranges: [] } : null;
        if (best) results.push({ a, ranges: best.ranges, score: best.score });
      }
      results.sort((x, y) => y.score - x.score);
    }
    Palette.results = results;
    Palette.activeIndex = results.length ? 0 : -1;
    renderList();
  }

  function renderList() {
    const list = Palette.listEl;
    if (!Palette.results.length) {
      list.innerHTML = '<li class="cmdk-empty" role="option" aria-disabled="true">No matches. Try another word.</li>';
      Palette.input.setAttribute("aria-activedescendant", "");
      return;
    }
    let lastGroup = null;
    let html = "";
    Palette.results.forEach((r, i) => {
      const a = r.a;
      if (a.group && a.group !== lastGroup) {
        html += `<li class="cmdk-group" role="presentation">${escHtml(a.group)}</li>`;
        lastGroup = a.group;
      }
      const active = i === Palette.activeIndex;
      const keysHtml = a.keys
        ? `<span class="cmdk-keys">${a.keys.map((k) => `<kbd>${escHtml(k)}</kbd>`).join("")}</span>`
        : "";
      html +=
        `<li id="cmdk-opt-${i}" class="cmdk-opt${active ? " is-active" : ""}" role="option" ` +
          `data-index="${i}" aria-selected="${active ? "true" : "false"}">` +
          `<span class="cmdk-opt-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="18" height="18">${a.icon || ICON.book}</svg></span>` +
          `<span class="cmdk-opt-text"><span class="cmdk-opt-title">${highlight(a.title, r.ranges)}</span>` +
          (a.hint ? `<span class="cmdk-opt-hint">${escHtml(a.hint)}</span>` : "") + `</span>` +
          keysHtml +
        `</li>`;
    });
    list.innerHTML = html;

    // Wire row interactions.
    list.querySelectorAll(".cmdk-opt").forEach((li) => {
      const idx = Number(li.dataset.index);
      li.addEventListener("mousemove", () => setActive(idx, false));
      li.addEventListener("click", () => { setActive(idx, false); invokeActive(); });
    });
    syncActiveDescendant();
  }

  function setActive(i, scroll) {
    if (!Palette.results.length) return;
    const n = Palette.results.length;
    Palette.activeIndex = ((i % n) + n) % n;
    Palette.listEl.querySelectorAll(".cmdk-opt").forEach((li) => {
      const on = Number(li.dataset.index) === Palette.activeIndex;
      li.classList.toggle("is-active", on);
      li.setAttribute("aria-selected", on ? "true" : "false");
      if (on && scroll) li.scrollIntoView({ block: "nearest" });
    });
    syncActiveDescendant();
  }

  function syncActiveDescendant() {
    const id = Palette.activeIndex >= 0 ? `cmdk-opt-${Palette.activeIndex}` : "";
    Palette.input.setAttribute("aria-activedescendant", id);
  }

  function invokeActive() {
    const r = Palette.results[Palette.activeIndex];
    if (!r) return;
    const run = r.a.run;
    closePalette();
    if (typeof run === "function") {
      // Defer so focus restoration settles before the route swap/transition.
      setTimeout(run, 0);
    }
  }

  function onPaletteKeydown(e) {
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); setActive(Palette.activeIndex + 1, true); break;
      case "ArrowUp": e.preventDefault(); setActive(Palette.activeIndex - 1, true); break;
      case "Home": e.preventDefault(); setActive(0, true); break;
      case "End": e.preventDefault(); setActive(Palette.results.length - 1, true); break;
      case "Enter": e.preventDefault(); invokeActive(); break;
      case "Escape": e.preventDefault(); closePalette(); break;
      case "Tab": e.preventDefault(); break; // focus trap: only the input is focusable
    }
  }

  /* =========================== SHORTCUTS HELP ============================= */
  const ShortcutsHelp = {
    el: null,
    lastFocused: null,
    open: openHelp,
    close: closeHelp,
  };

  function cmdLabel() { return isMac() ? "⌘" : "Ctrl"; }

  function openHelp() {
    if (ShortcutsHelp.el) return;
    ShortcutsHelp.lastFocused = document.activeElement;
    const overlay = document.createElement("div");
    overlay.className = "help-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "help-title");

    const rows = [
      [[cmdLabel(), "K"], "Open the command palette"],
      [["N"], "Start a new book"],
      [["G", "then", "L"], "Go to the library"],
      [["?"], "Show this help"],
      [["Esc"], "Close any overlay"],
      [["↑", "↓"], "Move through results"],
      [["↵"], "Open the highlighted result"],
    ];
    const rowHtml = rows.map(([keys, label]) => {
      const kb = keys.map((k) => k === "then"
        ? '<span class="help-then">then</span>'
        : `<kbd>${escHtml(k)}</kbd>`).join(" ");
      return `<div class="help-row"><span class="help-keys">${kb}</span><span class="help-desc">${escHtml(label)}</span></div>`;
    }).join("");

    overlay.innerHTML =
      '<div class="help-panel" role="document">' +
        '<div class="help-head">' +
          '<p class="eyebrow">Keyboard</p>' +
          '<h2 id="help-title" class="serif">Shortcuts</h2>' +
          '<button type="button" class="help-close" aria-label="Close shortcuts">' +
            '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>' +
          '</button>' +
        '</div>' +
        '<div class="help-rows">' + rowHtml + '</div>' +
        '<p class="help-foot">Shortcuts are ignored while you’re typing in a field.</p>' +
      '</div>';

    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeHelp(); });
    overlay.querySelector(".help-close").addEventListener("click", closeHelp);
    overlay.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); closeHelp(); return; }
      if (e.key === "Tab") {
        // Simple focus trap: only the close button is focusable inside.
        e.preventDefault();
        const btn = overlay.querySelector(".help-close");
        if (btn) btn.focus();
      }
    });

    document.body.appendChild(overlay);
    document.body.classList.add("cmdk-lock");
    ShortcutsHelp.el = overlay;
    if (!prefersReduced()) requestAnimationFrame(() => overlay.classList.add("is-open"));
    else overlay.classList.add("is-open");
    overlay.querySelector(".help-close").focus();
    announce("Keyboard shortcuts");
  }

  function closeHelp() {
    const overlay = ShortcutsHelp.el;
    if (!overlay) return;
    ShortcutsHelp.el = null;
    // As in closePalette: release the lock only when nothing else locks scroll.
    if (!Palette.el && !document.querySelector(".create-overlay:not(.is-closing)")) {
      document.body.classList.remove("cmdk-lock");
    }
    const finish = () => overlay.remove();
    if (prefersReduced()) finish();
    else {
      // Tag the fade-out so the other overlays' lock-release checks (which all
      // skip .is-closing) don't count this node as "still open".
      overlay.classList.remove("is-open");
      overlay.classList.add("is-closing");
      let done = false;
      const onEnd = () => { if (done) return; done = true; finish(); };
      overlay.addEventListener("transitionend", onEnd, { once: true });
      setTimeout(onEnd, 240);
    }
    const prev = ShortcutsHelp.lastFocused;
    ShortcutsHelp.lastFocused = null;
    if (prev && typeof prev.focus === "function" && document.contains(prev)) {
      try { prev.focus(); } catch {}
    }
  }

  function anyOverlayOpen() { return !!(Palette.el || ShortcutsHelp.el); }

  /* =========================== GLOBAL KEYMAP ============================= */
  // Chord state for "g" then "l".
  let chordPending = null;
  let chordTimer = 0;
  function clearChord() { chordPending = null; if (chordTimer) { clearTimeout(chordTimer); chordTimer = 0; } }

  function onGlobalKeydown(e) {
    // Cmd/Ctrl-K works everywhere (even while typing) — it's the escape hatch.
    const key = (e.key || "").toLowerCase();
    if ((e.metaKey || e.ctrlKey) && key === "k" && !e.altKey) {
      e.preventDefault();
      if (ShortcutsHelp.el) closeHelp();
      Palette.toggle();
      return;
    }

    // Esc closes the topmost overlay (help sits above palette if both somehow open).
    if (e.key === "Escape" && anyOverlayOpen()) {
      // Each overlay also handles its own Escape when focused; this is the
      // belt-and-suspenders global handler for focus that drifted.
      if (ShortcutsHelp.el) { e.preventDefault(); closeHelp(); return; }
      if (Palette.el) { e.preventDefault(); closePalette(); return; }
    }

    // Don't run single-key shortcuts while an overlay owns the keyboard, while
    // typing, or with a modifier held.
    if (anyOverlayOpen()) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (isTypingTarget(e.target)) { clearChord(); return; }

    // Chord: "g" then "l" -> library.
    if (chordPending === "g") {
      clearChord();
      if (key === "l") { e.preventDefault(); goHash("#/"); return; }
      // any other key cancels the chord; fall through to normal handling
    }

    if (key === "g") {
      chordPending = "g";
      chordTimer = setTimeout(clearChord, 900);
      return;
    }
    if (key === "n") { e.preventDefault(); goHash("#/new"); return; }
    if (key === "?" || (e.key === "/" && e.shiftKey)) { e.preventDefault(); openHelp(); return; }
  }

  /* ========================= VIEW TRANSITIONS ============================ */
  // Wrap a DOM-swapping callback in document.startViewTransition where supported,
  // with a clean no-op fallback and no animation under reduced-motion.
  // app.js calls window.BWNav.transition(fn) around its view render.
  function transition(swap) {
    if (typeof swap !== "function") return;
    if (prefersReduced() || !document.startViewTransition) { swap(); return; }
    try {
      document.documentElement.classList.add("vt-active");
      const vt = document.startViewTransition(() => { swap(); });
      const cleanup = () => document.documentElement.classList.remove("vt-active");
      if (vt && vt.finished && typeof vt.finished.then === "function") {
        vt.finished.then(cleanup, cleanup);
      } else {
        setTimeout(cleanup, 400);
      }
    } catch {
      swap(); // any failure -> plain swap
    }
  }

  /* ============================ SKIP LINK ================================ */
  function installSkipLink() {
    if (document.querySelector(".skip-link")) return;
    const a = document.createElement("a");
    a.className = "skip-link";
    a.href = "#app";
    a.textContent = "Skip to main content";
    a.addEventListener("click", (e) => {
      const main = document.getElementById("app");
      if (main) {
        e.preventDefault();
        main.setAttribute("tabindex", "-1");
        main.focus();
        main.scrollIntoView();
      }
    });
    document.body.insertBefore(a, document.body.firstChild);
  }

  /* ============================== INSTALL ================================ */
  function install() {
    installSkipLink();
    document.addEventListener("keydown", onGlobalKeydown, true);
    // After any successful book creation, the library may change — drop the
    // cache so the next palette open re-fetches. (hashchange is a good proxy.)
    window.addEventListener("hashchange", () => { BookCache.at = 0; });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install);
  } else {
    install();
  }

  // Public surface.
  window.Palette = Palette;
  window.ShortcutsHelp = ShortcutsHelp;
  window.BWNav = { transition, goHash, toggleTheme, openHelp, openPalette };
})();
