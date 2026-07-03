/* ===========================================================================
   Bookwriter Pro — frontend app (vanilla JS, no build).
   Talks only to /api/* and consumes /api/books/{id}/events via EventSource.
   Hash routing: #/ (library), #/new, #/b/<id> (studio),
                 #/b/<id>/graph, #/b/<id>/manuscript
   =========================================================================== */
"use strict";

/* --------------------------------- API ---------------------------------- */
const API = {
  async _json(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch("/api" + path, opts);
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch { data = { raw: text }; } }
    if (!res.ok) {
      let detail = data && (data.detail || data.message);
      // FastAPI 422 bodies can carry detail as an array of error objects; flatten
      // to a readable string so the toast never shows "[object Object]".
      if (Array.isArray(detail)) {
        detail = detail.map((d) => (d && d.msg) ? d.msg : (typeof d === "string" ? d : JSON.stringify(d))).join("; ");
      }
      const msg = detail || `Request failed (${res.status})`;
      const err = new Error(msg); err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  },
  health: () => API._json("GET", "/health"),
  profiles: () => API._json("GET", "/profiles"),
  providers: () => API._json("GET", "/providers"),
  settings: () => API._json("GET", "/settings"),
  saveSettings: (values) => API._json("PUT", "/settings", { values }),
  testProvider: (kind, provider) => API._json("POST", "/settings/test", { kind, provider }),
  books: () => API._json("GET", "/books"),
  book: (id) => API._json("GET", `/books/${id}`),
  createBook: (payload) => API._json("POST", "/books", payload),
  write: (id, payload) => API._json("POST", `/books/${id}/write`, payload || {}),
  chapter: (id, n) => API._json("GET", `/books/${id}/chapters/${n}`),
  graph: (id) => API._json("GET", `/books/${id}/graph`),
  cost: (id) => API._json("GET", `/books/${id}/cost`),
  manuscript: (id) => API._json("GET", `/books/${id}/manuscript`),
  deleteBook: (id) => API._json("DELETE", `/books/${id}`),
  // KDP publishing: generate listing metadata (auto-fill), read it back, and
  // fetch the plain-text listing for clipboard. EPUB/cover are downloaded via
  // direct hrefs / client-side canvas, not these JSON helpers.
  kdpGenerate: (id, payload) => API._json("POST", `/books/${id}/kdp`, payload || {}),
  kdp: (id) => API._json("GET", `/books/${id}/kdp`),
  // Generate a catchy AI cover (artwork + typography) via the image backend.
  generateCover: (id, payload) => API._json("POST", `/books/${id}/cover/generate`, payload || {}),
  // Import pre-written material + modify existing chapters.
  importBook: (payload) => API._json("POST", "/books/import", payload || {}),
  editChapter: (id, n, payload) => API._json("PUT", `/books/${id}/chapters/${n}`, payload || {}),
  reviseChapter: (id, n, payload) => API._json("POST", `/books/${id}/chapters/${n}/revise`, payload || {}),
  appendChapters: (id, payload) => API._json("POST", `/books/${id}/outline`, payload || {}),
};

/* ------------------------------- helpers -------------------------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const tpl = (id) => document.getElementById(id).content.firstElementChild.cloneNode(true);
const app = () => document.getElementById("app");

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Swap the main view in. MUST be synchronous: several views (especially the
// Studio) populate via document-scoped queries ($("#reader-body"), etc.)
// immediately after mounting, so the new view has to be attached before we
// return. A deferred View-Transition swap left the Studio querying a detached
// tree and rendering an empty "0 of 0" skeleton. The per-view CSS `viewIn`
// keyframe still provides a smooth fade-in, so the navigation stays animated.
function mountView(view) {
  app().replaceChildren(view);
}

const fmtMoney = (n, dp = 4) => "$" + (Number(n) || 0).toFixed(dp);
const fmtInt = (n) => (Number(n) || 0).toLocaleString();
const fmtTokens = (n) => {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
};
const initials = (name) => String(name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase() || "?";

// requestAnimationFrame number tween with an easeOutCubic curve. Calls
// `apply(value)` each frame; cancels gracefully if reduced-motion is on.
function tweenNumber(from, to, duration, apply) {
  from = Number(from) || 0; to = Number(to) || 0;
  if (from === to) { apply(to); return; }
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !duration) { apply(to); return; }
  const start = performance.now();
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  function frame(now) {
    const t = Math.min(1, (now - start) / duration);
    apply(from + (to - from) * ease(t));
    if (t < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// Deterministic warm-palette color from a string (for cast avatars / graph nodes).
function hueFor(str) {
  let h = 0;
  for (let i = 0; i < String(str).length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
  return h;
}
const avatarColor = (s) => `hsl(${(hueFor(s) * 0.18 + 12) % 360} 52% 46%)`;

/* -------------------------------- toasts -------------------------------- */
function toast(message, { title, type = "info", timeout = 4200 } = {}) {
  const region = document.getElementById("toast-region");
  const el = document.createElement("div");
  el.className = `toast t-${type}`;
  el.setAttribute("role", "status");
  const icons = {
    good: '<path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
    error: '<path d="M12 8v5M12 16.5v.5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>',
    info: '<path d="M12 11v5M12 8v.5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>',
    warn: '<path d="M12 3l9 16H3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12 10v4M12 16.5v.3" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  };
  el.innerHTML =
    `<span class="toast-icon"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">${icons[type] || icons.info}</svg></span>` +
    `<div class="toast-body">${title ? `<strong>${esc(title)}</strong>` : ""}<span>${esc(message)}</span></div>`;
  region.appendChild(el);
  const close = () => {
    el.classList.add("is-out");
    el.addEventListener("animationend", () => el.remove(), { once: true });
  };
  if (timeout) setTimeout(close, timeout);
  el.addEventListener("click", close);
  return close;
}

/* ------------------------------ flourish -------------------------------- */
// A brief, tasteful completion flourish: a foil ripple over a target element
// plus a few drifting "ink" sparks. Decorative only; skipped for reduced-motion.
function flourish(target, opts) {
  opts = opts || {};
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !target) return;
  const host = document.createElement("div");
  host.className = "flourish" + (opts.grand ? " is-grand" : "");
  host.setAttribute("aria-hidden", "true");
  const n = opts.grand ? 14 : 8;
  let sparks = '<span class="flourish-ring"></span>';
  for (let i = 0; i < n; i++) {
    const a = (i / n) * 360 + Math.random() * 20;
    const dist = (opts.grand ? 90 : 60) + Math.random() * 40;
    const dx = Math.cos((a * Math.PI) / 180) * dist;
    const dy = Math.sin((a * Math.PI) / 180) * dist;
    const d = (0.5 + Math.random() * 0.4).toFixed(2);
    sparks += `<span class="flourish-spark" style="--dx:${dx.toFixed(0)}px;--dy:${dy.toFixed(0)}px;--d:${d}s"></span>`;
  }
  host.innerHTML = sparks;
  // position relative to target
  const wrap = target;
  const prevPos = getComputedStyle(wrap).position;
  if (prevPos === "static") wrap.style.position = "relative";
  wrap.appendChild(host);
  setTimeout(() => host.remove(), opts.grand ? 1600 : 1100);
}

/* -------------------------------- theme --------------------------------- */
function initTheme() {
  // The inline <head> script already applied the stored/preferred theme before
  // first paint (no flash); here we just sync the toggle's pressed state and
  // wire the click handler.
  const stored = localStorage.getItem("bw-theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = document.documentElement.getAttribute("data-theme") || stored || (prefersDark ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-toggle");
  btn.setAttribute("aria-pressed", String(theme === "dark"));
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("bw-theme", next);
    btn.setAttribute("aria-pressed", String(next === "dark"));
  });
}

/* Polite status announcer for assistive tech (short summaries only). */
function srStatus(text) {
  const el = document.getElementById("sr-status");
  if (el) el.textContent = String(text || "");
}

/* ----------------------------- global state ----------------------------- */
const State = {
  hasApiKey: false,
};

/* health pill */
async function refreshHealth() {
  const pill = document.getElementById("api-pill");
  const text = document.getElementById("api-pill-text");
  try {
    const h = await API.health();
    State.hasApiKey = !!h.has_api_key;
    const labels = {
      anthropic: "Anthropic API live",
      openai: "OpenAI live",
      openrouter: "OpenRouter live",
      grok: "Grok (xAI) live",
      "claude-cli": "Claude CLI (subscription)",
      codex: "Codex CLI (ChatGPT sub)",
      "grok-cli": "Grok CLI (subscription)",
      cli: "Custom CLI",
    };
    if (h.has_api_key) { pill.className = "api-pill is-live"; text.textContent = labels[h.provider] || "Live"; }
    else { pill.className = "api-pill is-demo"; text.textContent = "Demo mode only"; }
  } catch {
    pill.className = "api-pill"; text.textContent = "Server offline";
  }
}

/* ============================ NAV / ROUTER ============================== */
function setActiveNav(name) {
  $$(".topnav a").forEach((a) => a.classList.toggle("is-active", a.dataset.nav === name));
}

const Router = {
  current: null,
  start() {
    window.addEventListener("hashchange", () => Router.resolve());
    Router.resolve();
  },
  go(hash) { if (location.hash === hash) Router.resolve(); else location.hash = hash; },
  resolve() {
    // tear down any live studio / book-reader before navigating
    Studio.teardown();
    if (typeof Reader !== "undefined") Reader.teardown();
    const raw = location.hash.replace(/^#/, "") || "/";
    const parts = raw.split("/").filter(Boolean); // e.g. ["b","id","graph"]
    if (parts.length === 0) return Views.library();
    if (parts[0] === "new") {
      // "New book" is a modal, not a page. Land on the library and pop it open.
      const p = Views.library();
      Promise.resolve(p).then(() => CreateModal.open());
      return p;
    }
    if (parts[0] === "b" && parts[1]) {
      const id = parts[1];
      if (parts[2] === "graph") return Views.graph(id);
      if (parts[2] === "manuscript") return Views.manuscript(id);
      if (parts[2] === "publish") return Views.publish(id);
      return Views.studio(id);
    }
    return Views.library();
  },
};

/* ============================== VIEWS ================================== */
const Views = {};

/* ------- Library ------- */
Views.library = async function () {
  setActiveNav("library");
  const view = tpl("tpl-library");
  mountView(view);
  const grid = $("#book-grid", view);
  // skeletons
  grid.innerHTML = Array.from({ length: 3 }).map(() =>
    '<div class="book-card skeleton sk-card"></div>').join("");
  try {
    const { books } = await API.books();
    if (!books || books.length === 0) {
      grid.replaceWith(tpl("tpl-library-empty"));
      return;
    }
    grid.innerHTML = "";
    books.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    renderShelfStats(view, books);
    renderLibraryHero(view, books);
    let i = 0;
    for (const b of books) {
      const card = bookCard(b);
      // Staggered entrance (capped); CSS gates this behind reduced-motion.
      // Remove the class once done so its `transform:none` no longer overrides
      // the pointer-tilt transform on .book-card.
      card.style.setProperty("--i", String(Math.min(i, 8)));
      card.classList.add("card-enter");
      card.addEventListener("animationend", function once(e) {
        if (e.animationName === "cardRise") { card.classList.remove("card-enter"); card.removeEventListener("animationend", once); }
      });
      grid.appendChild(card);
      i++;
    }
  } catch (err) {
    grid.innerHTML = `<p class="rail-empty">Could not load your library: ${esc(err.message)}</p>`;
  }
};

// A one-line "your shelf" readout computed from the BookSummary list (no cost
// field exists on the summary, so we surface volumes / words bound / in progress).
function renderShelfStats(view, books) {
  const el = $("#shelf-stats", view);
  if (!el) return;
  const volumes = books.length;
  const words = books.reduce((s, b) => s + (Number(b.words) || 0), 0);
  const inProgress = books.filter((b) => {
    const total = b.chapters_total || 0, done = b.chapters_written || 0;
    return done > 0 && done < total;
  }).length;
  const bound = books.filter((b) => {
    const total = b.chapters_total || 0, done = b.chapters_written || 0;
    return total > 0 && done >= total;
  }).length;
  const stat = (n, label) =>
    `<div class="shelf-stat"><dt>${label}</dt><dd>${n}</dd></div>`;
  el.innerHTML =
    stat(fmtInt(volumes), volumes === 1 ? "volume" : "volumes") +
    (words ? stat(fmtInt(words), "words bound") : "") +
    (inProgress ? stat(fmtInt(inProgress), "in progress") : "") +
    (bound ? stat(fmtInt(bound), "complete") : "");
  el.hidden = false;
}

// The hero spotlight: the most-recent book at large size + a decorative row of
// spines (Covers.spineSvg) so the shelf reads as a coveted bookshelf, not a grid.
function renderLibraryHero(view, books) {
  const hero = $("#library-hero", view);
  if (!hero || !books.length) return;

  // Choose the featured book intelligently so the flagship card never reads as
  // an empty/unstarted "0%" placeholder. `books` is already sorted newest-first.
  // Prefer the in-progress book furthest along; else the most-recently-updated
  // book with at least one written chapter; else fall back to the newest book
  // (and label its meter clearly so 0% doesn't read as a rendering bug).
  const prog = (bk) => {
    const total = bk.chapters_total || 0, done = bk.chapters_written || 0;
    return total ? done / total : 0;
  };
  const inProgress = books
    .filter((bk) => { const t = bk.chapters_total || 0, d = bk.chapters_written || 0; return d > 0 && d < t; })
    .sort((a, c) => prog(c) - prog(a));
  const started = books.filter((bk) => (bk.chapters_written || 0) > 0); // newest-first preserved
  const b = inProgress[0] || started[0] || books[0];

  const total = b.chapters_total || 0, done = b.chapters_written || 0;
  const pct = total ? done / total : 0;
  const complete = total > 0 && done >= total;
  const started0 = done > 0;

  hero.setAttribute("href", `#/b/${b.id}`);
  $("#hero-eyebrow", view).textContent = complete ? "Most recent" : (started0 ? "Continue writing" : "Newest in the shelf");
  $("#hero-title", view).textContent = b.title || "Untitled";
  $("#hero-logline", view).textContent = b.logline || "No logline yet.";
  paintCover($(".hero-cover", hero), b);
  $("#hero-progress-fill", view).style.width = `${Math.round(pct * 100)}%`;
  $("#hero-progress-label", view).textContent =
    complete ? `Bound · ${done} chapters`
    : started0 ? `${done} of ${total || "—"} chapters · ${Math.round(pct * 100)}%`
    : "Not started yet";

  // The "also on your shelf" row: a proper, legible anchored shelf of the rest
  // of the library (deterministic/offline spine art). If the featured book is the
  // only volume, hide the whole shelf zone so we never show a lone/empty filler.
  const shelfEl = $("#hero-shelf", view);
  const spinesEl = $("#hero-spines", view);
  const rest = books.filter((bk) => bk.id !== b.id).slice(0, 6);
  if (spinesEl && shelfEl && rest.length && window.Covers && typeof window.Covers.spineSvg === "function") {
    spinesEl.innerHTML = rest.map((bk) => {
      try { return `<span class="hero-spine" title="${esc(bk.title || "Untitled")}">${window.Covers.spineSvg(bk)}</span>`; }
      catch { return ""; }
    }).join("");
    shelfEl.hidden = false;
  } else if (shelfEl) {
    shelfEl.hidden = true;
  }
  hero.hidden = false;
}

function bookCard(b) {
  const card = tpl("tpl-book-card");
  card.setAttribute("href", `#/b/${b.id}`);
  card.setAttribute("aria-label", `${b.title || "Untitled"}${b.genre ? ", " + b.genre : ""}`);
  $(".card-genre", card).textContent = b.genre || "Untitled genre";
  // The per-card corner badge is redundant (the global "Demo mode" pill and the
  // card's own status line already convey it) and reads as placeholder chrome on
  // the shelf, so keep covers clean and never show it.
  $(".card-mock", card).hidden = true;
  $(".card-title", card).textContent = b.title || "Untitled";
  $(".card-logline", card).textContent = b.logline || "No logline yet.";

  // Procedural cover artwork (deterministic from id/title/genre).
  paintCover($(".cover-art", card), b);

  const total = b.chapters_total || 0, done = b.chapters_written || 0;
  const pct = total ? done / total : 0;
  const ring = $(".ring-fg", card);
  const C = 2 * Math.PI * 18;
  ring.style.strokeDasharray = C.toFixed(1);
  // animate in next frame
  ring.style.strokeDashoffset = C.toFixed(1);
  requestAnimationFrame(() => { ring.style.strokeDashoffset = (C * (1 - pct)).toFixed(1); });
  $(".ring-label", card).textContent = Math.round(pct * 100) + "%";
  $(".card-chapters", card).textContent = `${done} / ${total} chapters`;
  // The library summary has no cost field (BookSummary), so show the demo/live
  // mode instead of a perpetual dash.
  const costEl = $(".card-cost", card);
  if (costEl) costEl.textContent = b.mock ? "Demo" : (done >= total && total ? "Complete" : "In progress");

  const del = $(".card-delete", card);
  if (del) del.addEventListener("click", (e) => {
    // The card is an <a>; don't navigate when deleting.
    e.preventDefault(); e.stopPropagation();
    deleteBookFromLibrary(b, card);
  });

  attachTilt(card);
  return card;
}

async function deleteBookFromLibrary(b, card) {
  const title = b.title || "this book";
  if (!window.confirm(`Delete “${title}”?\n\nThis permanently removes the book, its chapters, and any generated images. This cannot be undone.`)) return;
  card.classList.add("is-deleting");
  try {
    await API.deleteBook(b.id);
    toast("Book deleted.", { title: title, type: "info" });
    // Re-render the library so stats/hero update too.
    if (location.hash.replace(/^#/, "").replace(/\/$/, "") === "" || location.hash === "") Views.library();
    else Router.go("#/");
  } catch (err) {
    card.classList.remove("is-deleting");
    toast(err.message || "Could not delete the book.", { title: "Delete failed", type: "error" });
  }
}

/* Render a procedural cover into a holder element (no-op if Covers missing). */
function paintCover(holder, book) {
  if (!holder) return;
  if (window.Covers && typeof window.Covers.svg === "function") {
    try { holder.innerHTML = window.Covers.svg(book); return; }
    catch { /* fall through to fallback */ }
  }
  // Graceful fallback: a plain tinted plate with the title.
  holder.classList.add("cover-fallback");
  holder.textContent = book.title || "Untitled";
}

// Subtle 3D pointer-tilt for library cards. Respects reduced-motion and only
// runs on fine pointers (no jank on touch). Purely decorative.
function attachTilt(card) {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (window.matchMedia && !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  let raf = 0;
  const onEnter = () => {
    // While tracking the pointer the tilt should follow snappily (no lag), so we
    // drop the transition; the spring-ish settle is restored on leave.
    card.classList.add("is-tilting");
  };
  const onMove = (e) => {
    const rect = card.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width - 0.5;
    const py = (e.clientY - rect.top) / rect.height - 0.5;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      card.style.setProperty("--tilt-x", (py * -7).toFixed(2) + "deg");
      card.style.setProperty("--tilt-y", (px * 8).toFixed(2) + "deg");
      card.style.setProperty("--gloss-x", ((px + 0.5) * 100).toFixed(1) + "%");
      card.style.setProperty("--gloss-y", ((py + 0.5) * 100).toFixed(1) + "%");
    });
  };
  const reset = () => {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    // Spring-ish return: re-enable the transition (CSS uses --ease-spring while
    // not tilting) then drop the tilt to neutral.
    card.classList.remove("is-tilting");
    card.style.setProperty("--tilt-x", "0deg");
    card.style.setProperty("--tilt-y", "0deg");
  };
  card.addEventListener("pointerenter", onEnter);
  card.addEventListener("pointermove", onMove);
  card.addEventListener("pointerleave", reset);
}

/* ===================== CREATE AI BOOK (modal) ========================= */
// The book-setup dialog. Replaces the full-page composer as the "New book"
// entry point: provider/model pickers (wired per-book to the backend provider
// system), chapter length, writing style, audience, book type, and toggles —
// with the live cover forge beside it. "Generate Outline" plans the book
// (Step 1), then drops into the studio to review + write (Step 2).
const reduceMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Keep Tab cycling inside an open modal overlay. aria-modal="true" is only a
   promise to assistive tech — it doesn't actually stop Tab from wandering into
   the inert page behind, so each dialog wires this on open (the command palette
   and help modal in palette.js trap focus the same way). */
function trapFocus(overlay) {
  overlay.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const els = Array.from(overlay.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((el) => !el.closest("[hidden]"));
    if (!els.length) { e.preventDefault(); return; }
    const first = els[0], last = els[els.length - 1];
    const cur = document.activeElement;
    if (e.shiftKey) {
      if (cur === first || !overlay.contains(cur)) { e.preventDefault(); last.focus(); }
    } else if (cur === last || !overlay.contains(cur)) {
      e.preventDefault(); first.focus();
    }
  });
}

const CreateModal = {
  el: null, lastFocused: null, catalog: null, demoUserSet: false,

  async open() {
    if (CreateModal.el) return;
    CreateModal.lastFocused = document.activeElement;
    CreateModal.demoUserSet = false;
    const node = tpl("tpl-create");
    document.body.appendChild(node);
    document.body.classList.add("cmdk-lock");
    CreateModal.el = node;

    node.addEventListener("mousedown", (e) => { if (e.target === node) CreateModal.close(); });
    node.querySelector(".create-close").addEventListener("click", () => CreateModal.close());
    node.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); CreateModal.close(); }
    });
    trapFocus(node);

    await CreateModal.populateProviders(node);

    const demo = node.querySelector("#cm-demo");
    if (!State.hasApiKey) demo.checked = true;
    demo.addEventListener("change", () => { CreateModal.demoUserSet = true; CreateModal.refreshNote(node); });

    // Chapter-images availability hint (image backend is configured server-side).
    const imgToggle = node.querySelector("#cm-chapter-images");
    imgToggle.addEventListener("change", () => CreateModal.refreshImageNote(node));
    CreateModal.refreshImageNote(node);

    const seed = "cm-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    bindForge(node, seed);

    node.querySelector("#create-form").addEventListener("submit", (e) => CreateModal.submit(e, node));

    if (!reduceMotion()) requestAnimationFrame(() => node.classList.add("is-open"));
    else node.classList.add("is-open");
    setTimeout(() => { const f = node.querySelector("#cm-topic"); if (f) f.focus(); }, 60);
  },

  close() {
    const node = CreateModal.el;
    if (!node) return;
    CreateModal.el = null;
    // Release the scroll lock only when no OTHER locking overlay remains open
    // (overlays fading out keep their root class ~280ms — skip .is-closing).
    if (!document.querySelector(".cmdk-overlay:not(.is-closing), .help-overlay:not(.is-closing), .settings-overlay:not(.is-closing), .import-overlay")) document.body.classList.remove("cmdk-lock");
    const finish = () => node.remove();
    if (reduceMotion()) finish();
    else {
      node.classList.remove("is-open"); node.classList.add("is-closing");
      let done = false;
      const end = () => { if (done) return; done = true; finish(); };
      node.addEventListener("transitionend", end, { once: true });
      setTimeout(end, 280);
    }
    const prev = CreateModal.lastFocused; CreateModal.lastFocused = null;
    if (prev && typeof prev.focus === "function" && document.contains(prev)) { try { prev.focus(); } catch {} }
    // If we arrived via the #/new deep-link, return to the library cleanly.
    if (location.hash === "#/new") Router.go("#/");
  },

  async populateProviders(node) {
    if (!CreateModal.catalog) {
      try { CreateModal.catalog = await API.providers(); }
      catch { CreateModal.catalog = { providers: [], current: "anthropic" }; }
    }
    const cat = CreateModal.catalog;
    const sel = node.querySelector("#cm-provider");
    sel.innerHTML = "";
    (cat.providers || []).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.label + (p.available ? "" : " — not configured");
      sel.appendChild(o);
    });
    const avail = (cat.providers || []).filter((p) => p.available);
    let def = cat.current;
    if (!avail.find((p) => p.id === cat.current) && avail.length) def = avail[0].id;
    if (def) sel.value = def;
    CreateModal.populateModels(node);
    sel.addEventListener("change", () => { CreateModal.populateModels(node); CreateModal.refreshNote(node); });
    CreateModal.refreshNote(node);
  },

  populateModels(node) {
    const cat = CreateModal.catalog || { providers: [] };
    const pid = node.querySelector("#cm-provider").value;
    const prov = (cat.providers || []).find((p) => p.id === pid);
    const msel = node.querySelector("#cm-model");
    const label = node.querySelector("#cm-model-label");
    const models = (prov && prov.models) || [];
    msel.innerHTML = "";
    if (!models.length) {
      const o = document.createElement("option"); o.value = ""; o.textContent = "Default"; msel.appendChild(o);
    } else {
      models.forEach((m) => { const o = document.createElement("option"); o.value = m.id; o.textContent = m.label; msel.appendChild(o); });
    }
    // A subscription CLI's single "" model ("default") isn't a real choice.
    const trivial = models.length <= 1 && (!models[0] || models[0].id === "");
    msel.disabled = trivial;
    label.textContent = (prov ? prov.label + " " : "") + "Text Model";
  },

  refreshNote(node) {
    const cat = CreateModal.catalog || { providers: [] };
    const pid = node.querySelector("#cm-provider").value;
    const prov = (cat.providers || []).find((p) => p.id === pid);
    const demo = node.querySelector("#cm-demo");
    const note = node.querySelector("#cm-note");
    note.classList.remove("is-error");
    if (prov && !prov.available) {
      if (!CreateModal.demoUserSet) demo.checked = true;
      note.textContent = demo.checked
        ? `${prov.label} isn’t configured — Demo mode is on so you can still try the flow.`
        : `${prov.label} isn’t configured on the server; a live run will be rejected.`;
    } else {
      note.textContent = "";
    }
  },

  refreshImageNote(node) {
    const el = node.querySelector("#cm-img-note");
    if (!el) return;
    const on = node.querySelector("#cm-chapter-images").checked;
    const img = (CreateModal.catalog && CreateModal.catalog.image) || null;
    if (on && img && !img.available) {
      el.hidden = false;
      el.classList.add("is-error");
      el.textContent = `Chapter images need an image provider — set PIXIO_API_KEY (default) or BOOKWRITER_IMAGE_PROVIDER. Chapters will be written without images until then.`;
    } else if (on && img && img.available) {
      el.hidden = false; el.classList.remove("is-error");
      el.textContent = `Images via ${img.provider}. One illustration per chapter is generated as the book is written.`;
    } else {
      el.hidden = true; el.textContent = "";
    }
  },

  async submit(e, node) {
    e.preventDefault();
    const note = node.querySelector("#cm-note");
    note.textContent = ""; note.classList.remove("is-error");
    const val = (s) => (node.querySelector(s).value || "").trim();

    const title = val("#cm-title");
    const topic = val("#cm-topic");
    const premise = topic || title;
    if (!premise) {
      note.textContent = "Add a topic (or at least a title) so the planner has a seed.";
      note.classList.add("is-error");
      node.querySelector("#cm-topic").focus();
      return;
    }

    const style = val("#cm-style"), audience = val("#cm-audience"), bookType = val("#cm-booktype");
    const textGraphics = node.querySelector("#cm-text-graphics").checked;
    const guidance = [
      style ? `Writing style: ${style}.` : "",
      audience ? `Intended audience: ${audience}.` : "",
      bookType ? `Book type: ${bookType}.` : "",
      textGraphics
        ? "You may include charts, diagrams, tables, and visual explainers where they genuinely help."
        : "Do not include charts, diagrams, tables, or visual explainers — write prose only.",
    ].filter(Boolean).join(" ");

    const chapters = node.querySelector("#cm-chapters").value;
    const payload = {
      premise,
      title: title || undefined,
      genre: bookType || undefined,
      guidance,
      chapters: chapters ? Number(chapters) : undefined,
      words_per_chapter: Number(node.querySelector("#cm-length").value) || 2000,
      profile: "balanced",
      mock: node.querySelector("#cm-demo").checked,
      use_cache: true,
      run_continuity_check: true,
      provider: node.querySelector("#cm-provider").value || undefined,
      model: node.querySelector("#cm-model").value || undefined,
      chapter_images: node.querySelector("#cm-chapter-images").checked,
    };

    const btn = node.querySelector("#cm-submit");
    btn.classList.add("is-busy"); btn.disabled = true;
    $(".btn-label", btn).textContent = "Generating outline…";
    try {
      const res = await API.createBook(payload);
      const bookEl = node.querySelector("#cm-book");
      if (bookEl && !reduceMotion() && node.querySelector("#cm-gen-cover").checked) {
        bookEl.classList.add("is-binding");
        if (typeof flourish === "function") flourish(bookEl);
        await new Promise((r) => setTimeout(r, 500));
      }
      toast("Your story bible is ready.", { title: res.book.title || "Outline ready", type: "good" });
      CreateModal.close();
      Router.go(`#/b/${res.book.id}`);
    } catch (err) {
      note.textContent = err.message || "Could not generate the outline.";
      note.classList.add("is-error");
      toast(err.message || "Generation failed.", { title: "Could not create book", type: "error" });
    } finally {
      btn.classList.remove("is-busy"); btn.disabled = false;
      $(".btn-label", btn).textContent = "Generate Outline with AI";
    }
  },
};
window.CreateModal = CreateModal;

/* ========================== IMPORT (modal) ========================== */
// Bring pre-written material in as a first-class book: paste or upload, then
// POST /books/import (split + reverse-engineer bible) and open the studio.
const ImportModal = {
  el: null, lastFocused: null,
  open() {
    if (ImportModal.el) return;
    ImportModal.lastFocused = document.activeElement;
    const node = tpl("tpl-import");
    document.body.appendChild(node);
    document.body.classList.add("cmdk-lock");
    ImportModal.el = node;
    node.addEventListener("mousedown", (e) => { if (e.target === node) ImportModal.close(); });
    node.querySelector(".im-close").addEventListener("click", () => ImportModal.close());
    node.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); ImportModal.close(); } });
    trapFocus(node);

    const text = node.querySelector("#im-text");
    const stat = node.querySelector("#im-stat");
    const updateStat = () => {
      const v = text.value.trim();
      const words = v ? v.split(/\s+/).length : 0;
      // quick chapter-count preview (matches the server splitter's heuristics)
      const heads = (v.match(/^\s{0,3}#{1,3}\s+\S|^\s*(?:chapter|part)\s+/gim) || []).length;
      stat.textContent = v ? `${fmtInt(words)} words · ~${heads || 1} chapter(s) detected` : "No text yet.";
    };
    text.addEventListener("input", updateStat);

    node.querySelector("#im-file").addEventListener("change", async (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      try {
        text.value = await f.text();
        if (!node.querySelector("#im-title").value) {
          node.querySelector("#im-title").value = f.name.replace(/\.[^.]+$/, "");
        }
        updateStat();
      } catch { toast("Couldn't read that file.", { type: "error" }); }
    });

    node.querySelector("#im-submit").addEventListener("click", () => ImportModal.submit(node));
    // .create-overlay is opacity:0 until .is-open (matches Create/Settings modals).
    requestAnimationFrame(() => node.classList.add("is-open"));
    setTimeout(() => text.focus(), 60);
  },
  close() {
    const node = ImportModal.el;
    if (!node) return;
    ImportModal.el = null;
    node.remove();
    // Release the scroll lock unless another locking overlay is still open
    // (this node is already out of the DOM, so it can't match itself; overlays
    // mid-fade are excluded via .is-closing).
    if (!document.querySelector(".cmdk-overlay:not(.is-closing), .help-overlay:not(.is-closing), .create-overlay:not(.is-closing)")) {
      document.body.classList.remove("cmdk-lock");
    }
    const prev = ImportModal.lastFocused; ImportModal.lastFocused = null;
    if (prev && prev.focus) prev.focus();
  },
  async submit(node) {
    const btn = node.querySelector("#im-submit");
    const note = node.querySelector("#im-note");
    const text = node.querySelector("#im-text").value.trim();
    note.textContent = "";
    if (!text) { note.textContent = "Paste or upload a manuscript first."; node.querySelector("#im-text").focus(); return; }
    btn.classList.add("is-busy"); btn.disabled = true;
    $(".btn-label", btn).textContent = "Importing…";
    try {
      const res = await API.importBook({
        text,
        title: node.querySelector("#im-title").value.trim() || undefined,
        genre: node.querySelector("#im-genre").value.trim() || undefined,
        analyze: node.querySelector("#im-analyze").checked,
        mock: node.querySelector("#im-mock").checked,
      });
      const id = res && res.book && res.book.id;
      toast("Manuscript imported.", { title: "Imported", type: "good" });
      ImportModal.close();
      if (id) Router.go(`#/b/${id}`); else Router.go("#/");
    } catch (err) {
      note.textContent = err.message || "Import failed.";
      toast(err.message || "Import failed.", { title: "Couldn't import", type: "error" });
    } finally {
      btn.classList.remove("is-busy"); btn.disabled = false;
      $(".btn-label", btn).textContent = "Import & open";
    }
  },
};
window.ImportModal = ImportModal;

/* Live cover forge for the modal — the jacket designs itself from the title /
   topic / book-type, mirroring the composer's forge (reduced-motion-safe). */
function bindForge(node, seed) {
  const coverEl = node.querySelector("#cm-cover");
  const bookEl = node.querySelector("#cm-book");
  const emptyEl = node.querySelector("#cm-empty");
  const titleEl = node.querySelector("#cm-title");
  const topicEl = node.querySelector("#cm-topic");
  const typeEl = node.querySelector("#cm-booktype");
  if (!coverEl || !window.Covers || typeof window.Covers.svg !== "function") return;

  const workingTitle = () => {
    const t = (titleEl.value || "").trim();
    if (t) return t;
    const p = (topicEl.value || "").trim();
    if (p) {
      const words = p.replace(/[.,;:!?"'—–-].*$/, "").split(/\s+/).filter(Boolean).slice(0, 5);
      if (words.length) return words.join(" ");
    }
    return "";
  };
  const render = () => {
    const title = workingTitle();
    const genre = (typeEl.value || "").trim();
    const hasInput = !!(title || genre);
    if (emptyEl) emptyEl.hidden = hasInput;
    coverEl.hidden = !hasInput;
    if (!hasInput) { coverEl.innerHTML = ""; return; }
    try { coverEl.innerHTML = window.Covers.svg({ id: seed, title: title || "Untitled", genre }, { seed }); }
    catch { return; }
    if (!reduceMotion()) { bookEl.classList.remove("is-stamp"); void bookEl.offsetWidth; bookEl.classList.add("is-stamp"); }
  };
  let t = 0;
  const onInput = () => { clearTimeout(t); t = setTimeout(render, 120); };
  [titleEl, topicEl, typeEl].forEach((el) => el && el.addEventListener("input", onInput));
  typeEl && typeEl.addEventListener("change", render);
  render();
}

/* ========================== SETTINGS (modal) ========================== */
// In-app API-key + provider configuration. Keys are stored server-side in the
// data folder (masked in responses); saving takes effect immediately. Each key
// row has a Test button that verifies the account is actually reachable.
const SettingsModal = {
  el: null, lastFocused: null, data: null,
  KEYS: [
    { name: "ANTHROPIC_API_KEY", label: "Anthropic API key", kind: "llm", provider: "anthropic", ph: "sk-ant-…" },
    { name: "OPENAI_API_KEY", label: "OpenAI API key", kind: "llm", provider: "openai", ph: "sk-…" },
    { name: "OPENROUTER_API_KEY", label: "OpenRouter API key", kind: "llm", provider: "openrouter", ph: "sk-or-…" },
    { name: "GROK_API_KEY", label: "Grok (xAI) API key", kind: "llm", provider: "grok", ph: "xai-…" },
    { name: "PIXIO_API_KEY", label: "Pixio API key — chapter images", kind: "image", provider: "pixio", ph: "pxio_live_…" },
  ],
  // Subscription backends — auth lives in the vendor CLI (you sign in once in a
  // terminal); we detect the CLI and let you pick it as your writing model.
  SUBS: [
    { provider: "claude-cli", label: "Claude — Pro / Max", signin: "Install Claude Code, then run `claude` and use /login (or `claude setup-token`)." },
    { provider: "codex", label: "ChatGPT — Plus / Pro", signin: "Install the OpenAI Codex CLI, then run `codex login` and choose “Sign in with ChatGPT”." },
    { provider: "grok-cli", label: "Grok — X Premium / SuperGrok", signin: "Install a Grok CLI and sign in; set its command under Advanced if it isn’t `grok`." },
  ],

  _provById(id) { return ((SettingsModal.data && SettingsModal.data.llm.providers) || []).find((p) => p.id === id); },

  async open() {
    if (SettingsModal.el) return;
    SettingsModal.lastFocused = document.activeElement;
    const node = tpl("tpl-settings");
    document.body.appendChild(node);
    document.body.classList.add("cmdk-lock");
    SettingsModal.el = node;

    node.addEventListener("mousedown", (e) => { if (e.target === node) SettingsModal.close(); });
    node.querySelector(".set-close").addEventListener("click", () => SettingsModal.close());
    node.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); SettingsModal.close(); } });
    trapFocus(node);
    node.querySelector("#set-save").addEventListener("click", () => SettingsModal.save(node));
    node.querySelector("#set-img").addEventListener("change", () => SettingsModal.toggleHttp(node));

    try { SettingsModal.data = await API.settings(); }
    catch (e) { SettingsModal.data = null; node.querySelector("#set-note").textContent = "Couldn't load settings: " + (e.message || e); }
    if (SettingsModal.data) SettingsModal.render(node);

    if (!reduceMotion()) requestAnimationFrame(() => node.classList.add("is-open"));
    else node.classList.add("is-open");
    // Move focus INTO the dialog — the Escape handler and focus trap live on the
    // overlay, so they only work once focus is inside it.
    setTimeout(() => {
      const f = node.querySelector("#set-llm") || node.querySelector(".set-close");
      if (f) { try { f.focus(); } catch {} }
    }, 60);
  },

  close() {
    const node = SettingsModal.el;
    if (!node) return;
    SettingsModal.el = null;
    // Release the scroll lock only when no OTHER locking overlay remains open
    // (this node keeps .create-overlay while fading — excluded via
    // :not(.settings-overlay); overlays mid-fade are excluded via .is-closing).
    if (!document.querySelector(".cmdk-overlay:not(.is-closing), .help-overlay:not(.is-closing), .create-overlay:not(.settings-overlay):not(.is-closing)")) {
      document.body.classList.remove("cmdk-lock");
    }
    const finish = () => node.remove();
    if (reduceMotion()) finish();
    else { node.classList.remove("is-open"); node.classList.add("is-closing"); setTimeout(finish, 280); }
    const prev = SettingsModal.lastFocused; SettingsModal.lastFocused = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch {} }
  },

  render(node) {
    const d = SettingsModal.data;
    // Default writing-model select.
    const llm = node.querySelector("#set-llm");
    llm.innerHTML = "";
    (d.llm.providers || []).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.label + (p.available ? " ✓" : "");
      llm.appendChild(o);
    });
    llm.value = d.options.BOOKWRITER_LLM_PROVIDER || d.llm.selected || "anthropic";
    llm.onchange = () => SettingsModal.renderStatus(node);

    SettingsModal.renderStatus(node);
    SettingsModal.renderSubs(node);

    // Image provider + custom-http fields.
    node.querySelector("#set-img").value = d.options.BOOKWRITER_IMAGE_PROVIDER || d.image.provider || "pixio";
    SettingsModal.toggleHttp(node);
    node.querySelector("#set-img-hint").textContent = d.image.available
      ? `Active backend: ${d.image.provider}.`
      : `No image backend configured — chapter images will be skipped until you add a key.`;

    // Prefill every [data-opt] field from saved options.
    node.querySelectorAll("[data-opt]").forEach((el) => {
      const k = el.getAttribute("data-opt");
      if (k in d.options && d.options[k]) el.value = d.options[k];
    });

    // Secret custom-HTTP auth header: never echoed in full — show a "saved"
    // placeholder from the masked hint; empty on save = leave unchanged.
    const authEl = node.querySelector("#set-img-auth");
    if (authEl) {
      const info = (d.keys && d.keys.BOOKWRITER_IMAGE_AUTH) || { set: false, masked: "" };
      if (info.set) authEl.placeholder = `Saved (${info.masked}) — type to replace`;
    }

    // Key rows.
    const wrap = node.querySelector("#set-keys");
    wrap.innerHTML = "";
    SettingsModal.KEYS.forEach((k) => {
      const info = d.keys[k.name] || { set: false, masked: "" };
      const row = document.createElement("div");
      row.className = "set-key-row";
      row.dataset.key = k.name; row.dataset.kind = k.kind; row.dataset.provider = k.provider;
      row.innerHTML =
        `<div class="set-key-head"><label>${esc(k.label)}</label>` +
        `<span class="set-badge ${info.set ? "is-saved" : "is-unset"}">${info.set ? "Saved" : "Not set"}</span></div>` +
        `<div class="set-key-input">` +
        `<input type="password" autocomplete="off" spellcheck="false" data-secret="${esc(k.name)}" ` +
        `placeholder="${info.set ? `Saved (${esc(info.masked)}) — type to replace` : esc(k.ph)}">` +
        `<button type="button" class="btn btn-ghost set-test">Test</button>` +
        (info.set ? `<button type="button" class="btn-link set-clear">Clear</button>` : "") +
        `</div><p class="field-hint set-key-detail" hidden></p>`;
      row.querySelector(".set-test").addEventListener("click", () => SettingsModal.test(node, row));
      const clr = row.querySelector(".set-clear");
      if (clr) clr.addEventListener("click", () => SettingsModal.clearKey(node, k.name));
      wrap.appendChild(row);
    });
  },

  toggleHttp(node) {
    node.querySelector("#set-http").hidden = node.querySelector("#set-img").value !== "http";
  },

  // Banner: is the currently-selected default writing model ready to generate?
  renderStatus(node) {
    const el = node.querySelector("#set-status");
    if (!el) return;
    const sel = node.querySelector("#set-llm").value;
    const prov = SettingsModal._provById(sel);
    el.hidden = false;
    if (prov && prov.available) {
      el.className = "settings-status is-ok";
      el.textContent = `Ready to generate with ${prov.label}.`;
    } else {
      el.className = "settings-status is-warn";
      el.textContent = `“${prov ? prov.label : sel}” isn’t connected yet — add its key or pick a connected provider below. Until then new books run in Demo mode.`;
    }
  },

  renderSubs(node) {
    const wrap = node.querySelector("#set-subs");
    if (!wrap) return;
    wrap.innerHTML = "";
    SettingsModal.SUBS.forEach((s) => {
      const prov = SettingsModal._provById(s.provider);
      const found = !!(prov && prov.available);
      const row = document.createElement("div");
      row.className = "set-key-row";
      row.dataset.kind = "llm"; row.dataset.provider = s.provider;
      row.innerHTML =
        `<div class="set-key-head"><label>${esc(s.label)}</label>` +
        `<span class="set-badge ${found ? "is-saved" : "is-unset"}">${found ? "CLI detected" : "Not found"}</span></div>` +
        `<div class="set-key-input">` +
        `<button type="button" class="btn btn-ghost set-use">Use for writing</button>` +
        `<button type="button" class="btn btn-ghost set-test">Test</button>` +
        `</div>` +
        `<p class="field-hint set-key-detail">${esc(s.signin)}</p>`;
      row.querySelector(".set-use").addEventListener("click", () => SettingsModal.useProvider(node, s.provider));
      row.querySelector(".set-test").addEventListener("click", () => SettingsModal.test(node, row));
      wrap.appendChild(row);
    });
  },

  async useProvider(node, provider) {
    node.querySelector("#set-llm").value = provider;
    await SettingsModal.save(node);          // persists BOOKWRITER_LLM_PROVIDER + refreshes pill
    SettingsModal.renderStatus(node);
    toast(`New books will write with ${provider}.`, { type: "good" });
  },

  collectValues(node) {
    const values = {};
    node.querySelectorAll("[data-opt]").forEach((el) => { values[el.getAttribute("data-opt")] = el.value.trim(); });
    node.querySelectorAll("[data-secret]").forEach((el) => {
      const v = el.value.trim();
      if (v) values[el.getAttribute("data-secret")] = v;  // empty = leave unchanged
    });
    return values;
  },

  async save(node, opts) {
    opts = opts || {};
    const btn = node.querySelector("#set-save");
    const note = node.querySelector("#set-note");
    note.textContent = ""; note.classList.remove("is-error");
    btn.classList.add("is-busy"); btn.disabled = true;
    try {
      SettingsModal.data = await API.saveSettings(SettingsModal.collectValues(node));
      SettingsModal.render(node);               // re-render: fresh masks + badges
      CreateModal.catalog = null;               // make the create modal re-fetch providers
      refreshHealth();
      if (!opts.silent) toast("Settings saved.", { title: "Saved", type: "good" });
    } catch (e) {
      note.textContent = e.message || "Could not save settings."; note.classList.add("is-error");
      if (!opts.silent) toast(note.textContent, { title: "Save failed", type: "error" });
      throw e;
    } finally {
      btn.classList.remove("is-busy"); btn.disabled = false;
    }
  },

  async clearKey(node, name) {
    try {
      SettingsModal.data = await API.saveSettings({ [name]: "" });
      SettingsModal.render(node);
      CreateModal.catalog = null; refreshHealth();
      toast("Key cleared.", { type: "info" });
    } catch (e) { toast(e.message || "Could not clear key.", { type: "error" }); }
  },

  async test(node, row) {
    const badge = row.querySelector(".set-badge");
    const detail = row.querySelector(".set-key-detail");
    const kind = row.dataset.kind, provider = row.dataset.provider;
    badge.className = "set-badge is-testing"; badge.textContent = "Testing…";
    try {
      await SettingsModal.save(node, { silent: true });   // test exactly what's entered
      const r = await API.testProvider(kind, provider);
      badge.className = "set-badge " + (r.ok ? "is-active" : "is-fail");
      badge.textContent = r.ok ? "Active" : "Failed";
      detail.hidden = false; detail.textContent = r.detail || "";
      detail.classList.toggle("is-error", !r.ok);
    } catch (e) {
      badge.className = "set-badge is-fail"; badge.textContent = "Failed";
      detail.hidden = false; detail.textContent = e.message || "Test failed."; detail.classList.add("is-error");
    }
  },
};
window.SettingsModal = SettingsModal;

/* ============================== STUDIO ================================= */
const Studio = {
  id: null,
  data: null,          // GET /api/books/{id}
  es: null,            // EventSource
  active: null,        // active chapter number in reader
  streamingNum: null,  // chapter currently streaming
  buffers: {},         // number -> accumulated streamed text
  running: false,
  startedLive: false,       // this session actually started/attached to a run
  initiallyComplete: false, // book was fully written when the studio mounted

  teardown() {
    Studio.disconnect();
    Studio.id = null; Studio.data = null; Studio.active = null;
    Studio.streamingNum = null; Studio.buffers = {}; Studio.running = false;
    Studio.startedLive = false; Studio.initiallyComplete = false;
  },

  // Close the SSE stream. The server ends the stream after a terminal event
  // ('done'/'error'), and EventSource auto-reconnects (~3s) after ANY close —
  // including a clean one — replaying the whole job history each time. So on
  // terminal events we must close explicitly; onGenerate re-attaches via
  // Studio.connect(true) for the next run.
  disconnect() {
    if (Studio.es) { try { Studio.es.close(); } catch {} Studio.es = null; }
  },
};

Views.studio = async function (id) {
  setActiveNav("");
  Studio.id = id;
  const view = tpl("tpl-studio");
  mountView(view);

  // tab wiring — these are route-changing nav links (real hrefs so
  // middle/right-click and aria-current work), not an ARIA tab widget.
  $$(".studio-tabs .tab", view).forEach((t) => {
    const tab = t.dataset.tab;
    const href = tab === "graph" ? `#/b/${id}/graph`
      : tab === "manuscript" ? `#/b/${id}/manuscript`
      : tab === "publish" ? `#/b/${id}/publish`
      : `#/b/${id}`;
    t.setAttribute("href", href);
  });
  $("#generate-btn", view).addEventListener("click", Studio.onGenerate);
  Studio.bindModify();

  // loading skeleton in reader
  $("#reader-body", view).innerHTML =
    '<div class="sk-line w-80 skeleton"></div><div class="sk-line skeleton"></div><div class="sk-line w-60 skeleton"></div>';

  try {
    const data = await API.book(id);
    if (Studio.id !== id) return; // navigated away while loading — don't paint stale data
    Studio.data = data;
    Studio.renderShell();
    Studio.renderChapters();
    Studio.renderCost(data.cost);
    await Studio.renderGraphRail();
    if (Studio.id !== id) return; // route moved on during the graph fetch
    // pick first written chapter, else first
    const written = (data.chapters || []).filter((c) => c.written);
    const total = (data.chapters || []).length;
    // Reconnecting to the event stream replays the last job's whole history;
    // remember whether this book was already fully written so replayed events
    // don't re-fire follow/celebration side effects (see handleEvent).
    Studio.startedLive = false;
    Studio.initiallyComplete = total > 0 && written.length === total;
    if (written.length) Studio.openChapter(written[0].number);
    else Studio.showPlaceholder();
    // initial control label: Resume if partially written, else Generate
    const gen = $("#generate-btn", view);
    $(".btn-label", gen).textContent =
      written.length && written.length < total ? "Resume" : "Generate";
    // connect to events to replay any in-progress / last job
    Studio.connect();
  } catch (err) {
    if (Studio.id !== id) return;
    app().innerHTML = `<div class="empty-state"><h2 class="serif">We couldn't open that book.</h2><p>${esc(err.message)}</p><a class="btn btn-primary" href="#/">Back to library</a></div>`;
  }
};

Studio.renderShell = function () {
  const b = Studio.data.book, bible = Studio.data.bible || {};
  const title = b.title || bible.title || "Untitled";
  const genre = b.genre || bible.genre || "Manuscript";
  $("#studio-genre").textContent = genre;
  $("#studio-title").textContent = title;
  $("#studio-logline").textContent = b.logline || bible.logline || bible.premise || "";
  document.title = `${b.title || "Book"} · Bookwriter Pro`;
  // Hero cover for this book (same seed as the library card).
  paintCover($("#studio-cover"), { id: b.id, title, genre, logline: b.logline });
};

Studio.renderChapters = function () {
  const list = $("#chapter-list");
  const chapters = Studio.data.chapters || [];
  list.innerHTML = "";
  for (const c of chapters) list.appendChild(Studio.chapterItem(c));
  Studio.updateProgress();
};

Studio.chapterItem = function (c) {
  const li = document.createElement("li");
  li.className = "chapter-item" + (c.written ? " is-written" : "");
  const num = c.number;
  li.dataset.num = num;
  li.tabIndex = 0;
  li.setAttribute("role", "button");
  li.innerHTML =
    `<span class="ci-num">${num}</span>` +
    `<span class="ci-main"><span class="ci-title">${esc(c.title || "Untitled")}</span>` +
    `<span class="ci-meta"><span>Act ${c.act || 1}</span>${c.word_count ? `<span>${fmtInt(c.word_count)} w</span>` : ""}</span></span>`;
  const open = () => Studio.openChapter(num);
  li.addEventListener("click", open);
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  return li;
};

Studio.updateProgress = function () {
  const chapters = Studio.data.chapters || [];
  const total = chapters.length;
  const done = chapters.filter((c) => c.written).length;
  $("#studio-progress-fill").style.width = total ? `${(done / total) * 100}%` : "0%";
  $("#studio-progress-label").textContent = `${done} of ${total} chapters`;
};

Studio.setChapterStatus = function (num, status) {
  const li = $(`#chapter-list .chapter-item[data-num="${num}"]`);
  if (!li) return;
  li.classList.remove("is-writing");
  if (status === "writing") li.classList.add("is-writing");
  if (status === "written") li.classList.add("is-written");
};

Studio.showPlaceholder = function () {
  $("#reader-act").textContent = "Act —";
  $("#reader-title").textContent = "Select a chapter";
  $("#reader-words").textContent = "";
  $("#reader-body").classList.remove("is-streaming");
  $("#reader-body").innerHTML =
    '<div class="reader-placeholder"><p class="serif">Your story will appear here.</p>' +
    '<p>Press <strong>Generate</strong> to plan-and-write live, or pick a written chapter from the outline.</p></div>';
  $("#reader-flags").hidden = true;
};

Studio.renderProse = function (text, imageUrl) {
  const body = $("#reader-body");
  const paras = String(text || "").split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  const fig = imageUrl
    ? `<figure class="chapter-image"><img src="${esc(imageUrl)}" alt="Chapter illustration" loading="lazy" onerror="this.closest('figure').remove()"></figure>`
    : "";
  body.innerHTML = fig + (paras.map((p) => `<p>${esc(p)}</p>`).join("") || "<p></p>");
  // Reset the incremental-stream cursor (used by appendDelta).
  Studio._streamRendered = "";
};

// Is the reader scrolled close enough to the bottom that we should keep
// following the growing edge? (Don't yank the view if the reader scrolled up.)
Studio._nearBottom = function (body) {
  return body.scrollHeight - body.scrollTop - body.clientHeight < 80;
};

// Append only the *new* part of a streaming buffer instead of rebuilding the
// whole #reader-body from scratch on every token (which is O(n^2) and forces a
// full reflow of the drop-cap each frame).
Studio.appendDelta = function (full) {
  const body = $("#reader-body");
  const prev = Studio._streamRendered || "";
  full = String(full || "");
  // If the buffer was reset/replaced (shorter than what we rendered), fall back
  // to a full render to stay correct.
  if (!full.startsWith(prev)) { Studio.renderProse(full); Studio.showStreamSkeleton(); return; }
  const added = full.slice(prev.length);
  Studio._streamRendered = full;
  const wasNear = Studio._nearBottom(body);

  // The shimmer skeleton (if present) always trails the prose, so new <p>s must
  // be inserted BEFORE it — never appended after. `anchor` is that insert point.
  const skel = body.querySelector(".stream-skeleton");
  const insertBefore = (node) => { if (skel) body.insertBefore(node, skel); else body.appendChild(node); };

  // Ensure there is a trailing <p> (just before any skeleton) to append into.
  let last = skel ? skel.previousElementSibling : body.lastElementChild;
  if (!last || last.tagName !== "P") {
    last = document.createElement("p");
    insertBefore(last);
  }
  // Split the added text on paragraph breaks; everything before a break closes
  // the current <p>, the remainder starts a fresh one.
  const segments = added.split(/\n{2,}/);
  for (let i = 0; i < segments.length; i++) {
    if (i > 0) {
      last = document.createElement("p");
      insertBefore(last);
    }
    // Collapse single newlines to spaces within a paragraph.
    last.appendChild(document.createTextNode(segments[i].replace(/\n/g, " ")));
  }
  if (wasNear) body.scrollTop = body.scrollHeight;
};

// LIVE-WRITING CUE: shimmer skeleton lines that trail the streamed prose so the
// reader unmistakably reads as "generating" even in a still frame. Idempotent —
// only one skeleton block ever exists, and it always stays the LAST child so the
// caret (on the last <p>) and appendDelta's insert-before logic stay correct.
Studio.showStreamSkeleton = function () {
  const body = $("#reader-body");
  if (!body || !body.classList.contains("is-streaming")) return;
  let skel = body.querySelector(".stream-skeleton");
  if (!skel) {
    skel = document.createElement("div");
    skel.className = "stream-skeleton";
    skel.setAttribute("aria-hidden", "true");
    skel.innerHTML =
      '<span class="stream-skeleton-line"></span>' +
      '<span class="stream-skeleton-line"></span>' +
      '<span class="stream-skeleton-line"></span>';
  }
  // Keep it as the final child even after new paragraphs are appended.
  if (body.lastElementChild !== skel) body.appendChild(skel);
};

Studio.removeStreamSkeleton = function () {
  const body = $("#reader-body");
  if (!body) return;
  const skel = body.querySelector(".stream-skeleton");
  if (skel) skel.remove();
};

// Clear every "still generating" cue after a run ends without a chapter_done
// (an 'error' terminal, or a defensive sweep on 'done'): the shimmer skeleton,
// the reader's streaming class/aria-busy, the pulsing words label, and the
// outline item's spinner. Otherwise a failed run reads as writing forever.
Studio.stopStreamingUI = function () {
  const n = Studio.streamingNum;
  Studio.streamingNum = null;
  Studio.removeStreamSkeleton();
  const body = $("#reader-body");
  if (body) { body.classList.remove("is-streaming"); body.removeAttribute("aria-busy"); }
  const rw = $("#reader-words");
  if (rw) rw.classList.remove("is-writing");
  if (n != null) {
    const li = $(`#chapter-list .chapter-item[data-num="${n}"]`);
    if (li) li.classList.remove("is-writing");
  }
};

Studio.openChapter = async function (num) {
  Studio.active = num;
  $$("#chapter-list .chapter-item").forEach((li) =>
    li.classList.toggle("is-active", Number(li.dataset.num) === Number(num)));
  const body = $("#reader-body");
  body.classList.remove("is-streaming");
  $("#reader-flags").hidden = true;
  // Reset any open inline editor; hide the modify bar until a written chapter loads.
  Studio.cancelEdit();
  const acts = $("#reader-actions"); if (acts) acts.hidden = true;

  // streaming buffer takes precedence
  if (Studio.buffers[num] != null) {
    Studio.renderHeaderForChapter(num);
    Studio.renderProse(Studio.buffers[num]);
    if (Studio.streamingNum === num) {
      body.classList.add("is-streaming");
      // Mark how much is already rendered so appendDelta continues correctly.
      Studio._streamRendered = String(Studio.buffers[num] || "");
      // Re-attach the live-writing skeleton when returning to the streaming chapter.
      Studio.showStreamSkeleton();
    } else {
      body.removeAttribute("aria-busy");
    }
    return;
  }
  const meta = (Studio.data.chapters || []).find((c) => c.number === num);
  Studio.renderHeaderForChapter(num, meta);
  if (!meta || !meta.written) {
    body.innerHTML = `<div class="reader-placeholder"><p class="serif">Not written yet.</p><p>Chapter ${num} is outlined but not yet generated. Press <strong>Generate</strong> to write it.</p></div>`;
    return;
  }
  body.innerHTML = '<div class="sk-line w-80 skeleton"></div><div class="sk-line skeleton"></div><div class="sk-line w-60 skeleton"></div>';
  const myId = Studio.id, myNum = num;
  try {
    const ch = await API.chapter(myId, num);
    // Stale continuation: the user navigated to another book/chapter while this
    // one was loading — don't paint old data into the live view.
    if (Studio.id !== myId || Studio.active !== myNum) return;
    Studio.renderHeaderForChapter(num, { act: (ch.plan && ch.plan.act) || meta.act, title: ch.title, word_count: ch.word_count });
    Studio.renderProse(ch.text, ch.image_url);
    if (acts) acts.hidden = false;  // written → allow Edit / Revise
  } catch (err) {
    if (Studio.id !== myId || Studio.active !== myNum) return;
    body.innerHTML = `<p class="rail-empty">Couldn't load chapter: ${esc(err.message)}</p>`;
  }
};

Studio.renderHeaderForChapter = function (num, meta) {
  meta = meta || (Studio.data.chapters || []).find((c) => c.number === num) || {};
  $("#reader-act").textContent = `Act ${meta.act || 1}`;
  $("#reader-title").textContent = `${num}. ${meta.title || "Chapter " + num}`;
  const rwHdr = $("#reader-words");
  rwHdr.classList.remove("is-writing");
  rwHdr.textContent = meta.word_count ? `${fmtInt(meta.word_count)} words` : "";
};

/* ---- chapter modify: manual edit / AI revise / continue ---------------- */
Studio.bindModify = function () {
  const on = (sel, fn) => { const el = $(sel); if (el) el.addEventListener("click", fn); };
  on("#ch-edit", () => Studio.startEdit());
  on("#ch-revise", () => Studio.reviseChapter());
  on("#ch-edit-save", () => Studio.saveEdit());
  on("#ch-edit-cancel", () => Studio.cancelEdit());
  on("#add-chapters-btn", () => Studio.addChapters());
};

Studio.cancelEdit = function () {
  const ed = $("#reader-editor"); if (ed) ed.hidden = true;
  const body = $("#reader-body"); if (body) body.hidden = false;
};

Studio.startEdit = async function () {
  const num = Studio.active;
  const meta = (Studio.data.chapters || []).find((c) => c.number === num);
  if (!meta || !meta.written) { toast("Generate this chapter first, then edit it.", { type: "warn" }); return; }
  const ed = $("#reader-editor"), ta = $("#reader-editor-text");
  $("#reader-body").hidden = true; ed.hidden = false;
  ta.value = "Loading…"; ta.disabled = true;
  try {
    const ch = await API.chapter(Studio.id, num);
    ta.value = ch.text || ""; ta.disabled = false;
    $("#reader-editor-hint").textContent = `Editing chapter ${num} — your text is saved verbatim.`;
    ta.focus();
  } catch (err) { ta.value = ""; ta.disabled = false; toast(err.message || "Couldn't load chapter.", { type: "error" }); }
};

Studio.saveEdit = async function () {
  const num = Studio.active, ta = $("#reader-editor-text"), text = ta.value.trim();
  if (!text) { toast("A chapter can't be empty.", { type: "error" }); return; }
  const btn = $("#ch-edit-save"); btn.disabled = true;
  try {
    const r = await API.editChapter(Studio.id, num, { text });
    const meta = (Studio.data.chapters || []).find((c) => c.number === num);
    if (meta) { meta.word_count = r.word_count; meta.written = true; if (r.title) meta.title = r.title; }
    Studio.cancelEdit();
    Studio.renderChapters();
    Studio.renderProse(text);
    Studio.renderHeaderForChapter(num);
    toast("Chapter saved.", { title: "Saved", type: "good" });
  } catch (err) { toast(err.message || "Save failed.", { type: "error" }); }
  finally { btn.disabled = false; }
};

Studio.reviseChapter = async function () {
  const num = Studio.active;
  const meta = (Studio.data.chapters || []).find((c) => c.number === num);
  if (!meta || !meta.written) { toast("Generate this chapter first, then revise it.", { type: "warn" }); return; }
  const instructions = window.prompt(
    "How should the AI revise this chapter?\n(Leave blank to just polish the prose.)", "");
  if (instructions === null) return;
  const btn = $("#ch-revise"); btn.disabled = true; const label = btn.textContent; btn.textContent = "Revising…";
  try {
    const r = await API.reviseChapter(Studio.id, num, { instructions });
    if (meta) meta.word_count = r.word_count;
    Studio.renderChapters();
    const ch = await API.chapter(Studio.id, num);
    Studio.renderProse(ch.text, ch.image_url);
    Studio.renderHeaderForChapter(num, { act: meta && meta.act, title: ch.title, word_count: ch.word_count });
    toast("Chapter revised.", { title: "Revised", type: "good" });
  } catch (err) { toast(err.message || "Revision failed.", { type: "error" }); }
  finally { btn.disabled = false; btn.textContent = label; }
};

Studio.addChapters = async function () {
  const raw = window.prompt("Continue the story — how many new chapters to outline?\n(You'll then press Generate to write them.)", "3");
  if (raw === null) return;
  const count = Math.max(1, Math.min(40, parseInt(raw, 10) || 3));
  const btn = $("#add-chapters-btn"); btn.disabled = true; const label = btn.textContent; btn.textContent = "Adding…";
  try {
    const r = await API.appendChapters(Studio.id, { count });
    Studio.data = await API.book(Studio.id);
    Studio.renderChapters();
    const gen = $("#generate-btn"); if (gen) $(".btn-label", gen).textContent = "Resume";
    toast(`Added ${(r.added || []).length} chapter(s) — press Generate to write them.`, { title: "Outline extended", type: "good" });
  } catch (err) { toast(err.message || "Couldn't add chapters.", { type: "error" }); }
  finally { btn.disabled = false; btn.textContent = label; }
};

/* cost rail */
// Last-displayed values, so we can tween *from* them on the next snapshot.
Studio._cost = { total: 0, words: 0, per1k: 0, input: 0, output: 0, cache_read: 0, cache_write: 0 };

Studio.renderCost = function (snap, animate) {
  snap = snap || { total_cost: 0, words: 0, tokens: {}, cache_savings: 0 };
  const total = Number(snap.total_cost) || 0;
  const words = Number(snap.words) || 0;
  const per1k = words ? (total / words) * 1000 : 0;
  const tk = snap.tokens || {};
  const vals = {
    input: tk.input || 0, output: tk.output || 0,
    cache_read: tk.cache_read || 0, cache_write: tk.cache_write || 0,
  };
  const max = Math.max(1, vals.input, vals.output, vals.cache_read, vals.cache_write);
  const tokenTotal = vals.input + vals.output + vals.cache_read + vals.cache_write;

  // Writing progress (co-hero): derive chapters done / % from the current book.
  const chapters = (Studio.data && Studio.data.chapters) || [];
  const chTotal = chapters.length;
  const chDone = chapters.filter((c) => c.written).length;
  const pct = chTotal ? Math.round((chDone / chTotal) * 100) : 0;
  const chEl = $("#cost-chapters"), pctEl = $("#cost-pct"), progFill = $("#cost-progress-fill");
  if (chEl) chEl.textContent = `${fmtInt(chDone)} of ${fmtInt(chTotal)}`;
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (progFill) progFill.style.width = `${pct}%`;
  const tokTotalEl = $("#token-total");
  if (tokTotalEl) tokTotalEl.textContent = fmtTokens(tokenTotal);

  const totalEl = $("#cost-total"), totalFineEl = $("#cost-total-fine");
  const per1kEl = $("#cost-per1k"), wordsEl = $("#cost-words");
  // Headline shows a clean 2dp figure; the precise 4dp figure sits quietly
  // beneath it so we de-emphasize raw precision without removing the data.
  const setTotal = (v) => {
    if (totalEl) totalEl.textContent = fmtMoney(v, 2);
    if (totalFineEl) totalFineEl.textContent = fmtMoney(v, 4);
  };
  const setPer1k = (v) => { if (per1kEl) per1kEl.textContent = fmtMoney(v, 4); };
  const setWords = (v) => { if (wordsEl) wordsEl.textContent = fmtInt(Math.round(v)); };

  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (animate && !reduce) {
    tweenNumber(Studio._cost.total, total, 650, setTotal);
    tweenNumber(Studio._cost.per1k, per1k, 650, setPer1k);
    tweenNumber(Studio._cost.words, words, 650, setWords);
  } else {
    setTotal(total); setPer1k(per1k); setWords(words);
  }
  Studio._cost.total = total; Studio._cost.per1k = per1k; Studio._cost.words = words;

  for (const k of Object.keys(vals)) {
    const bar = $(`.token-bar[data-k="${k}"]`);
    if (!bar) continue;
    // width is CSS-transitioned already; just set the target.
    $(".tb-fill", bar).style.width = `${(vals[k] / max) * 100}%`;
    const valEl = $(".tb-val", bar);
    if (animate && !reduce) {
      tweenNumber(Studio._cost[k] || 0, vals[k], 600, (v) => { valEl.textContent = fmtTokens(Math.round(v)); });
    } else {
      valEl.textContent = fmtTokens(vals[k]);
    }
    Studio._cost[k] = vals[k];
  }

  const savings = Number(snap.cache_savings) || 0;
  const sv = $("#cache-savings");
  if (savings > 0) { sv.hidden = false; $("#savings-val").textContent = fmtMoney(savings, 4); }
  else sv.hidden = true;
};

// Animated tick toward a target total cost / words while streaming.
Studio.tickCost = function (snap) {
  Studio.renderCost(snap, true);
};

Studio.renderGraphRail = async function () {
  const castEl = $("#cast-list"), threadEl = $("#thread-list");
  const myId = Studio.id;
  try {
    const g = await API.graph(myId);
    if (Studio.id !== myId) return; // navigated away mid-fetch — don't paint stale data
    Studio._graph = g;
    const cast = (g.characters || []);
    castEl.innerHTML = cast.length ? "" : '<li class="rail-empty">No characters yet.</li>';
    for (const c of cast.slice(0, 12)) {
      const li = document.createElement("li");
      li.className = "cast-item";
      const dead = c.status && c.status !== "active";
      li.innerHTML =
        `<span class="cast-avatar" style="background:${avatarColor(c.id || c.name)}">${esc(initials(c.name))}</span>` +
        `<span class="cast-main"><span class="cast-name">${esc(c.name)}</span><span class="cast-role">${esc(c.role || "—")}</span></span>` +
        (dead ? `<span class="cast-status is-dead">${esc(c.status)}</span>` : "");
      castEl.appendChild(li);
    }
    const open = (g.threads || []).filter((t) => t.status !== "resolved");
    threadEl.innerHTML = open.length ? "" : '<li class="rail-empty">No open threads.</li>';
    for (const t of open.slice(0, 10)) {
      const li = document.createElement("li");
      li.className = "thread-pill";
      li.innerHTML = `<b>${esc(t.name)}</b>${t.description ? esc(t.description) : ""}`;
      threadEl.appendChild(li);
    }
  } catch {
    if (Studio.id !== myId) return;
    castEl.innerHTML = '<li class="rail-empty">Continuity unavailable.</li>';
    threadEl.innerHTML = "";
  }
};

/* ---- generate / SSE ---- */
Studio.onGenerate = async function () {
  const btn = $("#generate-btn");
  if (Studio.running) { toast("A generation is already running for this book.", { type: "info" }); return; }
  btn.classList.add("is-busy"); btn.disabled = true;
  try {
    await API.write(Studio.id, {});
    Studio.running = true;
    Studio.startedLive = true;   // replayed events for THIS run may follow/celebrate
    toast("Generation started — chapters will stream in live.", { title: "Writing", type: "good" });
    $(".btn-label", btn).textContent = "Writing…";
    // ensure event stream is live (connect is idempotent-ish)
    Studio.connect(true);
  } catch (err) {
    if (err.status === 409) {
      Studio.running = true;
      Studio.startedLive = true; // an actual run is in flight — treat as live
      toast("Already running — re-attaching to the live stream.", { type: "info" });
      Studio.connect(true);
      $(".btn-label", btn).textContent = "Writing…";
    } else {
      toast(err.message || "Could not start generation.", { title: "Error", type: "error" });
      btn.classList.remove("is-busy"); btn.disabled = false;
    }
  }
};

Studio.connect = function (force) {
  if (Studio.es && !force) return;
  if (Studio.es) { try { Studio.es.close(); } catch {} Studio.es = null; }
  const es = new EventSource(`/api/books/${Studio.id}/events`);
  Studio.es = es;
  es.onmessage = (ev) => {
    if (!ev.data || ev.data[0] !== "{") return;
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    Studio.handleEvent(msg);
  };
  es.onerror = () => {
    // Reconnect policy: while a run is (or may be) in flight, let the browser's
    // built-in EventSource retry handle transient drops — no noisy toast. If the
    // browser gave up for good (readyState CLOSED, e.g. the server answered 204
    // or a fatal error once the last job finished), drop our handle so a later
    // Studio.connect() can re-attach cleanly.
    if (Studio.es === es && es.readyState === EventSource.CLOSED) {
      Studio.es = null;
    }
  };
};

Studio.markRunning = function (on) {
  Studio.running = on;
  const btn = $("#generate-btn");
  if (!btn) return;
  if (on) { btn.classList.add("is-busy"); btn.disabled = true; $(".btn-label", btn).textContent = "Writing…"; }
  else { btn.classList.remove("is-busy"); btn.disabled = false; $(".btn-label", btn).textContent = "Resume"; }
};

Studio.handleEvent = function (msg) {
  switch (msg.type) {
    case "plan_done":
      // a fresh plan replaced the bible; refresh shell + rails. Replayed
      // history of a finished book carries stale intermediate cost snapshots —
      // the mount already rendered the final cost, so don't tween backwards.
      if (!Studio.startedLive && Studio.initiallyComplete) break;
      if (msg.cost) Studio.tickCost(msg.cost);
      break;

    case "chapter_start": {
      // A reconnect replays the last job's entire history. If this book was
      // already fully written when the studio mounted (and this session didn't
      // start a run itself), ignore replayed writing events entirely — no
      // button flip, no auto-follow, no blanking the reader pane, and no
      // streaming buffers: a leftover buffer would shadow the canonical
      // chapter (image + Edit/Revise included) in openChapter from then on.
      if (!Studio.startedLive && Studio.initiallyComplete) break;
      Studio.streamingNum = msg.number;
      Studio.buffers[msg.number] = "";
      Studio.markRunning(true);
      Studio.setChapterStatus(msg.number, "writing");
      srStatus(`Writing chapter ${msg.number}${msg.title ? ": " + msg.title : ""}`);
      // auto-follow the chapter being written
      Studio.openChapter(msg.number);
      Studio.renderHeaderForChapter(msg.number, { act: msg.act, title: msg.title, word_count: 0 });
      const rwStart = $("#reader-words");
      rwStart.classList.add("is-writing");
      rwStart.textContent = "Writing…";
      const body = $("#reader-body");
      body.classList.add("is-streaming");
      body.setAttribute("aria-busy", "true");
      body.innerHTML = "";
      Studio._streamRendered = "";
      $("#reader-flags").hidden = true;
      // Always-visible "words appearing now" cue, present from the very first
      // frame of the chapter (before any token arrives).
      Studio.showStreamSkeleton();
      break;
    }

    case "delta": {
      // Replayed history of a finished book: ignore (see chapter_start).
      if (!Studio.startedLive && Studio.initiallyComplete) break;
      const n = msg.number;
      Studio.buffers[n] = (Studio.buffers[n] || "") + (msg.text || "");
      if (Studio.active === n) {
        const body = $("#reader-body");
        body.classList.add("is-streaming");
        // Append only the new tokens (no full innerHTML rebuild per delta).
        Studio.appendDelta(Studio.buffers[n]);
        // Keep the shimmer skeleton trailing the freshest prose.
        Studio.showStreamSkeleton();
        const wc = (Studio.buffers[n].trim().match(/\S+/g) || []).length;
        const rwDelta = $("#reader-words");
        rwDelta.classList.add("is-writing");
        rwDelta.textContent = `Writing · ${fmtInt(wc)} words`;
      }
      break;
    }

    case "chapter_done": {
      // Replayed history of a finished book: the mount already rendered every
      // chapter as written with the final meta — ignore (see chapter_start).
      if (!Studio.startedLive && Studio.initiallyComplete) break;
      const n = msg.number;
      Studio.buffers[n] = msg.text || Studio.buffers[n] || "";
      Studio.streamingNum = null;
      Studio.setChapterStatus(n, "written");
      srStatus(`Chapter ${n} complete, ${fmtInt(msg.words || 0)} words`);
      // update local chapter meta
      const meta = (Studio.data.chapters || []).find((c) => c.number === n);
      if (meta) { meta.written = true; meta.word_count = msg.words || 0; meta.title = msg.title || meta.title; }
      Studio.updateProgress();
      if (Studio.active === n) {
        const body = $("#reader-body");
        body.classList.remove("is-streaming");
        body.removeAttribute("aria-busy");
        // Remove the live-writing skeleton, then do the full clean render
        // (renderProse rebuilds innerHTML, but remove explicitly for clarity).
        Studio.removeStreamSkeleton();
        // Full, clean render now that the chapter is final (drop-cap re-enabled).
        Studio.renderProse(msg.text, msg.image ? `/api/books/${Studio.id}/chapters/${n}/image` : "");
        const rwDone = $("#reader-words");
        rwDone.classList.remove("is-writing");
        rwDone.textContent = `${fmtInt(msg.words || 0)} words`;
        Studio.renderFlags(msg.flags);
        // brief completion flourish on the chapter's outline node
        const li = $(`#chapter-list .chapter-item[data-num="${n}"]`);
        if (li) { li.classList.add("just-done"); setTimeout(() => li.classList.remove("just-done"), 1200); flourish($(".ci-num", li)); }
      }
      if (msg.cost) Studio.tickCost(msg.cost);
      // refresh continuity rail (new characters/threads may have appeared)
      Studio.renderGraphRail();
      break;
    }

    case "manuscript_done": {
      // Replayed history of an already-finished book: no re-celebration.
      if (!Studio.startedLive && Studio.initiallyComplete) break;
      if (msg.cost) Studio.tickCost(msg.cost);
      if (Studio.running) Studio.markRunning(false);
      toast(`Manuscript complete — ${fmtInt(msg.words)} words.`, { title: "Done", type: "good" });
      // grand flourish over the hero cover, and an ember pulse on the progress bar
      const cover = $("#studio-cover");
      if (cover) { cover.classList.add("is-finished"); setTimeout(() => cover.classList.remove("is-finished"), 1800); flourish(cover, { grand: true }); }
      const prog = $("#studio-progress-fill");
      if (prog) { prog.classList.add("is-complete"); }
      break;
    }

    case "done":
      if (Studio.running) Studio.markRunning(false);
      // Defensive sweep: no "still writing" cue should outlive a finished run.
      Studio.stopStreamingUI();
      // The server ends the stream after a terminal event; without an explicit
      // close, EventSource reconnects forever and replays the whole job history
      // every ~3s. Studio.connect(true) in onGenerate re-attaches for new runs.
      Studio.disconnect();
      break;

    case "error":
      if (Studio.running) Studio.markRunning(false);
      // A mid-chapter failure otherwise leaves the skeleton shimmer, aria-busy,
      // pulsing words label, and outline spinner stuck in "writing" forever.
      Studio.stopStreamingUI();
      Studio.disconnect();
      if (Studio.startedLive || !Studio.initiallyComplete) {
        toast(msg.message || "Generation failed.", { title: "Error", type: "error" });
      }
      break;
  }
};

Studio.renderFlags = function (flags) {
  const el = $("#reader-flags");
  if (!flags || !flags.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = flags.map((f) => {
    const s = String(f);
    let cls = "flag-low";
    if (/^\[high\]/i.test(s) || /high/i.test(s)) cls = "flag-high";
    else if (/^\[med/i.test(s) || /medium/i.test(s)) cls = "flag-med";
    const text = s.replace(/^\[[^\]]*\]\s*/, "");
    return `<span class="flag-badge ${cls}">${esc(text || s)}</span>`;
  }).join("");
};

/* ============================== GRAPH ================================== */
Views.graph = async function (id) {
  setActiveNav("");
  const view = tpl("tpl-graph");
  mountView(view);
  $("#graph-back", view).setAttribute("href", `#/b/${id}`);

  const svg = $("#graph-svg", view);
  svg.innerHTML = '<text x="50%" y="50%" class="graph-empty" text-anchor="middle">Loading the web…</text>';
  try {
    const [g, book] = await Promise.all([API.graph(id), API.book(id).catch(() => null)]);
    if (book && book.book) $("#graph-book-title", view).textContent = book.book.title || "Story graph";
    renderGraphSVG(svg, g);
    renderThreadBoard($("#graph-threads", view), g.threads || []);
  } catch (err) {
    svg.innerHTML = `<text x="50%" y="50%" class="graph-empty" text-anchor="middle">${esc(err.message)}</text>`;
  }
};

function renderGraphSVG(svg, g) {
  const chars = (g.characters || []);
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  // A flatter canvas (was 900x440) so the card hugs the cast instead of leaving a
  // tall empty band above/below; threads then sit comfortably below the stage.
  const W = 900, H = 400;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!chars.length) {
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("x", "50%"); t.setAttribute("y", "50%");
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "graph-empty");
    t.textContent = "No characters to map yet.";
    svg.appendChild(t);
    return;
  }

  // radial layout: protagonist-ish nodes nearer the centre
  const cx = W / 2, cy = H / 2;
  const N = chars.length;
  // Sparse casts (1-2 nodes) get larger discs so the hero stage doesn't read as
  // a near-empty backdrop on first load.
  const sparse = N <= 2;
  const sizeBoost = sparse ? 1.5 : 1;
  const idIndex = {};
  const roleOf = (c) =>
    /protagonist|hero|lead/i.test(c.role || "") ? "pro"
    : (/antagonist|villain/i.test(c.role || "") ? "ant" : "sup");

  // Distribute supporting/antagonist nodes EVENLY around an ellipse so the web
  // never collapses onto a near-horizontal line. We lay out the non-protagonist
  // ring first at even angular steps (with a small rotation offset so a 2-node
  // cast sits diagonally, not flat), then place protagonists nearer the centre.
  const ringRoles = chars.map(roleOf);
  const ringIdx = []; // indices of nodes that sit on the outer ring
  chars.forEach((c, i) => { if (ringRoles[i] !== "pro") ringIdx.push(i); });
  const ringN = ringIdx.length || 1;
  // Start near the top and add a fixed tilt so even-count rings (esp. 2 nodes)
  // never align flat with the horizontal/vertical axes — the web stays balanced.
  const rot = -Math.PI / 2 + Math.PI / ringN + 0.34;
  const ringAngle = {};
  ringIdx.forEach((idx, k) => { ringAngle[idx] = rot + (k / ringN) * Math.PI * 2; });

  const proList = chars.map((c, i) => i).filter((i) => ringRoles[i] === "pro");

  const nodes = chars.map((c, i) => {
    idIndex[c.id] = i;
    const role = ringRoles[i];
    const isPro = role === "pro";
    let x, y;
    if (isPro) {
      // Protagonists sit in a small inner cluster near the centre (dead-centre if
      // there's exactly one) so the eye lands on the lead first.
      if (proList.length === 1) { x = cx; y = cy; }
      else {
        const k = proList.indexOf(i);
        const a = -Math.PI / 2 + (k / proList.length) * Math.PI * 2;
        x = cx + Math.cos(a) * 0.13 * W;
        y = cy + Math.sin(a) * 0.18 * H;
      }
    } else {
      // Even angular ellipse for the supporting cast. The vertical radius is kept
      // close to the horizontal one (relative to the flat canvas) so nodes spread
      // into a genuine ring rather than a squashed horizontal band.
      const a = ringAngle[i];
      x = cx + Math.cos(a) * 0.40 * W;
      y = cy + Math.sin(a) * 0.40 * H;
    }
    return {
      c, x, y,
      r: (isPro ? 34 : 27) * sizeBoost,
      cls: isPro ? "node-pro" : (role === "ant" ? "node-ant" : "node-sup"),
    };
  });

  const NS = "http://www.w3.org/2000/svg";
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // edges from relationships
  const edges = [];
  chars.forEach((c) => {
    const rels = c.relationships || {};
    Object.keys(rels).forEach((other) => {
      if (idIndex[other] == null) return;
      const a = idIndex[c.id], b = idIndex[other];
      if (a < b) edges.push({ a, b, label: rels[other] });
      else if (!(rels[other] && (chars[b].relationships || {})[c.id])) edges.push({ a, b, label: rels[other] });
    });
  });

  // A faint radial backdrop ring set so even a 2-node graph feels composed.
  const gBack = document.createElementNS(NS, "g");
  gBack.setAttribute("class", "graph-rings");
  [[0.18, 0.17], [0.29, 0.26], [0.40, 0.35]].forEach(([fx, fy]) => {
    const ring = document.createElementNS(NS, "ellipse");
    ring.setAttribute("cx", cx); ring.setAttribute("cy", cy);
    ring.setAttribute("rx", (W * fx).toFixed(1)); ring.setAttribute("ry", (H * fy).toFixed(1));
    ring.setAttribute("class", "graph-ring");
    gBack.appendChild(ring);
  });
  svg.appendChild(gBack);

  // Curved relationship edges (quadratic Béziers bowed away from centre) so
  // overlapping relationships stay readable and the web feels organic.
  //
  // READABILITY MODEL: at rest the web shows ONLY the connectors (no text) so a
  // dense 7-node / ~20-edge graph stays legible. Each edge's relationship label
  // is built as a solid filled PILL CHIP but kept hidden until its node is
  // hovered/focused — then we reveal only that node's chips. We stash the chip +
  // its anchor on the edge record so the focus handler can position/show them.
  const gEdges = document.createElementNS(NS, "g");
  edges.forEach((e) => {
    const n1 = nodes[e.a], n2 = nodes[e.b];
    const mx = (n1.x + n2.x) / 2, my = (n1.y + n2.y) / 2;
    // bow the control point outward from the graph centre
    let bx = mx - cx, by = my - cy;
    const blen = Math.hypot(bx, by) || 1;
    const bow = 26;
    const qx = mx + (bx / blen) * bow, qy = my + (by / blen) * bow;
    const path = document.createElementNS(NS, "path");
    const d = `M${n1.x.toFixed(1)} ${n1.y.toFixed(1)} Q ${qx.toFixed(1)} ${qy.toFixed(1)} ${n2.x.toFixed(1)} ${n2.y.toFixed(1)}`;
    path.setAttribute("d", d);
    path.setAttribute("class", "graph-edge");
    path.dataset.a = e.a; path.dataset.b = e.b;
    if (!reduce) {
      const len = path.getTotalLength ? path.getTotalLength() : 300;
      path.style.strokeDasharray = len.toFixed(0);
      path.style.strokeDashoffset = len.toFixed(0);
      path.style.transition = "stroke-dashoffset .7s var(--ease) " + (0.12 * Math.min(e.a, e.b)).toFixed(2) + "s, stroke var(--dur) var(--ease), stroke-width var(--dur) var(--ease)";
      requestAnimationFrame(() => { path.style.strokeDashoffset = "0"; });
    }
    gEdges.appendChild(path);
  });
  svg.appendChild(gEdges);

  // Relationship-label chips: a SEPARATE layer painted above the nodes so a
  // revealed pill is never occluded by a disc. Hidden by default (.is-hidden);
  // the focus handler shows only the chips incident to the active node.
  const gChips = document.createElementNS(NS, "g");
  gChips.setAttribute("class", "graph-chips");
  edges.forEach((e) => {
    if (!e.label) { e.chip = null; return; }
    const n1 = nodes[e.a], n2 = nodes[e.b];
    const mx = (n1.x + n2.x) / 2, my = (n1.y + n2.y) / 2;
    let bx = mx - cx, by = my - cy;
    const blen = Math.hypot(bx, by) || 1;
    const lx = mx + (bx / blen) * 30, ly = my + (by / blen) * 30 - 2;
    const text = String(e.label).slice(0, 28);
    const chip = document.createElementNS(NS, "g");
    chip.setAttribute("class", "graph-edge-chip is-hidden");
    chip.dataset.a = e.a; chip.dataset.b = e.b;
    const pill = document.createElementNS(NS, "rect");
    const w = Math.max(34, text.length * 6.1 + 18), h = 19;
    pill.setAttribute("x", (lx - w / 2).toFixed(1));
    pill.setAttribute("y", (ly - h / 2).toFixed(1));
    pill.setAttribute("width", w.toFixed(1)); pill.setAttribute("height", h);
    pill.setAttribute("rx", "9.5"); pill.setAttribute("class", "graph-edge-pill");
    chip.appendChild(pill);
    const lbl = document.createElementNS(NS, "text");
    lbl.setAttribute("x", lx.toFixed(1)); lbl.setAttribute("y", (ly + 0.5).toFixed(1));
    lbl.setAttribute("dominant-baseline", "middle");
    lbl.setAttribute("text-anchor", "middle"); lbl.setAttribute("class", "graph-edge-label");
    lbl.textContent = text;
    chip.appendChild(lbl);
    gChips.appendChild(chip);
    e.chip = chip;
  });

  // nodes
  nodes.forEach((n, i) => {
    const grp = document.createElementNS(NS, "g");
    grp.setAttribute("class", "graph-node");
    grp.setAttribute("transform", `translate(${n.x},${n.y})`);
    grp.setAttribute("tabindex", "0");
    grp.setAttribute("role", "img");
    grp.setAttribute("aria-label", `${n.c.name}${n.c.role ? ", " + n.c.role : ""}`);
    // soft halo behind each node
    const halo = document.createElementNS(NS, "circle");
    halo.setAttribute("r", n.r + 7); halo.setAttribute("class", "node-halo");
    grp.appendChild(halo);
    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("r", n.r); circle.setAttribute("class", n.cls);
    grp.appendChild(circle);
    const ini = document.createElementNS(NS, "text");
    ini.setAttribute("dy", "0.35em");
    ini.setAttribute("font-size", String(n.r >= 33 ? 18 : 15)); ini.setAttribute("class", "node-initials");
    ini.textContent = initials(n.c.name);
    grp.appendChild(ini);
    const name = document.createElementNS(NS, "text");
    name.setAttribute("y", n.r + 17); name.setAttribute("font-size", "12");
    name.setAttribute("class", "node-name");
    name.textContent = n.c.name;
    grp.appendChild(name);
    // On hover/focus: highlight this node's edges, dim everything else, and
    // REVEAL the relationship labels for only this node's connections (each a
    // solid pill). At rest no labels show, so the web stays clean and legible.
    const hot = (on) => {
      svg.classList.toggle("has-focus", on);
      // Edge strokes: light up incident edges.
      gEdges.querySelectorAll(".graph-edge").forEach((ln) => {
        const inc = Number(ln.dataset.a) === i || Number(ln.dataset.b) === i;
        ln.classList.toggle("is-hot", on && inc);
      });
      // Label chips: show + highlight only this node's; hide all others.
      gChips.querySelectorAll(".graph-edge-chip").forEach((ch) => {
        const inc = Number(ch.dataset.a) === i || Number(ch.dataset.b) === i;
        ch.classList.toggle("is-hidden", !(on && inc));
        ch.classList.toggle("is-hot", on && inc);
      });
      // dim non-incident nodes
      svg.querySelectorAll(".graph-node").forEach((gn) => gn.classList.remove("is-dim", "is-focus"));
      if (on) {
        const connected = new Set([i]);
        edges.forEach((e) => { if (e.a === i) connected.add(e.b); if (e.b === i) connected.add(e.a); });
        svg.querySelectorAll(".graph-node").forEach((gn, gi) => {
          if (!connected.has(gi)) gn.classList.add("is-dim");
          if (gi === i) gn.classList.add("is-focus");
        });
      }
    };
    grp.addEventListener("mouseenter", () => hot(true));
    grp.addEventListener("mouseleave", () => hot(false));
    grp.addEventListener("focus", () => hot(true));
    grp.addEventListener("blur", () => hot(false));
    // settle-in entrance: nodes scale + fade from the centre. Animate via the
    // CSS `transform` *style* (reliable across engines) rather than tweening the
    // SVG presentation attribute, which is historically flaky in WebKit.
    if (!reduce) {
      grp.removeAttribute("transform");
      grp.style.transformBox = "fill-box";
      grp.style.transformOrigin = "center";
      grp.style.opacity = "0";
      grp.style.transform = `translate(${cx}px,${cy}px) scale(.4)`;
      grp.style.transition = "opacity .5s var(--ease), transform .6s var(--ease-spring, var(--ease))";
      const delay = 80 + 70 * i;
      setTimeout(() => {
        grp.style.opacity = "1";
        grp.style.transform = `translate(${n.x}px,${n.y}px) scale(1)`;
      }, delay);
    }
    svg.appendChild(grp);
  });

  // Chips paint last so a revealed pill always sits above the discs + edges.
  svg.appendChild(gChips);

  // A graceful low-count line so a 1-2 character book still feels composed.
  if (sparse) {
    const cap = document.createElementNS(NS, "text");
    cap.setAttribute("x", cx); cap.setAttribute("y", H - 28);
    cap.setAttribute("text-anchor", "middle");
    cap.setAttribute("class", "graph-grow-note");
    cap.textContent = "The cast will grow as you write.";
    svg.appendChild(cap);
  }
}

function renderThreadBoard(el, threads) {
  if (!threads.length) { el.innerHTML = '<li class="rail-empty">No plot threads recorded.</li>'; return; }
  el.innerHTML = "";
  threads.forEach((t) => {
    const li = document.createElement("li");
    const resolved = t.status === "resolved";
    li.className = "thread-card" + (resolved ? " is-resolved" : "");
    li.innerHTML =
      `<span class="thread-status">${esc(t.status || "open")}</span>` +
      `<h3>${esc(t.name)}</h3>` +
      (t.description ? `<p>${esc(t.description)}</p>` : "");
    el.appendChild(li);
  });
}

/* ============================ MANUSCRIPT =============================== */
/* The Manuscript view is an interactive 3D page-turn book reader. The procedural
   cover is page 0; opening it turns to a two-page spread (single page on mobile).
   Pages are paginated by measuring into a hidden page-sized box and filling
   paragraph-by-paragraph (word-by-word for an overflowing final paragraph) so
   NO text is ever clipped. A "Plain view" toggle drops to the classic scrolled
   column (assistive-tech / scrolling escape hatch). All animation is gated behind
   prefers-reduced-motion. */
Views.manuscript = async function (id) {
  setActiveNav("");
  Reader.teardown();
  const view = tpl("tpl-manuscript");
  mountView(view);
  $("#ms-back", view).setAttribute("href", `#/b/${id}`);
  $("#ms-download", view).setAttribute("href", `/api/books/${id}/manuscript?download=1`);
  const msPublish = $("#ms-publish", view);
  if (msPublish) msPublish.setAttribute("href", `#/b/${id}/publish`);

  const book = $("#reader-book", view);
  book.innerHTML = '<div class="reader-loading"><div class="sk-line w-40 skeleton" style="margin:0 auto 2em;height:2.2em;"></div>' +
    Array.from({ length: 6 }).map(() => '<div class="sk-line skeleton"></div><div class="sk-line w-80 skeleton"></div>').join("") + '</div>';

  try {
    const [ms, bookData] = await Promise.all([API.manuscript(id), API.book(id).catch(() => null)]);
    const words = Number(ms.words) || 0;
    const readMin = Math.max(1, Math.round(words / 230)); // ~230 wpm
    const bk = (bookData && bookData.book) || { id, title: "Untitled" };
    const chapters = bookData ? (bookData.chapters || []).filter((c) => c.written).length : 0;
    const title = bk.title || "Manuscript";
    const genre = bk.genre || "Manuscript";

    $("#ms-title", view).textContent = title;
    $("#ms-meta", view).textContent = `${genre} · ${fmtInt(words)} words`;
    document.title = `${title} · Bookwriter Pro`;

    // Colophon parts (shared by plain view + the book's colophon page).
    const colParts = [];
    if (bk.genre) colParts.push(esc(bk.genre));
    if (words) colParts.push(`${fmtInt(words)} words`);
    if (chapters) colParts.push(`${chapters} chapter${chapters === 1 ? "" : "s"}`);
    if (words) colParts.push(`≈ ${readMin} min read`);
    const colophonHtml = colParts.join(' <span class="colophon-sep">·</span> ');

    // ----- Plain view (kept; the simple scrolled column) -----
    const colophonEl = $("#manuscript-colophon", view);
    paintCover($("#manuscript-cover", view), { id: bk.id || id, title: bk.title, genre: bk.genre, logline: bk.logline });
    if (colophonEl && colophonHtml) { colophonEl.innerHTML = colophonHtml; colophonEl.hidden = false; }
    const plainBody = stripLeadingTitle(ms.markdown || "");
    const paper = $("#manuscript-paper", view);
    paper.innerHTML = (ms.markdown && ms.words)
      ? renderMarkdown(plainBody)
      : '<p class="rail-empty" style="text-align:center;font-family:var(--font-text)">No chapters written yet. Generate the book first, then come back to read it whole.</p>';

    // ----- Book reader -----
    Reader.mount(view, {
      id: bk.id || id, title, genre, logline: bk.logline,
      markdown: ms.markdown || "", words, colophonHtml,
      hasContent: !!(ms.markdown && ms.words),
    });
  } catch (err) {
    $("#reader-book", view).innerHTML = `<p class="rail-empty">${esc(err.message)}</p>`;
    const tray = $("#reader-tray", view); if (tray) tray.hidden = true;
  }
};

/* --------------------- Book reader (3D page-turn) ---------------------- */
const Reader = {
  state: null,

  teardown() {
    if (Reader.state && Reader.state.cleanup) Reader.state.cleanup();
    Reader.state = null;
  },

  mount(view, meta) {
    const reduce = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Default to the book reader on desktop; on narrow/touch screens (or with
    // assistive tech preferring it) the plain scrolled view is the better default.
    const wantsBook = window.matchMedia ? window.matchMedia("(min-width: 760px)").matches : true;

    const els = {
      view,
      mode: $("#ms-mode", view),
      stage: $("#reader-stage", view),
      bookEl: $("#reader-book", view),
      prevBtn: $("#reader-prev", view),
      nextBtn: $("#reader-next", view),
      tray: $("#reader-tray", view),
      scrubber: $("#reader-scrubber", view),
      readout: $("#reader-readout", view),
      plain: $("#manuscript-plain", view),
    };

    const st = {
      meta,
      els,
      tokens: parseManuscript(meta.markdown),
      pages: [],        // array of {type, html, label}
      index: 0,         // current LEAF index (0 = cover)
      twoUp: false,
      isBook: false,    // currently showing book vs plain
      animating: false,
      listeners: [],
      ro: null,
      resizeT: 0,
      cleanup() {
        this.listeners.forEach(([t, e, fn]) => t.removeEventListener(e, fn));
        this.listeners = [];
        if (this.ro) { try { this.ro.disconnect(); } catch {} this.ro = null; }
        if (this.resizeT) clearTimeout(this.resizeT);
      },
    };
    Reader.state = st;

    const on = (t, e, fn, opts) => { t.addEventListener(e, fn, opts); st.listeners.push([t, e, fn]); };

    // ----- Plain <-> Book toggle -----
    const setMode = (book) => {
      st.isBook = book;
      els.plain.hidden = book;
      els.stage.hidden = !book;
      els.tray.hidden = !book || st.pages.length <= 1;
      els.mode.setAttribute("aria-pressed", String(!book));
      els.mode.textContent = book ? "Plain view" : "Book view";
      if (book) { paginate(); }
    };
    on(els.mode, "click", () => setMode(!st.isBook));

    // ----- pagination (the hard part; never clips) -----
    function paginate() {
      const wide = window.matchMedia && window.matchMedia("(min-width: 900px)").matches;
      st.twoUp = wide;
      els.bookEl.classList.toggle("is-twoup", wide);

      if (!meta.hasContent) {
        st.pages = [{ type: "cover" }];
        render(true);
        return;
      }
      // Measure into a hidden page sized exactly like a live page's content box.
      const probe = buildProbe(els.bookEl, wide);
      try {
        st.pages = layoutPages(st.tokens, probe.box, meta);
      } finally {
        probe.host.remove();
      }
      // Prepend the cover (leaf 0) and append a colophon end-leaf.
      st.pages.unshift({ type: "cover" });
      st.pages.push({ type: "colophon", html: meta.colophonHtml });
      // Clamp current index after a re-paginate.
      if (st.index >= st.pages.length) st.index = st.pages.length - 1;
      render(true);
    }

    // Build an offscreen measuring page matching the live content box.
    function buildProbe(bookEl, wide) {
      const host = document.createElement("div");
      host.className = "reader-probe-host";
      // Render a real (hidden) spread/page to read the true content box size.
      host.innerHTML = wide
        ? '<div class="reader-spread"><div class="reader-leaf left"><div class="leaf-face"><div class="page-content"></div></div></div><div class="reader-leaf right"><div class="leaf-face"><div class="page-content"></div></div></div></div>'
        : '<div class="reader-spread is-single"><div class="reader-leaf right"><div class="leaf-face"><div class="page-content"></div></div></div></div>';
      bookEl.appendChild(host);
      const box = host.querySelector(".page-content");
      return { host, box };
    }

    // ----- navigation -----
    // A "leaf step" is one page; on two-up we step by spreads (2 leaves). The
    // cover is its own spread; content spreads pair as [1,2],[3,4]…
    // Left index of the spread that contains leaf i (cover is its own spread).
    const pairLeft = (i) => (i <= 0 ? 0 : (i % 2 === 1 ? i : i - 1));
    const lastIndex = () => st.pages.length - 1;
    const atStart = () => st.index <= 0;
    // In two-up the final reachable spread is the pair containing the last leaf.
    const atEnd = () => st.index >= (st.twoUp ? pairLeft(lastIndex()) : lastIndex());

    function go(target, dir) {
      if (st.animating) return;
      target = Math.max(0, Math.min(lastIndex(), target));
      // In two-up, normalize to the spread's left leaf so every position is a
      // distinct spread (cover=0, then [1,2],[3,4]…) — no redundant half-steps.
      if (st.twoUp) target = pairLeft(target);
      if (target === st.index) { render(); return; }
      const prev = st.index;
      st.index = target;
      animateTurn(prev, target, dir);
    }

    function next() {
      if (atEnd()) return;
      let target;
      if (st.twoUp) target = st.index === 0 ? 1 : pairLeft(st.index) + 2;
      else target = st.index + 1;
      if (target > lastIndex()) { if (atEnd()) return; target = lastIndex(); }
      go(target, 1);
    }
    function prev() {
      if (atStart()) return;
      let target;
      if (st.twoUp) target = st.index <= 2 ? 0 : pairLeft(st.index) - 2;
      else target = st.index - 1;
      go(target, -1);
    }

    on(els.nextBtn, "click", next);
    on(els.prevBtn, "click", prev);

    // Click zones on the book itself (left third = prev, right third = next).
    on(els.bookEl, "click", (e) => {
      if (e.target.closest("a")) return; // don't hijack links
      const rect = els.bookEl.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      if (st.index === 0) { next(); return; } // tapping the cover opens it
      if (x < 0.34) prev(); else if (x > 0.66) next();
    });

    // Keyboard: arrows + Home/End. Scoped to the reader group.
    on(els.bookEl, "keydown", (e) => {
      let handled = true;
      switch (e.key) {
        case "ArrowRight": case "PageDown": next(); break;
        case "ArrowLeft": case "PageUp": prev(); break;
        case "Home": go(0, -1); break;
        case "End": go(lastIndex(), 1); break;
        case "Enter": case " ": if (st.index === 0) next(); else handled = false; break;
        default: handled = false;
      }
      if (handled) e.preventDefault();
    });

    // Scrubber
    on(els.scrubber, "input", () => {
      const v = Number(els.scrubber.value) || 0;
      if (st.animating) { els.scrubber.value = String(st.index); return; }
      go(v, v >= st.index ? 1 : -1);
    });

    // ----- rendering -----
    function leafHtml(page, side) {
      if (!page) return `<div class="reader-leaf ${side} is-blank"><div class="leaf-face"></div></div>`;
      let inner;
      if (page.type === "cover") {
        let cover = "";
        try { cover = window.Covers && window.Covers.svg ? window.Covers.svg(meta) : ""; } catch { cover = ""; }
        inner = `<div class="leaf-cover">${cover || `<div class="cover-fallback">${esc(meta.title)}</div>`}</div>`;
      } else if (page.type === "colophon") {
        inner = `<div class="leaf-end"><p class="leaf-end-mark" aria-hidden="true">❧</p>` +
          `<p class="leaf-end-title">${esc(meta.title)}</p>` +
          (page.html ? `<p class="leaf-end-colophon">${page.html}</p>` : "") +
          `<p class="leaf-end-imprint">Bookwriter&#8202;Pro</p></div>`;
      } else {
        inner = `<div class="page-content">${page.html}</div>` +
          (page.label ? `<div class="page-folio" aria-hidden="true">${esc(page.label)}</div>` : "");
      }
      const coverCls = page.type === "cover" ? " is-cover" : "";
      return `<div class="reader-leaf ${side}${coverCls}"><div class="leaf-face">${inner}</div></div>`;
    }

    function spreadHtml(leftPage, rightPage, single) {
      if (single) return `<div class="reader-spread is-single">${leafHtml(rightPage, "right")}</div>`;
      return `<div class="reader-spread">${leafHtml(leftPage, "left")}${leafHtml(rightPage, "right")}</div>`;
    }

    // Determine which page(s) are visible for the current leaf index.
    function visiblePages() {
      if (st.index === 0) return { left: null, right: st.pages[0], single: true, cover: true };
      if (!st.twoUp) return { left: null, right: st.pages[st.index], single: true };
      // Two-up: spreads pair as [1,2], [3,4]… We snap index to the pair's left leaf.
      const left = pairLeft(st.index);
      return { left: st.pages[left], right: st.pages[left + 1], single: false };
    }

    function render(rebuild) {
      // Remove any probe leftovers.
      const v = visiblePages();
      els.bookEl.classList.toggle("is-cover", st.index === 0);
      els.bookEl.innerHTML = spreadHtml(v.left, v.right, v.single || v.cover);

      // Controls
      els.prevBtn.hidden = atStart();
      els.nextBtn.hidden = atEnd();
      els.tray.hidden = !st.isBook || st.pages.length <= 1;
      els.scrubber.max = String(lastIndex());
      els.scrubber.value = String(st.index);

      // Readout + polite announce
      const readout = readoutFor();
      els.readout.textContent = readout;
      srStatus(readout);
    }

    function readoutFor() {
      const total = lastIndex();
      if (st.index === 0) return "Cover";
      const cur = st.pages[st.index];
      if (cur && cur.type === "colophon") return "The end";
      // Content page number(s). Cover (0) and colophon (last) aren't "pages".
      const isContent = (i) => i >= 1 && i <= total - 1;
      if (st.twoUp) {
        const left = st.index % 2 === 1 ? st.index : st.index - 1;
        const right = left + 1;
        const shown = [left, right].filter(isContent);
        if (shown.length === 2) return `Pages ${shown[0]}–${shown[1]} of ${total - 1}`;
        if (shown.length === 1) return `Page ${shown[0]} of ${total - 1}`;
        return `Page ${st.index} of ${total}`;
      }
      return `Page ${st.index} of ${total - 1}`;
    }

    // GPU-friendly page-flip. Under reduced-motion we swap/cross-fade instantly.
    function animateTurn(from, to, dir) {
      if (reduce() || !window.matchMedia) {
        render();
        els.bookEl.classList.add("is-fade");
        requestAnimationFrame(() => els.bookEl.classList.remove("is-fade"));
        st.animating = false;
        focusAfterTurn();
        return;
      }
      st.animating = true;
      // Capture the leaf that will flip (the outgoing right leaf for forward, the
      // incoming for back). We render the target first, then overlay a flipping
      // leaf showing the old face so the turn reads as a real page lifting.
      const oldRight = els.bookEl.querySelector(".reader-leaf.right");
      const oldHtml = oldRight ? oldRight.outerHTML : "";
      render();
      const flip = document.createElement("div");
      flip.className = "reader-flip " + (dir >= 0 ? "fwd" : "back");
      flip.setAttribute("aria-hidden", "true");
      // Front shows the page we're leaving; it rotates to reveal the new spread.
      const targetRight = els.bookEl.querySelector(".reader-leaf.right");
      flip.innerHTML = `<div class="flip-front">${oldHtml}</div>`;
      els.bookEl.appendChild(flip);
      void flip.offsetWidth; // force reflow before adding the state class
      flip.classList.add("is-turning");
      const done = () => {
        flip.remove();
        st.animating = false;
        focusAfterTurn();
      };
      flip.addEventListener("transitionend", done, { once: true });
      // Safety net if transitionend doesn't fire.
      setTimeout(done, 720);
      void targetRight;
    }

    function focusAfterTurn() {
      // Keep keyboard focus on the book so subsequent arrows keep working.
      if (document.activeElement === els.scrubber) return;
      if (st.isBook) els.bookEl.focus({ preventScroll: true });
    }

    // ----- resize (debounced) re-paginate -----
    const onResize = () => {
      if (!st.isBook) return;
      clearTimeout(st.resizeT);
      st.resizeT = setTimeout(() => {
        // Preserve reading position by remembering the current content page,
        // then re-clamp after re-layout.
        paginate();
      }, 180);
    };
    on(window, "resize", onResize);

    // Initial mode.
    setMode(wantsBook);
    if (!wantsBook) { els.stage.hidden = true; els.tray.hidden = true; }
  },
};

// Parse manuscript markdown into a flat token stream the paginator consumes.
// Each token: {kind:"h1"|"h2"|"h3"|"hr"|"p", text, dropcap?:bool}
function parseManuscript(md) {
  const body = stripLeadingTitle(md || "");
  const lines = String(body).split(/\r?\n/);
  const tokens = [];
  let para = [];
  let firstAfterHeading = false;
  const flush = () => {
    if (para.length) {
      tokens.push({ kind: "p", text: para.join(" "), dropcap: firstAfterHeading });
      para = [];
      firstAfterHeading = false;
    }
  };
  for (const line of lines) {
    const t = line.trim();
    if (!t) { flush(); continue; }
    if (/^#\s+/.test(t)) { flush(); tokens.push({ kind: "h1", text: t.replace(/^#\s+/, "") }); continue; }
    if (/^##\s+/.test(t)) { flush(); tokens.push({ kind: "h2", text: t.replace(/^##\s+/, "") }); firstAfterHeading = true; continue; }
    if (/^#{3,}\s+/.test(t)) { flush(); tokens.push({ kind: "h3", text: t.replace(/^#{3,}\s+/, "") }); firstAfterHeading = true; continue; }
    const im = t.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (im) { flush(); tokens.push({ kind: "img", alt: im[1], url: im[2] }); firstAfterHeading = true; continue; }
    if (/^([-*_])\1{2,}$/.test(t) || t === "***" || t === "---") { flush(); tokens.push({ kind: "hr" }); continue; }
    para.push(t);
  }
  flush();
  return tokens;
}

// Render a single token to HTML (matches the plain-view markdown styling).
function tokenHtml(tok) {
  switch (tok.kind) {
    case "h1": return `<h1>${esc(tok.text)}</h1>`;
    case "h2": return `<h2>${esc(tok.text)}</h2><p class="chapter-ornament" aria-hidden="true">❧</p>`;
    case "h3": return `<h3>${esc(tok.text)}</h3>`;
    case "hr": return "<hr/>";
    case "img": return `<figure class="ms-figure"><img src="${esc(tok.url)}" alt="${esc(tok.alt || "Chapter illustration")}" loading="lazy" onerror="this.closest('figure').remove()"></figure>`;
    case "p": return `<p${tok.dropcap ? ' class="first-para"' : ""}>${esc(tok.text)}</p>`;
    default: return "";
  }
}

// Fill pages by measuring into `box`. We append a token, and if it overflows we
// pop it back and start a new page; an overflowing paragraph is split WORD BY
// WORD across the page break so nothing is ever clipped. Chapter "## " headings
// start a fresh page. Guarantees: every token's text lands on some page.
function layoutPages(tokens, box, meta) {
  const pages = [];
  const overflows = () => box.scrollHeight > box.clientHeight + 1;

  let pageNo = 0;            // content page number (folio)
  let started = false;       // has the current page received any content
  const startPage = () => { box.innerHTML = ""; started = false; };
  const commit = () => {
    pageNo += 1;
    pages.push({ type: "page", html: box.innerHTML, label: String(pageNo) });
  };

  startPage();

  const appendHtml = (html) => { box.insertAdjacentHTML("beforeend", html); };

  for (let i = 0; i < tokens.length; i++) {
    const tok = tokens[i];

    // Chapter heading begins a new page (unless the current page is empty).
    if (tok.kind === "h2" && started) { commit(); startPage(); }

    if (tok.kind === "p") {
      // Try the whole paragraph first.
      const before = box.innerHTML;
      appendHtml(tokenHtml(tok));
      if (!overflows()) { started = true; continue; }
      // Doesn't fit whole. If the page already has content, try it on a fresh
      // page; if it fits there, keep it whole.
      box.innerHTML = before;
      if (started) {
        commit(); startPage();
        appendHtml(tokenHtml(tok));
        if (!overflows()) { started = true; continue; }
        // Still overflows on an empty page -> word-wrap split below.
        box.innerHTML = "";
      }
      // Split this paragraph word-by-word across as many pages as needed.
      splitParagraph(tok, box, overflows, () => { commit(); startPage(); }, () => { started = true; });
      // splitParagraph leaves the trailing remainder on the current page.
      continue;
    }

    // Headings / hr: place, and if they overflow, push to a fresh page.
    const before = box.innerHTML;
    appendHtml(tokenHtml(tok));
    if (overflows() && started) {
      box.innerHTML = before;
      commit(); startPage();
      appendHtml(tokenHtml(tok));
    }
    started = true;
  }

  // Commit the final page if it holds anything.
  if (box.innerHTML.trim()) commit();
  if (!pages.length) pages.push({ type: "page", html: "", label: "1" });
  return pages;
}

// Split a paragraph word-by-word so a paragraph longer than a page never clips.
// Writes filled <p> chunks, committing a page each time the chunk overflows.
// Invariant: every word lands on exactly one page; the trailing remainder is
// left on the current (uncommitted) page for the caller to continue from.
function splitParagraph(tok, box, overflows, commitPage, markStarted) {
  const words = tok.text.split(/\s+/).filter(Boolean);
  let p = document.createElement("p");
  if (tok.dropcap) p.className = "first-para";
  box.appendChild(p);
  let line = [];                 // words committed to the current <p>
  for (let w = 0; w < words.length; w++) {
    line.push(words[w]);
    p.textContent = line.join(" ");
    if (overflows()) {
      if (line.length === 1) {
        // A single word taller than the empty page (pathological / very long
        // token). Keep it here — CSS overflow-wrap breaks it — and start the next
        // page fresh so we never loop forever.
        markStarted();
        commitPage();
        p = document.createElement("p");
        box.appendChild(p);
        line = [];
        continue;
      }
      // This word overflowed: drop it back, the page is full.
      line.pop();
      p.textContent = line.join(" ");
      markStarted();
      commitPage();
      // Fresh continuation paragraph on the next page (no drop-cap).
      p = document.createElement("p");
      box.appendChild(p);
      line = [];
      w--; // reprocess the word that didn't fit, now on the new page
    }
  }
  // Whatever remains stays on the current page for the caller to continue.
  if (!line.length) p.remove();
  markStarted();
}

// Drop a single leading top-level "# Title" heading (and the blank line after
// it) from assembled-manuscript markdown. The cover + colophon already carry the
// title, so this prevents the title appearing twice back-to-back. Subsequent
// "## Chapter" headings are preserved.
function stripLeadingTitle(md) {
  const lines = String(md).split(/\r?\n/);
  let i = 0;
  while (i < lines.length && !lines[i].trim()) i++; // skip leading blanks
  if (i < lines.length && /^#\s+/.test(lines[i].trim())) {
    lines.splice(0, i + 1);
    // also consume one immediately-following blank line for clean spacing
    if (lines.length && !lines[0].trim()) lines.shift();
    return lines.join("\n");
  }
  return md;
}

// Minimal, safe markdown -> HTML for the assembled manuscript.
function renderMarkdown(md) {
  const lines = String(md).split(/\r?\n/);
  const out = [];
  let para = [];
  // True right after a chapter heading: the next paragraph gets the drop-cap.
  let firstAfterHeading = false;
  const flush = () => {
    if (para.length) {
      const cls = firstAfterHeading ? ' class="first-para"' : "";
      out.push(`<p${cls}>${esc(para.join(" "))}</p>`);
      para = [];
      firstAfterHeading = false;
    }
  };
  for (const line of lines) {
    const t = line.trim();
    if (!t) { flush(); continue; }
    if (/^#\s+/.test(t)) { flush(); out.push(`<h1>${esc(t.replace(/^#\s+/, ""))}</h1>`); continue; }
    if (/^##\s+/.test(t)) { flush(); out.push(`<h2>${esc(t.replace(/^##\s+/, ""))}</h2>`); out.push('<p class="chapter-ornament" aria-hidden="true">❧</p>'); firstAfterHeading = true; continue; }
    if (/^#{3,}\s+/.test(t)) { flush(); out.push(`<h3>${esc(t.replace(/^#{3,}\s+/, ""))}</h3>`); firstAfterHeading = true; continue; }
    const im = t.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (im) { flush(); out.push(`<figure class="ms-figure"><img src="${esc(im[2])}" alt="${esc(im[1] || "Chapter illustration")}" loading="lazy" onerror="this.closest('figure').remove()"></figure>`); firstAfterHeading = true; continue; }
    if (/^([-*_])\1{2,}$/.test(t) || t === "***" || t === "---") { flush(); out.push("<hr/>"); continue; }
    para.push(t);
  }
  flush();
  return out.join("\n");
}

/* ============================== BOOT =================================== */
function boot() {
  // Expose a few helpers for palette.js (command palette / shortcuts), which is
  // loaded first and reaches for these lazily/defensively.
  window.Router = Router;
  window.toast = toast;
  window.srStatus = srStatus;
  initTheme();
  refreshHealth();
  // "New book" links open the Create-AI-Book modal in place rather than
  // navigating to a page — without changing the hash (deep-linking #/new still
  // works via the router branch above).
  document.addEventListener("click", (e) => {
    if (!e.target.closest) return;
    if (e.target.closest('[data-action="open-settings"]')) { e.preventDefault(); SettingsModal.open(); return; }
    if (e.target.closest('[data-action="import"]')) { e.preventDefault(); ImportModal.open(); return; }
    if (e.target.closest('a[href="#/new"], [data-action="new-book"]')) { e.preventDefault(); CreateModal.open(); }
  });
  Router.start();
}

// Boot only once the document is fully parsed AND all deferred scripts have run
// (e.g. kdp.js, which registers Views.publish). During deferred execution
// readyState is "interactive", so waiting for DOMContentLoaded guarantees every
// other defer script has executed before the first route resolves. Only boot
// synchronously if the page is already fully "complete" (e.g. injected late).
if (document.readyState === "complete") {
  boot();
} else {
  document.addEventListener("DOMContentLoaded", boot);
}
