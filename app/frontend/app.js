/* browser2zen frontend: vanilla JS state machine.
 *
 * All dynamic content is built with createElement / textContent. We never
 * assign user-derived strings (or strings that could embed user data) to
 * innerHTML; the only innerHTML use clears nodes via empty string.
 */

const $  = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const Bridge = () => (window.pywebview && window.pywebview.api) || null;
const sleep  = (ms) => new Promise((r) => setTimeout(r, ms));

function el(tag, props, children) {
  const e = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null) continue;
      if (k === "class") e.className = v;
      else if (k === "dataset") Object.assign(e.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "text") e.textContent = String(v);
      else if (k === "value") e.value = v;
      else if (k === "selected") e.selected = !!v;
      else e.setAttribute(k, String(v));
    }
  }
  for (const c of (children || [])) {
    if (c == null || c === false) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function replace(node, ...kids) { clear(node); for (const k of kids) if (k) node.appendChild(k); }

async function whenBridgeReady(timeoutMs = 5000) {
  const started = Date.now();
  while (!Bridge()) {
    if (Date.now() - started > timeoutMs) throw new Error("Bridge not ready");
    await sleep(50);
  }
  return Bridge();
}

function setScreen(name) {
  document.body.dataset.screen = name;
  for (const node of $$(".screen")) {
    node.classList.toggle("is-active", node.id === `screen-${name}`);
  }
}

// ---- state ----------------------------------------------------------------

const state = {
  env: null,
  selectedZenProfile: null,
  source: { name: "arc", displayName: "Arc" },  // current source-browser
  sources: [],                                   // catalogue from list_sources()
  preview: null,
  options: {
    foldersCollapsed: true,
    includeWorkspaces: true,
    includeBookmarks: true,
    includeFavicons: true,
    includePinnedTabs: true,
    includeOpenTabs: true,
    includeHistory: false,
    includeCookies: false,
    excludedSpaces: [],
  },
  steps: [],
  stepLabels: {},
  stepStates: {},
  stepSummaries: {},
  logLines: [],
  pollHandle: null,
  startedAt: 0,
  elapsedHandle: null,
  activeStep: null,
  activeDetail: "",
  activeProgress: null,  // 0..1 or null
};

// ---- button loading state helper ----------------------------------------

function setLoading(btn, on, label = null) {
  if (on) {
    if (label && !btn.dataset.savedLabel) {
      btn.dataset.savedLabel = btn.textContent;
      btn.textContent = label;
    }
    if (!btn.querySelector(".spinner")) {
      btn.insertBefore(el("span", {class: "spinner"}), btn.firstChild);
    }
    btn.classList.add("is-loading");
  } else {
    btn.classList.remove("is-loading");
    const sp = btn.querySelector(".spinner"); if (sp) sp.remove();
    if (btn.dataset.savedLabel) {
      btn.textContent = btn.dataset.savedLabel;
      delete btn.dataset.savedLabel;
    }
  }
}

// ---- brand marks --------------------------------------------------------
//
// Brand glyphs are real official logos served from
// ``app/frontend/assets/sources/<name>.svg`` (Wikimedia Commons + simpleicons,
// see assets/sources/README.md for attributions). The wrapper span carries
// the brand-tinted background (CSS); the <img> sits centred on top.
//
// Sizes:
//   default — 56 px, used in the welcome hero strip
//   small   — 32 px, used in the source picker + detect cards
//   tiny    — 22 px, used inline (e.g. progress meta)

const SOURCE_NAMES = ["arc", "chrome", "edge", "brave", "firefox", "safari"];

function makeBrowserMark(name, size = "default") {
  const wrap = document.createElement("span");
  const sizeClass = size === "small" ? " small" : (size === "tiny" ? " tiny" : "");
  wrap.className = `brand-mark ${name}${sizeClass}`;
  const img = document.createElement("img");
  img.src = `assets/sources/${name}.svg`;
  img.alt = "";        // decorative; the surrounding label carries the name
  img.setAttribute("aria-hidden", "true");
  img.draggable = false;
  wrap.appendChild(img);
  return wrap;
}

function makeArrowGlyph() {
  const span = document.createElement("span");
  span.className = "arrow";
  span.textContent = "→";
  return span;
}

// Decorate static placeholders on welcome + detect.
function decorateBranding() {
  // Welcome hero: every supported source on the left, arrow, Zen on the
  // right. Conveys multi-source support at a glance.
  const pair = $("brand-pair");
  if (pair && !pair.firstChild) {
    const sources = document.createElement("span");
    sources.className = "brand-source-row";
    for (const name of SOURCE_NAMES) {
      sources.appendChild(makeBrowserMark(name, "small"));
    }
    const zenHero = makeBrowserMark("zen");
    zenHero.id = "welcome-zen-mark";
    pair.appendChild(sources);
    pair.appendChild(makeArrowGlyph());
    pair.appendChild(zenHero);
    wireMergeEasterEgg(zenHero, sources);
  }
  // Source-card on the detect screen: the chosen source's mark.
  const srcMark = $("source-mark");
  if (srcMark) {
    const srcName = (state.source && state.source.name) || "arc";
    const fresh = makeBrowserMark(srcName, "small");
    fresh.id = "source-mark";
    srcMark.replaceWith(fresh);
  }
  // Zen card on the detect screen.
  const zenMark = $("zen-mark");
  if (zenMark) {
    const fresh = makeBrowserMark("zen", "small");
    fresh.id = "zen-mark";
    zenMark.replaceWith(fresh);
  }
}

// Easter egg: clicking the welcome Zen logo plays a quick merge
// animation — each source badge slides into Zen with a 60 ms stagger,
// Zen gives one warm pulse, then everything snaps back. Visualises
// what the app actually does. Cooldown blocks re-trigger until the
// previous run finishes.
function wireMergeEasterEgg(zenMark, sourceRow) {
  let cooldown = false;
  zenMark.style.cursor = "pointer";
  zenMark.addEventListener("click", () => {
    if (cooldown) return;
    cooldown = true;
    sourceRow.classList.add("is-merging");
    zenMark.classList.add("is-merging");
    setTimeout(() => {
      sourceRow.classList.remove("is-merging");
      zenMark.classList.remove("is-merging");
      cooldown = false;
    }, 1800);
  });
}

// ---- titlebar -------------------------------------------------------------

$("tl-close").addEventListener("click", async () => {
  const api = Bridge(); if (api) api.quit_app();
});

// ---- welcome --------------------------------------------------------------

$("welcome-go").addEventListener("click", () => goToSourcePicker());
$("welcome-backup").addEventListener("click", () => setScreen("backup-mode"));

async function goToSourcePicker() {
  setScreen("source");
  await runSourcePicker();
}

async function goToDetect() {
  setScreen("detect");
  await runDetect();
}

// ---- source picker --------------------------------------------------------

async function runSourcePicker() {
  const api = await whenBridgeReady();
  let sources;
  try {
    sources = await api.list_sources();
  } catch (e) {
    sources = [{ name: "arc", displayName: "Arc", installed: true, running: false }];
  }
  state.sources = Array.isArray(sources) ? sources : [];
  renderSourcePicker(state.sources);
}

function renderSourcePicker(sources) {
  const grid = $("source-grid");
  clear(grid);

  // Pre-select the current source if it's in the list and installed,
  // otherwise pick the first installed one.
  const installedNames = sources.filter(s => s.installed).map(s => s.name);
  let pick = state.source && installedNames.includes(state.source.name)
    ? state.source.name
    : (installedNames[0] || null);
  state.source = sources.find(s => s.name === pick) || state.source;

  for (const s of sources) {
    const card = el("button", {
      class: "source-card",
      type: "button",
      dataset: { name: s.name, installed: String(!!s.installed), selected: String(s.name === pick) },
    }, [
      makeBrowserMark(s.name, "small"),
      el("div", { class: "source-card-text" }, [
        el("div", { class: "source-card-name" }, [s.displayName || s.name]),
        el("div", {
          class: "source-card-status " + (s.installed ? (s.running ? "is-warn" : "is-ok") : "is-dim"),
        }, [s.installed ? (s.running ? "Running" : "Installed") : "Not found"]),
      ]),
    ]);
    if (s.installed) {
      card.addEventListener("click", () => selectSource(s.name));
    } else {
      card.disabled = true;
    }
    grid.appendChild(card);
  }
  $("source-next").disabled = !pick;
}

function selectSource(name) {
  const picked = state.sources.find(s => s.name === name);
  if (!picked || !picked.installed) return;
  state.source = picked;
  for (const card of $$("#source-grid .source-card")) {
    card.dataset.selected = String(card.dataset.name === name);
  }
  $("source-next").disabled = false;
}

$("source-back").addEventListener("click", () => setScreen("welcome"));
$("source-next").addEventListener("click", async () => {
  const api = await whenBridgeReady();
  if (!state.source) return;
  const result = await api.set_source(state.source.name);
  if (!result || result.ok === false) {
    // Best-effort fallback: still navigate; the Detect screen will
    // surface the error.
  } else {
    state.source = {
      name: result.name, displayName: result.displayName,
      installed: !!result.installed, running: !!result.running,
    };
  }
  await goToDetect();
});

// ---- detect ---------------------------------------------------------------

async function runDetect() {
  const api = await whenBridgeReady();
  $("source-pill").textContent = "Detecting…";
  $("zen-pill").textContent = "Detecting…";
  state.env = await api.check_env();
  renderDetect(state.env);
}

function renderDetect(env) {
  // Source-browser card (Arc / Chrome / etc.). The DOM ids start with
  // "arc-" for legacy reasons; the field names on env are also "arc*"
  // but they describe whichever source is currently selected.
  const srcName = (state.source && state.source.displayName) || "Arc";
  const srcKey = (state.source && state.source.name) || "arc";
  const sourceCardName = $("source-card-name");
  if (sourceCardName) sourceCardName.textContent = srcName;
  // Refresh the brand mark to match the chosen source.
  const srcMark = $("source-mark");
  if (srcMark) {
    const fresh = makeBrowserMark(srcKey, "small");
    fresh.id = "arc-mark";
    srcMark.replaceWith(fresh);
  }
  const srcOk = env.sourceInstalled && !env.sourceRunning && env.sourceProfiles.length > 0;
  const srcPill = $("source-pill");
  const srcDetail = $("source-detail");
  const srcCard = $("card-source");
  $("source-running-row").style.display = env.sourceRunning ? "" : "none";
  const quitBtn = $("source-quit-btn");
  if (quitBtn) quitBtn.textContent = `Quit ${srcName}`;

  srcCard.dataset.ok = srcOk ? "true" : "false";
  if (!env.sourceInstalled) {
    srcPill.className = "pill pill-err"; srcPill.textContent = "Not found";
    srcDetail.textContent = `We couldn't find ${srcName} data on this machine.`;
  } else if (env.sourceRunning) {
    srcPill.className = "pill pill-warn"; srcPill.textContent = "Running";
    srcDetail.textContent = `${srcName} is open. Quit it before we read its data.`;
  } else if (env.sourceProfiles.length === 0) {
    srcPill.className = "pill pill-warn"; srcPill.textContent = "Empty";
    srcDetail.textContent = `${srcName} is installed but no profiles were found.`;
  } else {
    srcPill.className = "pill pill-ok"; srcPill.textContent = "Ready";
    const n = env.sourceProfiles.length;
    srcDetail.textContent = `${n} profile${n === 1 ? "" : "s"}: ${env.sourceProfiles.join(", ")}.`;
  }

  // Zen card
  const zenCard = $("card-zen");
  const zenPill = $("zen-pill");
  const zenDetail = $("zen-detail");
  const zenSelect = $("zen-profile-select");
  $("zen-running-row").style.display = env.zenRunning ? "" : "none";
  $("zen-install-row").style.display = env.zenInstalled ? "none" : "";

  if (!env.zenInstalled) {
    zenCard.dataset.ok = "false";
    zenPill.className = "pill pill-err"; zenPill.textContent = "Not installed";
    zenDetail.textContent = "Zen Browser doesn't appear to be installed yet.";
    zenSelect.style.display = "none";
  } else if (env.zenRunning) {
    zenCard.dataset.ok = "false";
    zenPill.className = "pill pill-warn"; zenPill.textContent = "Running";
    zenDetail.textContent = "Zen is open. Quit it before we write to its profile.";
    zenSelect.style.display = "none";
  } else {
    zenCard.dataset.ok = "true";
    zenPill.className = "pill pill-ok"; zenPill.textContent = "Ready";
    if (env.zenProfiles.length > 1) {
      zenDetail.textContent = `${env.zenProfiles.length} profiles found. Pick one:`;
      zenSelect.style.display = "";
      clear(zenSelect);
      env.zenProfiles.forEach((p, i) => {
        // `install` ("XDG" / "Flatpak") tells two same-named profiles
        // apart when a native and a sandboxed Zen sit side by side.
        // The dir name usually already carries "(release)", so only add
        // that tag when it doesn't.
        const tags = [];
        if (p.isRelease && !/release/i.test(p.name)) tags.push("release");
        if (p.install) tags.push(p.install);
        zenSelect.appendChild(el("option", {value: p.path, selected: i === 0,
          text: p.name + (tags.length ? ` (${tags.join(", ")})` : "")}));
      });
      state.selectedZenProfile = env.zenProfiles[0].path;
      zenSelect.onchange = () => { state.selectedZenProfile = zenSelect.value; updateGate(state.env); };
    } else {
      const p = env.zenProfiles[0];
      zenDetail.textContent = `Profile: ${p.name}.`;
      zenSelect.style.display = "none";
      state.selectedZenProfile = p.path;
    }
  }

  updateGate(env);
}

function updateGate(env) {
  const gate = $("detect-gate");
  const text = $("detect-gate-text");
  const next = $("detect-next");
  const srcName = (state.source && state.source.displayName) || "Arc";

  const issues = [];
  if (!env.sourceInstalled) issues.push(`${srcName} isn't installed.`);
  if (env.sourceRunning) issues.push(`${srcName} is still running.`);
  if (env.sourceInstalled && env.sourceProfiles.length === 0) issues.push(`${srcName} has no profiles.`);
  if (!env.zenInstalled) issues.push("Zen isn't installed (or never launched).");
  if (env.zenRunning) issues.push("Zen is still running.");
  if (!env.hasLz4) issues.push("Python lz4 module missing (build issue).");

  if (issues.length === 0) {
    gate.style.display = "none";
    next.disabled = false;
  } else {
    gate.style.display = "";
    gate.classList.toggle("is-error", issues.some(i => i.includes("isn't installed")));
    text.textContent = issues.join(" ");
    next.disabled = true;
  }
}

$("source-quit-btn").addEventListener("click", async () => {
  const btn = $("source-quit-btn");
  const srcName = (state.source && state.source.displayName) || "Arc";
  setLoading(btn, true, `Quitting ${srcName}`);
  await Bridge().quit_source(); await sleep(400);
  await runDetect();
  setLoading(btn, false);
});
$("zen-quit-btn").addEventListener("click", async () => {
  const btn = $("zen-quit-btn");
  setLoading(btn, true, "Quitting Zen");
  await Bridge().quit_browser("zen"); await sleep(400);
  await runDetect();
  setLoading(btn, false);
});
$("detect-recheck").addEventListener("click", () => runDetect());
$("detect-back").addEventListener("click", () => goToSourcePicker());
$("detect-next").addEventListener("click", () => goToPreview());
$("zen-install-btn").addEventListener("click", () => {
  const api = Bridge();
  if (api) api.open_url("https://zen-browser.app/");
});

// ---- preview --------------------------------------------------------------

async function goToPreview() {
  setScreen("preview");
  replace($("stat-strip"), el("span", {
    class: "muted",
    text: `Reading ${(state.source && state.source.displayName) || "Arc"} data…`,
  }));
  clear($("spaces-list"));
  clear($("toggles"));

  const api = Bridge();
  const opts = currentOptionsJson();
  const preview = await api.preview(opts);
  if (preview && preview.error) {
    replace($("stat-strip"), el("span", {class: "muted", text: preview.error}));
    return;
  }
  state.preview = preview;
  renderPreview(preview);
}

function renderPreview(p) {
  const strip = $("stat-strip");
  clear(strip);
  for (const [n, lbl] of [
    [p.spaces.length, "Spaces"],
    [p.pinnedTotal, "Pinned tabs"],
    [p.openTotal, "Open tabs"],
    [p.folderTotal, "Folders"],
    [p.bookmarkTotal, "Bookmarks"],
    [p.faviconMatchEstimate, "Favicons"],
  ]) {
    strip.appendChild(makeStat(n, lbl));
  }

  const list = $("spaces-list"); clear(list);
  for (const s of p.spaces) list.appendChild(makeSpaceRow(s));

  const toggles = $("toggles"); clear(toggles);
  toggles.appendChild(makeToggle("includeOpenTabs", "Open tabs",
                                 `Migrate ${p.openTotal} open tabs`));
  toggles.appendChild(makeToggle("includeHistory", "Browsing history",
                                 `Copy ~${formatRows(p.historyRowsEstimate)} history rows`));
  toggles.appendChild(makeToggle("includeCookies", "Cookies & login state",
                                 `Copy ~${formatRows(p.cookiesEstimate)} cookies (Keychain prompt)`));
  toggles.appendChild(makeToggle("foldersCollapsed", "Collapse folders",
                                 "Imported folders start collapsed"));
}

function makeStat(n, lbl) {
  return el("div", {class: "stat"}, [
    el("span", {class: "num", text: formatRows(n)}),
    el("span", {class: "lbl", text: lbl}),
  ]);
}

function makeSpaceRow(s) {
  const hasColor = Array.isArray(s.color) && s.color.length === 3;
  const excluded = (state.options.excludedSpaces || []).includes(s.name);

  const checkbox = el("input", {
    type: "checkbox",
    class: "space-check",
    checked: excluded ? null : "checked",
  });
  checkbox.checked = !excluded;
  checkbox.addEventListener("click", (ev) => ev.stopPropagation());
  checkbox.addEventListener("change", () => toggleSpaceExcluded(s.name, !checkbox.checked, row));

  const row = el("div", {
    class: hasColor ? "space-row" : "space-row no-color",
    role: "button",
    tabindex: "0",
    "data-included": excluded ? "false" : "true",
  }, [
    checkbox,
    el("div", {class: "icon", text: s.icon || "·"}),
    el("div", {class: "text"}, [
      el("div", {class: "name", text: s.name}),
      el("div", {class: "meta", text:
        `${s.folderCount} folder${s.folderCount === 1 ? "" : "s"}` +
        (s.essentialCount ? ` · ${s.essentialCount} essential` : "")}),
    ]),
    el("div", {class: "count", text: `${s.pinnedCount} tabs`}),
  ]);

  // Whole-card click toggles the checkbox so the entire row is the target.
  row.addEventListener("click", () => {
    checkbox.checked = !checkbox.checked;
    toggleSpaceExcluded(s.name, !checkbox.checked, row);
  });
  row.addEventListener("keydown", (ev) => {
    if (ev.key === " " || ev.key === "Enter") {
      ev.preventDefault();
      checkbox.checked = !checkbox.checked;
      toggleSpaceExcluded(s.name, !checkbox.checked, row);
    }
  });

  if (hasColor) {
    const [r, g, b] = s.color;
    row.style.setProperty("--space-tint-r", r);
    row.style.setProperty("--space-tint-g", g);
    row.style.setProperty("--space-tint-b", b);
  }
  return row;
}

function toggleSpaceExcluded(name, isExcluded, row) {
  if (!Array.isArray(state.options.excludedSpaces)) state.options.excludedSpaces = [];
  const list = state.options.excludedSpaces;
  const idx = list.indexOf(name);
  if (isExcluded && idx === -1) list.push(name);
  if (!isExcluded && idx !== -1) list.splice(idx, 1);
  if (row) row.dataset.included = isExcluded ? "false" : "true";
}

function makeToggle(key, label, desc) {
  const node = el("div", {
    class: "toggle",
    dataset: {key, on: state.options[key] ? "true" : "false"},
    onclick: () => {
      state.options[key] = !state.options[key];
      node.dataset.on = state.options[key] ? "true" : "false";
    },
  }, [
    el("div", {class: "label-stack"}, [
      el("span", {class: "lbl", text: label}),
      el("span", {class: "desc", text: desc}),
    ]),
    el("div", {class: "switch"}),
  ]);
  return node;
}

$("preview-back").addEventListener("click", () => goToDetect());
$("preview-go").addEventListener("click", () => goToProgress());

// ---- progress -------------------------------------------------------------

// ---- step icons (inline SVG built via DOM, no string parsing) ----------

const SVG_NS = "http://www.w3.org/2000/svg";

function svgChild(parent, tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
  parent.appendChild(node);
  return node;
}

const STEP_ICON_BUILDERS = {
  extract: (s) => {
    svgChild(s, "path", {d: "M5 2 L11 2 L14 5 L14 16 L4 16 L4 2 Z"});
    svgChild(s, "path", {d: "M11 2 L11 5 L14 5"});
    svgChild(s, "path", {d: "M6 9 L12 9"});
    svgChild(s, "path", {d: "M6 12 L12 12"});
  },
  containers: (s) => {
    svgChild(s, "path", {d: "M9 2 L15 5 L9 8 L3 5 Z"});
    svgChild(s, "path", {d: "M3 9 L9 12 L15 9"});
    svgChild(s, "path", {d: "M3 12 L9 15 L15 12"});
  },
  sessions: (s) => {
    svgChild(s, "rect", {x: 2.5, y: 3, width: 13, height: 12, rx: 1.5});
    svgChild(s, "line", {x1: 2.5, y1: 6.5, x2: 15.5, y2: 6.5});
  },
  bookmarks: (s) => svgChild(s, "path", {d: "M5 2 L13 2 L13 16 L9 13 L5 16 Z"}),
  favicons: (s) => {
    svgChild(s, "rect", {x: 2.5, y: 3, width: 13, height: 12, rx: 1.5});
    svgChild(s, "circle", {cx: 6.5, cy: 7, r: 1.3, fill: "currentColor", stroke: "none"});
    svgChild(s, "path", {d: "M2.5 13 L7 9 L11 12 L15.5 8"});
  },
  open_tabs: (s) => {
    svgChild(s, "rect", {x: 3, y: 4, width: 11, height: 11, rx: 1.5});
    svgChild(s, "rect", {x: 6, y: 2, width: 9, height: 3, rx: 1});
  },
  history: (s) => {
    svgChild(s, "circle", {cx: 9, cy: 9, r: 6.5});
    svgChild(s, "path", {d: "M9 5 L9 9 L12 11", "stroke-linecap": "round"});
  },
  cookies: (s) => {
    svgChild(s, "circle", {cx: 6, cy: 9, r: 3.5});
    svgChild(s, "path", {d: "M9.5 9 L16 9", "stroke-linecap": "round"});
    svgChild(s, "path", {d: "M14 9 L14 12", "stroke-linecap": "round"});
    svgChild(s, "path", {d: "M16 9 L16 11.5", "stroke-linecap": "round"});
  },
  finalize: (s) => svgChild(s, "path", {d: "M3 9 L7.5 13 L15 5",
                                         "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round"}),
};

function makeStepIcon(step) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 18 18");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linejoin", "round");
  const builder = STEP_ICON_BUILDERS[step] || STEP_ICON_BUILDERS.sessions;
  builder(svg);
  return svg;
}

// ---- step descriptions + streaming patterns -----------------------------

// Step subtitles are slightly source-aware: extract/favicons/cookies
// reference the source by name. Everything else describes the Zen-side
// write path, which is identical regardless of source.
function stepSubtitle(step) {
  const src = (state.source && state.source.displayName) || "Arc";
  const isArc = state.source && state.source.name === "arc";
  switch (step) {
    case "extract":
      return isArc
        ? "Reading Arc's StorableSidebar.json"
        : `Reading ${src} bookmarks and profile data`;
    case "containers":
      return "Setting up cookie-isolated containers per space";
    case "sessions":
      return "Writing spaces, pinned tabs and folders to zen-sessions.jsonlz4";
    case "bookmarks":
      return "Mirroring pinned tabs as Firefox bookmarks";
    case "favicons":
      return `Decoding ${src}'s icon cache and inlining into Zen tabs`;
    case "open_tabs":
      return "Creating Zen sessionstore entries";
    case "history":
      return "Copying browsing history into places.sqlite";
    case "cookies":
      return `Decrypting ${src} cookies and writing to cookies.sqlite`;
    case "finalize":
      return "Marking migration complete";
    default:
      return "";
  }
}
const STEP_SUBTITLES = new Proxy({}, { get: (_, step) => stepSubtitle(step) });

// Each pattern returns {detail, percent?} when matched against a log line.
// We use String.prototype.match (no regex.exec) so it composes cleanly.
const STREAM_PATTERNS = {
  extract: [
    [/Found (\d+) spaces with pinned tabs/, m => ({detail: `Found ${m[1]} spaces`})],
    [/✅ ([^:]+): (\d+) pinned tabs/,        m => ({detail: `Read ${m[1].trim()}: ${m[2]} pinned tabs`})],
  ],
  containers: [
    [/Creating containers? for (\d+)/, m => ({detail: `Creating ${m[1]} containers`})],
    [/Reusing existing container/,     () => ({detail: "Reusing existing container"})],
  ],
  sessions: [
    [/Importing pinned tabs? \(workspace ([^)]+)\)/, m => ({detail: `Workspace: ${m[1]}`})],
    [/Imported (\d+) pinned tabs/,                   m => ({detail: `Imported ${m[1]} pinned tabs`})],
    [/Backed up zen-sessions/,                       () => ({detail: "Backed up zen-sessions.jsonlz4"})],
  ],
  bookmarks: [
    [/Creating bookmark folder/, () => ({detail: "Creating bookmark folders"})],
    [/Imported: (\d+) bookmarks/, m => ({detail: `Imported ${m[1]} bookmarks`})],
  ],
  favicons: [
    [/Reading Arc favicons from (\S+)/,     m => ({detail: `Reading ${m[1]} profile`})],
    [/Matched favicons for (\d+) of (\d+)/, m => ({detail: `Matched ${m[1]} of ${m[2]}`, percent: +m[1] / +m[2]})],
    [/Backed up favicons/,                  () => ({detail: "Backed up favicons.sqlite"})],
    [/Imported (\d+) favicons/,             m => ({detail: `Imported ${m[1]} favicons`})],
    [/Injected favicons into (\d+) tabs/,   m => ({detail: `Inlined favicons into ${m[1]} tabs`})],
  ],
  history: [
    [/Reading Arc history from (\S+)/,                                    m => ({detail: `Reading ${m[1]} profile`})],
    [/Aggregated (\d+) URLs from Arc history \((\d+) visits\)/,           m => ({detail: `Aggregated ${formatRows(+m[1])} URLs / ${formatRows(+m[2])} visits`})],
    [/Backed up places\.sqlite/,                                          () => ({detail: "Backed up places.sqlite"})],
    [/History: \+(\d+) new places, ~(\d+) merged, \+(\d+) visits/,        m => ({detail: `+${formatRows(+m[1])} places, +${formatRows(+m[3])} visits`})],
  ],
  cookies: [
    [/Reading Arc cookies from (\S+)/,                            m => ({detail: `Reading ${m[1]} profile`})],
    [/Decrypted (\d+) encrypted cookie/,                          m => ({detail: `Decrypted ${formatRows(+m[1])} cookies`})],
    [/Decoded (\d+) of (\d+) cookies/,                            m => ({detail: `Decoded ${m[1]} of ${m[2]}`, percent: +m[1] / +m[2]})],
    [/Backed up cookies/,                                         () => ({detail: "Backed up cookies.sqlite"})],
    [/Cookies: imported (\d+), merged (\d+) across (\d+)/,        m => ({detail: `${formatRows(+m[1])} imported across ${m[3]} contexts`})],
  ],
};

function applyStreamingMatch(step, message) {
  const patterns = STREAM_PATTERNS[step]; if (!patterns) return null;
  for (const [re, fn] of patterns) {
    const m = String(message).match(re);
    if (m) return fn(m);
  }
  return null;
}

// ---- progress flow -------------------------------------------------------

async function goToProgress() {
  setScreen("progress");

  const api = Bridge();
  const meta = await api.get_step_metadata();
  state.steps = meta.steps;
  state.stepLabels = meta.labels;
  state.stepStates = Object.fromEntries(state.steps.map(s => [s, "pending"]));
  state.stepSummaries = {};
  state.logLines = [];
  state.activeStep = null;
  state.activeDetail = "";
  state.activeProgress = null;
  state.startedAt = Date.now();

  if (state.elapsedHandle) clearInterval(state.elapsedHandle);
  state.elapsedHandle = setInterval(updateElapsed, 1000);
  updateElapsed();

  renderProgress();

  await api.start_migration(currentOptionsJson());
  state.pollHandle = setInterval(pollProgress, 120);
}

function updateElapsed() {
  const sec = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
  const mm = Math.floor(sec / 60);
  const ss = String(sec % 60).padStart(2, "0");
  $("elapsed").textContent = `${mm}:${ss}`;
}

async function pollProgress() {
  const api = Bridge(); if (!api) return;
  const data = await api.drain_progress();
  for (const ev of (data.events || [])) handleEvent(ev);
  if (data.state && data.state.status === "done") {
    clearInterval(state.pollHandle); state.pollHandle = null;
    if (state.elapsedHandle) { clearInterval(state.elapsedHandle); state.elapsedHandle = null; }
    finishOk(data.state);
  } else if (data.state && data.state.status === "error") {
    clearInterval(state.pollHandle); state.pollHandle = null;
    if (state.elapsedHandle) { clearInterval(state.elapsedHandle); state.elapsedHandle = null; }
    finishError(data.state);
  }
}

function handleEvent(ev) {
  if (ev.kind === "step_start") {
    state.stepStates[ev.step] = "active";
    state.activeStep = ev.step;
    state.activeDetail = "";
    state.activeProgress = null;
  } else if (ev.kind === "step_done") {
    state.stepStates[ev.step] = "done";
    if (ev.summary) state.stepSummaries[ev.step] = ev.summary;
    if (state.activeStep === ev.step) {
      state.activeStep = null;
      state.activeDetail = "";
      state.activeProgress = null;
    }
  } else if (ev.kind === "step_error") {
    state.stepStates[ev.step] = "error";
  } else {
    state.logLines.push({kind: ev.kind, message: ev.message, step: ev.step});
    if (state.logLines.length > 500) state.logLines.shift();
    if (state.activeStep && (ev.step === state.activeStep || !ev.step)) {
      const match = applyStreamingMatch(state.activeStep, ev.message);
      if (match) {
        if (match.detail !== undefined) state.activeDetail = match.detail;
        if (match.percent !== undefined) state.activeProgress = match.percent;
      }
    }
  }
  renderProgress();
  renderLog();
}

function visibleSteps() {
  return state.steps.filter(s => {
    if (s === "history")   return state.options.includeHistory;
    if (s === "cookies")   return state.options.includeCookies;
    if (s === "containers" || s === "sessions") return state.options.includeWorkspaces || state.options.includePinnedTabs;
    if (s === "bookmarks") return state.options.includeBookmarks;
    if (s === "favicons")  return state.options.includeFavicons;
    return true;
  });
}

function renderProgress() {
  const steps = visibleSteps();

  // top meter
  const meter = $("meter"); clear(meter);
  for (const s of steps) {
    meter.appendChild(el("div", {class: "meter-segment",
                                  dataset: {state: state.stepStates[s] || "pending", step: s}}));
  }

  const activeIdx = steps.findIndex(s => state.stepStates[s] === "active");
  const total = steps.length;
  const meta = $("progress-meta");
  if (activeIdx >= 0)                                          meta.textContent = `Step ${activeIdx + 1} of ${total}`;
  else if (steps.every(s => state.stepStates[s] === "done"))   meta.textContent = `${total} of ${total} done`;
  else                                                         meta.textContent = "Working";

  // unified timeline
  const tl = $("timeline"); clear(tl);
  for (const s of steps) {
    const st = state.stepStates[s] || "pending";
    if (st === "active") {
      tl.appendChild(makeActiveRow(s));
    } else {
      tl.appendChild(makeRow(s, st));
    }
  }
}

function makeRow(step, st) {
  const summary = (st === "done") ? stepSummaryText(step, state.stepSummaries[step]) : "";
  return el("li", {class: "tl-step", dataset: {step, state: st}}, [
    el("div", {class: "ico"}, [makeStepIcon(step)]),
    el("span", {class: "lbl", text: state.stepLabels[step] || step}),
    el("span", {class: "summary", text: summary}),
  ]);
}

function makeActiveRow(step) {
  const live = el("div", {class: "live"});
  live.appendChild(el("div", {class: "detail", text: state.activeDetail || ""}));
  if (state.activeProgress != null) {
    const fill = el("span");
    fill.style.width = `${Math.round(state.activeProgress * 100)}%`;
    live.appendChild(el("div", {class: "bar"}, [fill]));
  }
  return el("li", {class: "tl-step", dataset: {step, state: "active"}}, [
    el("div", {class: "ico"}, [makeStepIcon(step)]),
    el("span", {class: "lbl", text: state.stepLabels[step] || step}),
    el("span", {class: "subtitle", text: STEP_SUBTITLES[step] || ""}),
    live,
  ]);
}

function stepSummaryText(step, s) {
  if (!s) return "";
  if (step === "extract")    return `${s.spaces ?? "?"} spaces · ${s.pinned ?? "?"} tabs`;
  if (step === "containers") return `${s.created_or_reused ?? "?"} containers`;
  if (step === "favicons") {
    const db = s.db || {}; const sess = s.session || {};
    return `${formatRows(db.imported ?? 0)} icons · ${formatRows(sess.updated ?? 0)} tabs inlined`;
  }
  if (step === "history") return `${formatRows(s.places_added ?? 0)} places · ${formatRows(s.visits_added ?? 0)} visits`;
  if (step === "cookies") return `${formatRows(s.imported ?? 0)} imported · ${formatRows(s.merged ?? 0)} merged`;
  if (step === "sessions") {
    if (s.pinned == null && s.open == null) return s.ok ? "ok" : "";
    const parts = [];
    if (s.pinned) parts.push(`${formatRows(s.pinned)} pinned`);
    if (s.open)   parts.push(`${formatRows(s.open)} open`);
    return parts.join(" · ") || (s.ok ? "ok" : "");
  }
  if (step === "bookmarks") return s.ok ? "ok" : "";
  return "";
}

function renderLog() {
  const wrap = $("log"); clear(wrap);
  const recent = state.logLines.slice(-200);
  for (const l of recent) {
    const cls = l.kind === "warn" ? "warn" : (l.kind === "step_error" ? "err" : "");
    wrap.appendChild(el("div", {class: cls, text: l.message}));
  }
  wrap.scrollTop = wrap.scrollHeight;
}

// ---- done -----------------------------------------------------------------

async function finishOk(finalState) {
  setScreen("done");
  const api = Bridge();
  const kind = finalState && finalState.kind;
  const isBackup = kind === "export" || kind === "restore";

  const headline = $("done-headline");
  const summary = $("done-summary");
  const launch = $("done-launch");
  const backupsSection = $("done-backups-section");

  if (kind === "export") {
    headline.textContent = "Backup saved.";
    const path = finalState.archivePath || backupState.exportOutputPath;
    summary.textContent = path ? shortenPath(path) : "Your Zen profile is bundled into a portable file.";
    if (path) {
      summary.style.cursor = "pointer";
      summary.title = "Reveal in Finder";
      summary.onclick = () => api && api.open_path_in_finder(path);
    }
  } else if (kind === "restore") {
    headline.textContent = "Restore complete.";
    const n = finalState.restoredCount || 0;
    summary.textContent = n
      ? `${n} file${n === 1 ? "" : "s"} written into your Zen profile.`
      : "Your Zen profile has been updated.";
    summary.style.cursor = "";
    summary.title = "";
    summary.onclick = null;
  } else {
    headline.textContent = "All set.";
    const ext = state.stepSummaries.extract || {};
    summary.textContent = ext.pinned
      ? `${ext.pinned} pinned tabs across ${ext.spaces} spaces. Backups saved next to your Zen profile.`
      : `Backups saved next to your Zen profile.`;
    summary.style.cursor = "";
    summary.title = "";
    summary.onclick = null;
  }

  launch.style.display = isBackup ? "none" : "";
  backupsSection.style.display = isBackup ? "none" : "";
  $("done-back").style.display = isBackup ? "" : "none";

  if (!isBackup) {
    const list = $("backups-list"); clear(list);
    const backups = (finalState.backups || []).slice(-12).reverse();
    for (const path of backups) {
      list.appendChild(el("li", {
        dataset: {path},
        text: shortenPath(path),
        onclick: () => api.open_path_in_finder(path),
      }));
    }
  }
}

$("done-launch").addEventListener("click", async () => {
  const api = Bridge(); if (!api) return;
  // Don't await quit_app — calling window.destroy() while a JS-Python promise
  // is still pending leaves WKWebView waiting for a reply that never comes,
  // which looks like the window "hanging on loading".
  try { await api.launch_zen(); } catch (e) { /* best-effort */ }
  setTimeout(() => { api.quit_app(); }, 80);
});
$("done-back").addEventListener("click", () => {
  // Reset backup state so a follow-up export/restore starts clean.
  backupState.mode = null;
  backupState.exportProfilePath = null;
  backupState.exportOutputPath = null;
  backupState.exportIncludes = new Set();
  backupState.restoreArchivePath = null;
  backupState.restoreTargetPath = null;
  backupState.restoreIncludes = new Set();
  backupState.restoreManifest = null;
  setScreen("welcome");
});

$("done-quit").addEventListener("click", async () => {
  const api = Bridge(); if (api) api.quit_app();
});

// ---- error ----------------------------------------------------------------

function finishError(finalState) {
  setScreen("error");
  $("error-summary").textContent = finalState.error || "Something went wrong.";
  const detail = (finalState.trace || "") + "\n\n" + state.logLines.map(l => `[${l.kind}] ${l.message}`).join("\n");
  $("error-body").textContent = detail.trim();
}

$("error-quit").addEventListener("click", async () => {
  const api = Bridge(); if (api) api.quit_app();
});
$("error-copy").addEventListener("click", async () => {
  const api = Bridge();
  if (api) await api.copy_to_clipboard($("error-body").textContent);
});

// ---- backups screen ----------------------------------------------------

const BACKUP_GROUP_LABELS = {
  "zen-sessions.jsonlz4": ["Sessions", "Workspaces, pinned tabs, folders, inline favicons"],
  "favicons.sqlite":      ["Favicons",  "Per-page favicon cache"],
  "places.sqlite":        ["Places",    "Bookmarks and browsing history"],
  "cookies.sqlite":       ["Cookies",   "Login state and per-container cookies"],
};

$("open-backups").addEventListener("click", () => goToBackups());
$("backups-back").addEventListener("click", () => setScreen("welcome"));
$("backups-refresh").addEventListener("click", () => loadBackups());

async function goToBackups() {
  setScreen("backups");
  await loadBackups();
}

async function loadBackups() {
  const api = await whenBridgeReady();
  const list = await api.list_backups();
  renderBackups(list || []);
}

function renderBackups(items) {
  const groups = $("backups-groups");
  clear(groups);

  if (!items.length) { $("backups-empty").style.display = ""; return; }
  $("backups-empty").style.display = "none";

  // group by `original`
  const byOriginal = new Map();
  for (const it of items) {
    if (!byOriginal.has(it.original)) byOriginal.set(it.original, []);
    byOriginal.get(it.original).push(it);
  }
  // sort each group's entries newest first (already from server, but defensive)
  for (const arr of byOriginal.values()) arr.sort((a, b) => b.ts - a.ts);

  // render in known order, then unknown
  const known = Object.keys(BACKUP_GROUP_LABELS);
  const ordered = [
    ...known.filter(k => byOriginal.has(k)),
    ...[...byOriginal.keys()].filter(k => !known.includes(k)),
  ];

  for (const original of ordered) {
    const entries = byOriginal.get(original);
    const [label, desc] = BACKUP_GROUP_LABELS[original] || [original, ""];

    const group = el("div", {class: "backup-group"});
    group.appendChild(el("h3", {text: label + " · " + original}));
    if (desc) group.appendChild(el("p", {class: "desc", text: desc}));
    const ul = el("ul", {class: "backup-list"});
    for (const e of entries) ul.appendChild(makeBackupRow(e));
    group.appendChild(ul);
    groups.appendChild(group);
  }
}

function makeBackupRow(entry) {
  return el("li", {class: "backup-row"}, [
    el("span", {class: "ts", text: entry.iso}),
    el("span", {class: "size", text: humanSize(entry.size)}),
    el("button", {
      class: "btn btn-soft btn-pill",
      text: "Restore",
      onclick: async () => {
        const api = Bridge();
        const r = await api.restore_backup(entry.path);
        if (r && r.ok) {
          await loadBackups();
        } else {
          alert("Restore failed: " + (r && r.error || "unknown error"));
        }
      },
    }),
    el("button", {
      class: "btn btn-soft btn-pill btn-danger",
      text: "Delete",
      onclick: async () => {
        const api = Bridge();
        const r = await api.delete_backup(entry.path);
        if (r && r.ok) await loadBackups();
        else alert("Delete failed: " + (r && r.error || "unknown error"));
      },
    }),
  ]);
}

function humanSize(b) {
  if (b == null) return "—";
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  if (b < 1024 * 1024 * 1024) return (b / (1024*1024)).toFixed(1) + " MB";
  return (b / (1024*1024*1024)).toFixed(2) + " GB";
}

// ---- utilities -----------------------------------------------------------

function currentOptionsJson() {
  return JSON.stringify({
    zenProfilePath: state.selectedZenProfile,
    spaceFilter: null,
    excludedSpaces: Array.isArray(state.options.excludedSpaces)
      ? state.options.excludedSpaces.slice()
      : [],
    foldersCollapsed: state.options.foldersCollapsed,
    includeWorkspaces: state.options.includeWorkspaces,
    includePinnedTabs: state.options.includePinnedTabs,
    includeBookmarks: state.options.includeBookmarks,
    includeFavicons: state.options.includeFavicons,
    includeOpenTabs: state.options.includeOpenTabs,
    includeHistory: state.options.includeHistory,
    includeCookies: state.options.includeCookies,
  });
}

function formatRows(n) {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1e6)  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
  return `${(n / 1e6).toFixed(1)}M`;
}

function shortenPath(p) {
  const home = "/Users/";
  if (p.startsWith(home)) {
    const idx = p.indexOf("/", home.length);
    if (idx > 0) return "~" + p.slice(idx);
  }
  return p;
}

// ---- bootstrap ------------------------------------------------------------

async function setPlatformAttribute() {
  const api = Bridge();
  if (!api) return;
  try {
    const p = await api.platform();
    if (typeof p === "string") document.body.dataset.platform = p;
  } catch (_) { /* best effort */ }
}

async function setAppVersion() {
  const api = Bridge();
  if (!api) return;
  try {
    const v = await api.version();
    if (typeof v === "string" && v) {
      const node = $("ver");
      if (node) node.textContent = `browser2zen · v${v}`;
    }
  } catch (_) { /* best effort */ }
}

window.addEventListener("pywebviewready", () => {
  setPlatformAttribute();
  setAppVersion();
  decorateBranding();
  setScreen("welcome");
});
setTimeout(() => {
  if (!Bridge()) return;
  setPlatformAttribute();
  setAppVersion();
  decorateBranding();
  setScreen("welcome");
}, 200);

// ============================================================================
// Backup & restore (.zenbackup)
//
// Lives off the welcome screen's "Backup or restore Zen" button. Two flows:
//   Export:  pick source profile -> pick destination -> include checklist
//            -> progress (snapshot/bundle/finalize) -> done
//   Restore: pick .zenbackup -> manifest preview -> pick target profile
//            -> include checklist -> progress (preflight/restore/finalize)
//            -> done
// Both flows reuse #screen-progress + #screen-done so the streaming-events
// renderer needs no second copy.

const backupState = {
  mode: null,              // "export" | "restore"
  categories: null,        // canonical [{id,label,default,caveat}] from bridge
  zenProfiles: [],
  // export inputs
  exportProfilePath: null,
  exportOutputPath: null,
  exportIncludes: new Set(),
  // restore inputs
  restoreArchivePath: null,
  restoreTargetPath: null,
  restoreIncludes: new Set(),
  restoreManifest: null,
};

async function loadBackupCategories() {
  if (backupState.categories) return backupState.categories;
  const api = await whenBridgeReady();
  backupState.categories = await api.list_backup_categories();
  return backupState.categories;
}

async function loadZenProfiles() {
  const api = await whenBridgeReady();
  backupState.zenProfiles = await api.list_zen_profiles_json();
  return backupState.zenProfiles;
}

// ---- mode picker ----------------------------------------------------------

$("backup-mode-back").addEventListener("click", () => setScreen("welcome"));
$("backup-mode-export").addEventListener("click", () => goToBackupExport());
$("backup-mode-restore").addEventListener("click", () => goToBackupRestore());

// ---- export ---------------------------------------------------------------

async function goToBackupExport() {
  setScreen("backup-export");
  backupState.mode = "export";
  backupState.exportProfilePath = null;
  backupState.exportOutputPath = null;
  backupState.exportIncludes = new Set();
  await renderBackupExportProfiles();
  await renderBackupCategories("export");
  await updateBackupExportGate();
}

async function renderBackupExportProfiles() {
  const profiles = await loadZenProfiles();
  const sel = $("backup-export-profile");
  clear(sel);
  if (!profiles.length) {
    sel.appendChild(el("option", {value: "", text: "No Zen profile found"}));
    sel.disabled = true;
    backupState.exportProfilePath = null;
    return;
  }
  sel.disabled = false;
  for (const p of profiles) {
    sel.appendChild(el("option", {
      value: p.path,
      text: p.name,
    }));
  }
  backupState.exportProfilePath = profiles[0].path;
  sel.onchange = () => {
    backupState.exportProfilePath = sel.value;
    updateBackupExportGate();
  };
}

async function renderBackupCategories(flow) {
  const cats = await loadBackupCategories();
  const host = $(flow === "export" ? "backup-export-categories" : "backup-restore-categories");
  const target = flow === "export" ? backupState.exportIncludes : backupState.restoreIncludes;
  target.clear();
  for (const c of cats) {
    if (c.default) target.add(c.id);
  }
  // For restore, intersect with the manifest's included list if we have one.
  if (flow === "restore" && backupState.restoreManifest) {
    const manifestIncluded = new Set(backupState.restoreManifest.included || []);
    for (const id of [...target]) {
      if (!manifestIncluded.has(id)) target.delete(id);
    }
  }
  clear(host);
  for (const c of cats) {
    const disabled = flow === "restore"
      && backupState.restoreManifest
      && !(backupState.restoreManifest.included || []).includes(c.id);
    const checkbox = el("input", {
      type: "checkbox",
      checked: target.has(c.id) ? "checked" : null,
    });
    checkbox.checked = target.has(c.id);
    if (disabled) checkbox.disabled = true;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) target.add(c.id);
      else target.delete(c.id);
      if (flow === "export") updateBackupExportGate();
      else updateBackupRestoreGate();
    });
    const card = el("label", {class: "backup-category"}, [
      checkbox,
      el("div", {class: "backup-category-text"}, [
        el("div", {class: "backup-category-label", text: c.label}),
        c.caveat ? el("div", {class: "backup-category-caveat", text: c.caveat}) : null,
      ]),
    ]);
    if (disabled) card.style.opacity = "0.5";
    host.appendChild(card);
  }
}

$("backup-export-back").addEventListener("click", () => setScreen("backup-mode"));

$("backup-export-recheck").addEventListener("click", async () => {
  await loadZenProfiles();
  await updateBackupExportGate();
});

$("backup-export-quit-zen").addEventListener("click", async () => {
  const api = await whenBridgeReady();
  setLoading($("backup-export-quit-zen"), true, "Quitting");
  try { await api.quit_browser("zen"); } catch (e) { /* best-effort */ }
  setLoading($("backup-export-quit-zen"), false);
  await updateBackupExportGate();
});

async function updateBackupExportGate() {
  const issues = [];
  if (!backupState.exportProfilePath) issues.push("No Zen profile found.");
  if (backupState.exportIncludes.size === 0) issues.push("Pick at least one category to include.");
  let zenRunning = false;
  const api = Bridge();
  if (api) {
    try { zenRunning = await api.is_zen_running(); } catch (e) { /* assume not running */ }
  }
  if (zenRunning) issues.unshift("Zen is running — quit it before exporting so SQLite files snapshot cleanly.");

  const gate = $("backup-export-gate");
  const text = $("backup-export-gate-text");
  const go = $("backup-export-go");
  const quitBtn = $("backup-export-quit-zen");
  quitBtn.style.display = zenRunning ? "" : "none";
  if (issues.length === 0) {
    gate.style.display = "none";
    go.disabled = false;
  } else {
    gate.style.display = "";
    text.textContent = issues.join(" ");
    go.disabled = true;
  }
}

$("backup-export-go").addEventListener("click", async () => {
  const api = await whenBridgeReady();
  // Open the save dialog now, then kick off the export. If the user
  // cancels, stay on this screen.
  const filename = "zen-backup-" + new Date().toISOString().slice(0, 10) + ".zenbackup";
  setLoading($("backup-export-go"), true, "Choosing");
  const chosen = await api.choose_path("save", filename);
  setLoading($("backup-export-go"), false);
  if (!chosen) return;
  backupState.exportOutputPath = chosen;
  const includes = JSON.stringify([...backupState.exportIncludes]);
  setLoading($("backup-export-go"), true, "Exporting");
  await api.start_zen_export(
    backupState.exportProfilePath,
    backupState.exportOutputPath,
    includes,
  );
  setLoading($("backup-export-go"), false);
  state.steps = ["snapshot", "bundle", "finalize"];
  state.stepLabels = {
    snapshot: "Reading the Zen profile",
    bundle: "Writing the archive",
    finalize: "Finalising",
  };
  startProgressPolling();
});

// ---- restore --------------------------------------------------------------

async function goToBackupRestore() {
  setScreen("backup-restore");
  backupState.mode = "restore";
  backupState.restoreArchivePath = null;
  backupState.restoreTargetPath = null;
  backupState.restoreIncludes = new Set();
  backupState.restoreManifest = null;
  $("backup-restore-path").textContent = "No file chosen yet.";
  $("backup-restore-path").classList.add("muted");
  $("backup-restore-manifest-row").style.display = "none";
  await renderBackupRestoreProfiles();
  await renderBackupCategories("restore");
  await updateBackupRestoreGate();
}

async function renderBackupRestoreProfiles() {
  const profiles = await loadZenProfiles();
  const sel = $("backup-restore-profile");
  clear(sel);
  if (!profiles.length) {
    sel.appendChild(el("option", {value: "", text: "No Zen profile found"}));
    sel.disabled = true;
    backupState.restoreTargetPath = null;
    return;
  }
  sel.disabled = false;
  for (const p of profiles) {
    sel.appendChild(el("option", {
      value: p.path,
      text: p.name,
    }));
  }
  backupState.restoreTargetPath = profiles[0].path;
  sel.onchange = () => {
    backupState.restoreTargetPath = sel.value;
    updateBackupRestoreGate();
  };
}

$("backup-restore-pick").addEventListener("click", async () => {
  const api = await whenBridgeReady();
  const chosen = await api.choose_path("open");
  if (!chosen) return;
  backupState.restoreArchivePath = chosen;
  $("backup-restore-path").textContent = chosen;
  $("backup-restore-path").classList.remove("muted");

  const preview = await api.preview_zen_backup(chosen);
  if (preview.ok) {
    backupState.restoreManifest = preview.manifest;
    renderBackupRestoreManifest(preview.manifest);
    await renderBackupCategories("restore");
  } else {
    backupState.restoreManifest = null;
    $("backup-restore-manifest-row").style.display = "";
    const host = $("backup-restore-manifest");
    clear(host);
    host.appendChild(el("span", {class: "muted",
      text: "Couldn't read this archive: " + (preview.errors || []).join(", ")}));
  }
  updateBackupRestoreGate();
});

function renderBackupRestoreManifest(manifest) {
  $("backup-restore-manifest-row").style.display = "";
  const host = $("backup-restore-manifest");
  clear(host);
  const includedNice = (manifest.included || []).join(", ");
  host.appendChild(el("div", {}, [
    el("span", {class: "name", text: manifest.source_profile_name || "Zen profile"}),
    el("span", {text: ", exported " + (manifest.exported_at || "")}),
  ]));
  host.appendChild(el("div", {class: "muted", style: "font-size: var(--fs-tiny);",
    text: "Includes: " + includedNice}));
  host.appendChild(el("div", {class: "muted", style: "font-size: var(--fs-tiny);",
    text: "Format v" + (manifest.format_version || "?")
          + " · browser2zen " + (manifest.browser2zen_version || "?")}));
}

$("backup-restore-back").addEventListener("click", () => setScreen("backup-mode"));

$("backup-restore-recheck").addEventListener("click", async () => {
  await loadZenProfiles();
  await updateBackupRestoreGate();
});

$("backup-restore-quit-zen").addEventListener("click", async () => {
  const api = await whenBridgeReady();
  setLoading($("backup-restore-quit-zen"), true, "Quitting");
  try { await api.quit_browser("zen"); } catch (e) { /* best-effort */ }
  setLoading($("backup-restore-quit-zen"), false);
  await updateBackupRestoreGate();
});

async function updateBackupRestoreGate() {
  const issues = [];
  if (!backupState.restoreArchivePath) issues.push("Pick a .zenbackup file.");
  else if (!backupState.restoreManifest) issues.push("That archive is unreadable.");
  if (!backupState.restoreTargetPath) issues.push("No target Zen profile found.");
  if (backupState.restoreManifest && backupState.restoreIncludes.size === 0) {
    issues.push("Pick at least one category to restore.");
  }
  let zenRunning = false;
  const api = Bridge();
  if (api) {
    try { zenRunning = await api.is_zen_running(); } catch (e) { /* assume not running */ }
  }
  if (zenRunning) issues.unshift("Zen is running — quit it before restoring so the writes don't fight live reads.");

  const gate = $("backup-restore-gate");
  const text = $("backup-restore-gate-text");
  const go = $("backup-restore-go");
  const quitBtn = $("backup-restore-quit-zen");
  quitBtn.style.display = zenRunning ? "" : "none";
  if (issues.length === 0) {
    gate.style.display = "none";
    go.disabled = false;
  } else {
    gate.style.display = "";
    text.textContent = issues.join(" ");
    go.disabled = true;
  }
}

$("backup-restore-go").addEventListener("click", async () => {
  const api = await whenBridgeReady();
  const includes = JSON.stringify([...backupState.restoreIncludes]);
  setLoading($("backup-restore-go"), true, "Restoring");
  await api.start_zen_restore(
    backupState.restoreArchivePath,
    backupState.restoreTargetPath,
    includes,
  );
  setLoading($("backup-restore-go"), false);
  state.steps = ["preflight", "restore", "finalize"];
  state.stepLabels = {
    preflight: "Validating the archive",
    restore: "Restoring files",
    finalize: "Finalising",
  };
  startProgressPolling();
});

// Helper: kick off the progress poller for a non-migration job. Reuses
// the migrate flow's renderProgress() + pollProgress() since both read
// the same step / event shape from drain_progress.
function startProgressPolling() {
  setScreen("progress");
  state.stepStates = {};
  state.stepSummaries = {};
  state.logLines = [];
  state.activeStep = null;
  state.activeDetail = "";
  state.activeProgress = null;
  state.startedAt = Date.now();
  if (state.elapsedHandle) clearInterval(state.elapsedHandle);
  state.elapsedHandle = setInterval(updateElapsed, 1000);
  updateElapsed();
  renderProgress();
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = setInterval(pollProgress, 120);
}

// Screenshot harness hook. The Playwright runner sets `window.__SHOOT__`
// in addInitScript before navigating, which lets it reach into the
// closure-captured app state to seed each screen deterministically.
// Stays a no-op in production because nothing outside docs/screenshots
// ever sets that flag.
if (typeof window !== "undefined" && window.__SHOOT__) {
  window.__shoot = {
    state, backupState,
    setScreen, finishOk,
    runSourcePicker, runDetect,
    goToBackupExport, goToBackupRestore,
    decorateBranding, setAppVersion, setPlatformAttribute,
  };
}
