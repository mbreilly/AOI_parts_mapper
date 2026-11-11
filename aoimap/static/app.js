"use strict";
// AOI parts mapper page. The server keeps all state; this page shows it and sends the user's clicks.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let S = null;                 // server state
let tab = "files";
const ui = {
  lineFilter: "all", lineSearch: "", openLines: new Set(), highlight: null, marks: true,
  pts: { front: {}, back: {} }, cur: { front: 0, back: 0 },
  anchors: { front: null, back: null },
  closeup: null,              // {side, index, pts, cur} while working on a closeup
  ghostOn: true, ghostOp: 35, stream: null, rot: {},
  pick: true,                 // clicks on a lined-up photo pick a part (not a fiducial); off while the setup is open
  look: { front: null, back: null },   // the part selected on each side
  sideStat: "all", partsStat: "all", setupOpen: false, badPick: false,
};
try { Object.assign(ui, JSON.parse(localStorage.getItem("aoimap-ui") || "{}"), { pts: { front: {}, back: {} }, closeup: null, stream: null, anchors: { front: null, back: null }, rot: {} }); } catch (e) { /* private window */ }
if (!["all", "in", "out"].includes(ui.lineFilter)) ui.lineFilter = "all";
const keepUi = () => { try { localStorage.setItem("aoimap-ui", JSON.stringify({ lineFilter: ui.lineFilter, ghostOn: ui.ghostOn, ghostOp: ui.ghostOp, marks: ui.marks })); } catch (e) { } };

const KIND_NAMES = { ic: "IC", tantalum: "Tantalum", electrolytic: "Electrolytic", diode: "Diode", led: "LED", other: "Other", look: "Look only" };
const SIDES = { front: "top", back: "bot" };
const ORIENTS = {
  front: [{ v: [1, -1], t: "TOP EDGE UP", d: "as the design shows it" }, { v: [-1, 1], t: "TURNED 180°", d: "top edge at the bottom" }],
  back: [{ v: [1, 1], t: "FLIPPED TOP TO BOTTOM", d: "turned over about its long edge" },
         { v: [-1, -1], t: "FLIPPED LEFT TO RIGHT", d: "turned over about its short edge" }],
};

// ---- talking to the server ---------------------------------------------------------------
function say(text, err = false) {
  const m = $("#msg");
  if (!text) { m.hidden = true; return; }
  m.hidden = false; m.textContent = text; m.className = "msg" + (err ? " err" : ""); m.title = "Click to close";
}
$("#msg").addEventListener("click", () => say(""));
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { throw new Error(`The app didn't answer (${r.status}).`); }
  if (!r.ok || j.error) throw new Error(j.error || `Error ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const upload = (path, blob) => api(path, { method: "POST", body: blob });
async function act(fn, done) {
  try { const st = await fn(); if (st && st.sides !== undefined) { S = st; S._r = Date.now(); } if (done) say(done); render(); }
  catch (e) { say(e.message, true); }
  if (S && (S.group ? S.group.id : null) !== G.cur) loadGroups();   // loading a board can choose its project
}

// ---- the project chosen above the tabs -------------------------------------------------------
// A project is a product (PTC, say) and its board design. The tabs show only its boards, references,
// checks and learned examples; the server remembers the one chosen last.
const G = { list: [], cur: null, form: null };
const curGroup = () => G.list.find(g => g.id === G.cur) || null;
const inGroup = board => { const g = curGroup(); return !g || g.boards.includes(board); };
async function loadGroups() {
  try { const r = await api("api/groups"); G.list = r.projects; G.cur = r.current; } catch (e) { say(e.message, true); }
  renderProjBar();
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function renderProjBar() {
  const g = curGroup();
  const opts = (G.cur ? "" : `<option value="">(choose a project)</option>`) +
    G.list.map(x => `<option value="${esc(x.id)}" ${x.id === G.cur ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  if (opts !== G.opts) { $("#pg-select").innerHTML = opts; G.opts = opts; }     // left alone while unchanged, so an open list stays open
  $("#pg-info").textContent = !g ? (G.list.length ? "" : "No project yet: press NEW PROJECT.") : !g.boards.length ? "No board design yet: load its ODB++ and BOM on Files." :
    `Design ${g.boards.join(", ")}  ·  ${plural(g.mapped, "board")} mapped` +
    `  ·  ${plural(g.references, "reference")}  ·  ${plural(g.checks, "check")}  ·  ${g.learned} learned`;
  $("#pg-rename").disabled = !g;
  $("#pg-form").hidden = !G.form; $("#pg-info").hidden = !!G.form;
  $("#pg-ok").textContent = G.form === "rename" ? "RENAME" : "CREATE";
}
function groupsDone(r, done) {
  G.list = r.groups.projects; G.cur = r.groups.current; G.form = null;
  S = r.state; S._r = Date.now(); recentAt = 0;
  H.sel = null; $("#hist-detail").innerHTML = ""; CK.anchors = null;
  renderProjBar();
  if (!S.design && !["files", "history", "check"].includes(tab)) tab = "files";
  goTab(tab);
  if (done) say(done);
}
$("#pg-select").addEventListener("change", async e => {
  if (!e.target.value) return;
  try {
    const r = await post("api/groups/switch", { id: e.target.value });
    const g = r.groups.projects.find(x => x.id === r.groups.current);
    groupsDone(r, `Project ${g.name}` + (r.state.project ? `: ${r.state.project.board}${r.state.project.serial ? " " + r.state.project.serial : ""} is open.` : ". Load its ODB++ and BOM on Files."));
  } catch (err) { say(err.message, true); renderProjBar(); }
});
// MORE menus close once a choice is made, or on a click anywhere else
document.addEventListener("click", e => {
  for (const d of $$("details.more[open]")) if (!d.contains(e.target) || e.target.closest(".menu > *")) d.open = false;
});
$("#pg-new").addEventListener("click", () => { G.form = "new"; $("#pg-name").value = ""; renderProjBar(); $("#pg-name").focus(); });
$("#pg-rename").addEventListener("click", () => { const g = curGroup(); if (!g) return; G.form = "rename"; $("#pg-name").value = g.name; renderProjBar(); $("#pg-name").focus(); });
$("#pg-cancel").addEventListener("click", () => { G.form = null; renderProjBar(); });
$("#pg-name").addEventListener("keydown", e => { if (e.key === "Enter") $("#pg-ok").click(); if (e.key === "Escape") $("#pg-cancel").click(); });
$("#pg-ok").addEventListener("click", async () => {
  const name = $("#pg-name").value.trim();
  try {
    if (G.form === "rename") {
      const r = await post("api/groups/rename", { id: G.cur, name });
      G.list = r.groups.projects; G.form = null; S = r.state; S._r = Date.now(); renderProjBar(); render(); say(`Project renamed ${name}.`);
    } else {
      stopCamera(); stopCk();
      groupsDone(await post("api/groups/new", { name }), `Project ${name} made. Now load its ODB++ and BOM.`);
      goTab("files");
      say(`Project ${name} made. Now load its ODB++ and BOM below.`);
    }
  } catch (e) { say(e.message, true); }
});

// ---- one part status, the same words and colours on every tab -----------------------------
// From the viewer's status (viewer.py): answered on this board's photo (GOOD, BAD, MISSING, NS), in the
// check (its photo shows it, or there's no lined-up photo yet), in the check but the photo can't show
// it, or not in the check.
const STATUS = {
  in: { t: "IN THE CHECK", n: "in the check", tip: "In the check, not answered on this board" },
  good: { t: "GOOD", n: "good", tip: "Answered GOOD on this board" },
  bad: { t: "BAD", n: "bad", tip: "Answered BAD on this board" },
  missing: { t: "MISSING", n: "missing", tip: "Answered MISSING on this board" },
  ns: { t: "NS", n: "NS", tip: "Not stuffed: left off on purpose" },
  cant: { t: "CAN'T BE SEEN", n: "can't be seen", tip: "In the check, but the photo can't show it (too small, hidden, off the photo), or covered on this board (a heatsink...)" },
  off: { t: "NOT CHECKED", n: "not checked", tip: "In the design, not in the check" },
};
const STATUS_COL = { in: "#5BB8F5", good: "#6FCF73", bad: "#FF5C5C", missing: "#FF9A3D", ns: "#B4B4B4", cant: "#C49BFF", off: "#8A909C" };
function ustat(o) {
  if (!o) return "off";
  return { good: "good", bad: o.reason === "missing" ? "missing" : "bad", ns: "ns", covered: "cant", checked: "in", nophoto: "in", skipped: "cant" }[o.st] || "off";
}
// COVERED on this board (a heatsink, a label...): shown as CAN'T BE SEEN, with what covers it
const covNote = o => o && o.st === "covered" ? ` · covered: ${esc(o.cover || "heatsink")}` : "";
const chip = (k, extra = "", cls = "") => `<span class="chip st-${k} ${cls}" title="${esc(STATUS[k].tip)}">${STATUS[k].t}${extra}</span>`;
// what a count in the legend picks: "check" is everything in the check, answered or not
const STAT_PICK = { all: () => true, check: k => k !== "off", in: k => k === "in", good: k => k === "good", wrong: k => k === "bad" || k === "missing", missing: k => k === "missing",
  ns: k => k === "ns", cant: k => k === "cant", off: k => k === "off" };
// the legend with counts: always on screen under a board; a count is a filter (again: all)
function statusBar(el, keys, cur, noun) {
  const n = k => keys.filter(STAT_PICK[k]).length;
  const item = (f, sk, label) => `<button class="count st-${sk} ${cur === f ? "on" : ""}" data-stat="${f}" title="${esc(sk === "all" ? "Show every part" : f === "check" ? "Every part in the check, answered or not" : STATUS[sk].tip)}${f === "all" ? "" : " · click to show only these"}"><i></i><b>${n(f)}</b><span>${label}</span></button>`;
  el.innerHTML = `<button class="count all ${cur === "all" ? "on" : ""}" data-stat="all"><b>${keys.length}</b><span>${noun}</span></button>`
    + item("check", "in", "in the check") + item("good", "good", "good") + item("wrong", "bad", "wrong")
    + item("missing", "missing", "missing")
    + item("ns", "ns", "NS") + item("cant", "cant", "can't be seen") + item("off", "off", "not checked");
}
// the whole design's status, read again whenever the server state changes
const PS = { key: null, parts: {}, wait: null };
async function loadStatus(force = false) {
  if (!S || !S.design || !S.project) { PS.parts = {}; return PS.parts; }
  const key = `${S.project.uid}|${S._r || ""}|${S._v || ""}`;
  if (!force && PS.key === key) return PS.parts;
  if (PS.wait && PS.waitKey === key && !force) return PS.wait;
  PS.waitKey = key;
  PS.wait = api("api/viewer/status").then(r => { PS.parts = r.parts; PS.key = key; return PS.parts; })
    .catch(() => PS.parts).finally(() => { PS.wait = null; });
  return PS.wait;
}
const pstat = ref => ustat(PS.parts[ref]);
const pchip = (ref, cls = "") => chip(pstat(ref), covNote(PS.parts[ref]), cls);

// ---- photo viewers: wheel or pinch to zoom, drag to move, − + FIT 100% -------------------------
// The stage is the photo at its own pixel size; a transform scales and moves it inside the viewer.
function zoomer(view, stage, bar) {
  const z = { k: 1, tx: 0, ty: 0, w: 0, h: 0, fitted: true, moved: false };
  if (bar) bar.innerHTML = `<button data-z="out" title="Zoom out (or the mouse wheel)">−</button><output>100%</output><button data-z="in" title="Zoom in">+</button>`
    + `<button data-z="fit" title="Show the whole photo">FIT</button><button data-z="one" title="One photo pixel per screen pixel">100%</button>`;
  const fitK = () => { const r = view.getBoundingClientRect(); return z.w && r.width ? Math.min(r.width / z.w, r.height / z.h) : 1; };
  z.apply = () => {
    stage.style.width = z.w + "px"; stage.style.height = z.h + "px";
    stage.style.transform = `translate(${z.tx}px, ${z.ty}px) scale(${z.k})`;
    if (bar) $("output", bar).textContent = Math.round(z.k * 100) + "%";
    if (z.onchange) z.onchange();
  };
  z.fit = () => {
    const r = view.getBoundingClientRect(); if (!z.w || !r.width) return;
    z.k = fitK(); z.tx = (r.width - z.w * z.k) / 2; z.ty = (r.height - z.h * z.k) / 2; z.fitted = true; z.apply();
  };
  z.size = (w, h) => { if (w && h && (w !== z.w || h !== z.h)) { z.w = w; z.h = h; z.fit(); } };
  z.at = (k, cx, cy) => {
    const nk = Math.min(Math.max(k, fitK() * 0.5), 8);
    z.tx = cx - (cx - z.tx) * nk / z.k; z.ty = cy - (cy - z.ty) * nk / z.k; z.k = nk; z.fitted = false; z.apply();
  };
  const mid = () => { const r = view.getBoundingClientRect(); return [r.width / 2, r.height / 2]; };
  // bring a point of the photo (stage pixels) into view, keeping the zoom
  z.show = (x, y) => {
    const r = view.getBoundingClientRect(), sx = z.tx + x * z.k, sy = z.ty + y * z.k, m = 30;
    if (z.fitted || (sx > m && sy > m && sx < r.width - m && sy < r.height - m)) return;
    z.tx = r.width / 2 - x * z.k; z.ty = r.height / 2 - y * z.k; z.fitted = false; z.apply();
  };
  if (bar) bar.addEventListener("click", e => {
    const b = e.target.closest("[data-z]"); if (!b) return;
    const [cx, cy] = mid();
    ({ in: () => z.at(z.k * 1.4, cx, cy), out: () => z.at(z.k / 1.4, cx, cy), fit: z.fit, one: () => z.at(1, cx, cy) })[b.dataset.z]();
  });
  view.addEventListener("wheel", e => {
    if (!z.w) return;
    e.preventDefault();
    const r = view.getBoundingClientRect();
    z.at(z.k * Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  const ptrs = new Map();
  let drag = null, pinch = null;
  view.addEventListener("pointerdown", e => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    z.moved = false;
    if (ptrs.size === 1) drag = { x: e.clientX, y: e.clientY, tx: z.tx, ty: z.ty, id: e.pointerId };
    if (ptrs.size === 2) {
      const [a, b] = [...ptrs.values()], r = view.getBoundingClientRect();
      pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: z.k, cx: (a[0] + b[0]) / 2 - r.left, cy: (a[1] + b[1]) / 2 - r.top };
      drag = null;
    }
  });
  view.addEventListener("pointermove", e => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    if (pinch && ptrs.size === 2) {
      const [a, b] = [...ptrs.values()];
      z.moved = true; z.at(pinch.k * Math.hypot(a[0] - b[0], a[1] - b[1]) / pinch.d, pinch.cx, pinch.cy);
    } else if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!z.moved && Math.abs(dx) + Math.abs(dy) > 5) { z.moved = true; view.classList.add("drag"); try { view.setPointerCapture(drag.id); } catch (x) { } }
      if (z.moved) { z.tx = drag.tx + dx; z.ty = drag.ty + dy; z.fitted = false; z.apply(); }
    }
  });
  const up = e => { ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null; if (!ptrs.size) { drag = null; view.classList.remove("drag"); } };
  view.addEventListener("pointerup", up); view.addEventListener("pointercancel", up);
  // a drag isn't a click: no fiducial is placed and no part picked
  view.addEventListener("click", e => { if (z.moved) { e.stopPropagation(); e.preventDefault(); z.moved = false; } }, true);
  new ResizeObserver(() => { if (z.fitted) z.fit(); }).observe(view);
  return z;
}
const ZS = zoomer($("#viewer"), $("#stage"), $("#zoom-side"));
// outlines and labels keep their size on screen whatever the zoom
ZS.onchange = () => $("#stage").style.setProperty("--u", (1 / ZS.k).toFixed(4) + "px");
const ZC = zoomer($("#ck-viewer"), $("#ck-stage"), $("#zoom-ck"));
// ---- tabs ----------------------------------------------------------------------------------
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.disabled) return;
  goTab(b.dataset.tab);
});
function goTab(t) {
  tab = t; ui.closeup = null; stopCamera(); stopCk(); say("");
  render();
  if (tab === "front" || tab === "back") { ui.badPick = false; loadAnchors(tab); }
  if (tab === "history") loadHistory();
  if (tab === "check") loadCheck();
  if (tab === "models") loadModels();
  if (tab === "review") loadReview();
  if (tab === "map" && S && S.project) bvOpen();
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-goto]"); if (!b) return;
  if (b.dataset.hist) setHistKind(b.dataset.hist, false);
  goTab(b.dataset.goto);
});

function render() {
  $$("#tabs button").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === tab);
    b.disabled = !["files", "history", "check", "models"].includes(b.dataset.tab) && !(S && S.design);
  });
  $("#tab-files").hidden = tab !== "files";
  $("#tab-parts").hidden = tab !== "parts";
  $("#tab-side").hidden = !(tab === "front" || tab === "back");
  $("#tab-map").hidden = tab !== "map";
  $("#tab-history").hidden = tab !== "history";
  $("#tab-check").hidden = tab !== "check";
  $("#tab-models").hidden = tab !== "models";
  $("#tab-review").hidden = tab !== "review";
  $("#sub").textContent = S && S.design ? `Open: ${S.design.name}${S.project && S.project.serial ? "  ·  " + S.project.serial : ""}  ·  ${S.design.units}` : "ODB++  ·  BOM  ·  photo map";
  if (tab === "files") renderFiles();
  if (tab === "parts") renderParts();
  if (tab === "front" || tab === "back") renderSide(tab);
  if (tab === "map") renderMap();
  // the parts' status: shown on Parts, Front, Back and Map, read again when the state changes
  if (S && S.project && ["parts", "front", "back", "map"].includes(tab)) {
    const before = PS.key;
    loadStatus().then(() => { if (PS.key !== before) statusChanged(); });
  }
}
function statusChanged() {
  if (tab === "parts") renderParts();
  if (tab === "front" || tab === "back") renderSide(tab);
  if (tab === "map") { BV.st = PS.parts; bvInfo(); bvDraw(); mapLegend(); }
}

// ---- 1 files -------------------------------------------------------------------------------
function setupDrop(id, kind) {
  const lab = $(`#drop-${kind}`), input = $(`#file-${kind}`);
  const send = f => act(() => upload(`api/upload?kind=${kind}&name=${encodeURIComponent(f.name)}`, f), (S && S.messages && S.messages.join(" ")) || "");
  input.addEventListener("change", () => input.files[0] && send(input.files[0]).then(showMessages));
  lab.addEventListener("dragover", e => { e.preventDefault(); lab.classList.add("over"); });
  lab.addEventListener("dragleave", () => lab.classList.remove("over"));
  lab.addEventListener("drop", e => { e.preventDefault(); lab.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) send(f).then(showMessages); });
}
const showMessages = () => { if (S && S.messages && S.messages.length) say(S.messages.join(" ")); };
setupDrop("odb", "odb"); setupDrop("bom", "bom");

function renderFiles() {
  if (!S) return;
  $("#drop-odb").classList.toggle("done", S.files.odb); $("#odb-state").textContent = S.files.odb ? "Loaded" : "Not loaded";
  $("#drop-bom").classList.toggle("done", S.files.bom); $("#bom-state").textContent = S.files.bom ? "Loaded" : "Not loaded";
  const p = S.project;
  $("#project-box").hidden = !p;
  if (p) {
    if (document.activeElement !== $("#proj-serial")) $("#proj-serial").value = p.serial || "";
    if (document.activeElement !== $("#proj-notes")) $("#proj-notes").value = p.notes || "";
    $("#proj-uid").textContent = `Project ${p.uid}`;
  }
  loadRecent();
  const d = S.design;
  $("#design-box").hidden = !d;
  if (!d) return;
  const c = d.counts, sideLine = s => c[s] ? Object.entries(c[s]).map(([k, n]) => `${n} ${KIND_NAMES[k] || k}`).join(", ") : "none";
  const o = d.outline, w = (o.xmax - o.xmin), h = (o.ymax - o.ymin);
  let html = `<div class="kv">
    <b>Board</b><span>${esc(d.name)}</span>
    <b>Size</b><span>${w.toFixed(3)} × ${h.toFixed(3)} ${d.units}${d.units_known ? "" : " (units assumed)"}</span>
    <b>Fiducials</b><span>top ${Object.values(d.fiducials).filter(f => f.side === "top").length}, bottom ${Object.values(d.fiducials).filter(f => f.side === "bot").length}</span>
    <b>In the check, front</b><span>${sideLine("top")}</span>
    <b>In the check, back</b><span>${sideLine("bot")}</span>
    <b>Rotation</b><span>${d.rot_sign === -1 ? "clockwise" : "anticlockwise"}${d.rot_note ? " · " + esc(d.rot_note) : ""}</span></div>`;
  for (const n of d.notes) html += `<div class="warn">${esc(n)}</div>`;
  if (d.bom_only.length) html += `<div class="warn">In the BOM but not on the board: ${esc(d.bom_only.slice(0, 40).join(", "))}${d.bom_only.length > 40 ? " …" : ""}</div>`;
  if (d.odb_only_count) html += `<div class="warn">On the board but not in the BOM: ${d.odb_only_count} (${esc(d.odb_only.slice(0, 25).join(", "))}${d.odb_only_count > 25 ? " …" : ""})</div>`;
  if (d.dnp_on_board.length) html += `<div class="warn">Marked DNP in the BOM, left out: ${esc(d.dnp_on_board.join(", "))}</div>`;
  $("#design-summary").innerHTML = html;
  const steps = S.steps || [];
  $("#step-row").hidden = steps.length < 2;
  $("#step").innerHTML = steps.map(s => `<option value="${esc(s.name)}" ${s.name === (S.profile.step || steps[0].name) ? "selected" : ""}>${esc(s.name)}${s.has_components ? "" : " (no parts)"}</option>`).join("");
  const fields = { refdes: "Reference", value: "Part number", description: "Description", type: "Type", dnp: "DNP" };
  $("#columns").innerHTML = Object.entries(fields).map(([f, label]) => `<span class="lab">${label.toUpperCase()}</span>
    <select data-col="${f}"><option value="">(none)</option>${S.bom_header.map((h, i) => `<option value="${i}" ${S.bom_columns[f] === i ? "selected" : ""}>${esc(h || "column " + (i + 1))}</option>`).join("")}</select>`).join("");
}
let recentAt = 0;
async function loadRecent() {
  if (Date.now() - recentAt < 3000) return;      // render() runs often; the list rarely changes
  recentAt = Date.now();
  try {
    const h = await api("api/history?limit=6");
    loadGroups();                                  // the project bar's counts follow the list
    $("#boards").innerHTML = h.projects.length ? h.projects.map(r => `<div class="${r.uid === h.current ? "cur" : ""}"><span class="grow">${esc(r.board)}${r.serial ? ` <span class="ok">${esc(r.serial)}</span>` : ""}<br><span class="dim">${esc(r.updated)} · ${sideBrief(r, "front")} · ${sideBrief(r, "back")}</span></span>${r.uid === h.current ? `<span class="dim">OPEN NOW</span>` : r.missing ? `<span class="dim" title="Only its database record is left">FOLDER GONE</span>` : `<button data-reopen="${esc(r.uid)}">OPEN</button>`}</div>`).join("")
      : `<div class="dim">None yet in this project. A board is saved as soon as its ODB++ and BOM are in.</div>`;
  } catch (e) { $("#boards").innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
}
const sideBrief = (r, side) => {
  const s = r[side], n = side === "front" ? "front" : "back";
  if (!s.parts) return `${n} none`;
  return s.aligned ? `${n} ${s.checkable}/${s.parts}` : s.photo ? `${n} photo` : `${n} –`;
};
// The list is reloaded before render() so it moves OPEN NOW straight away: two boards of one design
// otherwise look the same, and the button seemed to do nothing.
async function projectAct(btn, busy, fn, done) {
  const was = btn && btn.textContent;
  if (btn) { btn.disabled = true; btn.textContent = busy; }
  say(busy);
  await act(async () => { const st = await fn(); recentAt = 0; return st; }, done);
  if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = was; }
  if (S && S.design) goTab("files");
}
function reopen(uid, btn) {
  return projectAct(btn, "OPENING...", () => post(`api/history/${encodeURIComponent(uid)}/open`, {}),
    "Reopened the project as it was left.").then(() => S.project && S.project.uid === uid &&
      say(`Opened ${S.project.board || S.name}${S.project.serial ? " " + S.project.serial : ""} as it was left.${S.messages && S.messages.length ? " " + S.messages.join(" ") : ""}`));
}
function newFrom(uid, btn) {
  return projectAct(btn, "STARTING...", () => post(`api/history/${encodeURIComponent(uid)}/new`, {}),
    "New project for another board of the same design. Its choices are carried over; take the photos.");
}
document.addEventListener("click", e => {
  const o = e.target.closest("[data-reopen]"); if (o && !o.disabled) reopen(o.dataset.reopen, o);
  const n = e.target.closest("[data-newfrom]"); if (n && !n.disabled) newFrom(n.dataset.newfrom, n);
});
async function saveInfo(uid, body) {
  try { const d = await post(`api/history/${encodeURIComponent(uid)}/info`, body); recentAt = 0; return d; }
  catch (e) { say(e.message, true); }
}
for (const id of ["#proj-serial", "#proj-notes"]) $(id).addEventListener("change", async () => {
  if (!S || !S.project) return;
  await saveInfo(S.project.uid, { serial: $("#proj-serial").value, notes: $("#proj-notes").value });
  S.project.serial = $("#proj-serial").value.trim(); S.project.notes = $("#proj-notes").value;
  say("Saved with this project."); render();
});
$("#proj-new").addEventListener("click", () => S && S.project && newFrom(S.project.uid));
$("#step").addEventListener("change", e => act(() => post("api/option", { key: "step", value: e.target.value })));
$("#columns").addEventListener("change", e => {
  const cols = {}; $$("#columns select").forEach(s => cols[s.dataset.col] = s.value === "" ? null : +s.value);
  act(() => post("api/columns", cols), "BOM columns updated.");
});

// ---- 2 parts -------------------------------------------------------------------------------
function renderParts() {
  const q = ui.lineSearch.toLowerCase();
  // the status counts cover every part of the design; a count picks the lines with such a part
  const refs = Object.keys(PS.parts).filter(r => !(S.design.fiducials || {})[r]);
  statusBar($("#parts-legend"), refs.map(pstat), ui.partsStat, "parts in the design");
  const pick = STAT_PICK[ui.partsStat] || STAT_PICK.all;
  const lines = S.lines.filter(l => (ui.lineFilter === "all" || (ui.lineFilter === "in") === !!l.chosen_kind) &&
    (!q || (l.value + " " + l.description + " " + l.refs.join(" ")).toLowerCase().includes(q)) &&
    (ui.partsStat === "all" || l.refs.some(r => PS.parts[r] && pick(pstat(r)))));
  $$("#line-filter button").forEach(b => b.classList.toggle("on", b.dataset.f === ui.lineFilter));
  const nIn = S.lines.filter(l => l.chosen_kind).length;
  $("#line-count").textContent = `${nIn} of ${S.lines.length} BOM lines in the check${lines.length !== S.lines.length ? ` · ${lines.length} shown` : ""}`;
  // header and cells come from one list, so they can't drift apart
  $("#lines thead").innerHTML = `<tr>${LINE_COLS.map(c => `<th title="${esc(c.tip || "")}">${c.head}</th>`).join("")}</tr>`;
  $("#lines tbody").innerHTML = lines.map(l => {
    const open = ui.openLines.has(l.key);
    return `<tr data-key="${esc(l.key)}" class="${l.dnp ? "dnp" : ""}">${LINE_COLS.map(c => `<td class="${c.cls || ""}" ${c.attr ? c.attr(l) : ""}>${c.cell(l, open)}</td>`).join("")}</tr>`
      + (open ? `<tr class="sub"><td></td><td colspan="${LINE_COLS.length - 1}">${lineRefs(l)}</td></tr>` : "");
  }).join("") || `<tr><td colspan="${LINE_COLS.length}" class="no">No lines match.</td></tr>`;
  const mk = S.profile.markings;
  $("#markings").innerHTML = ["tantalum", "electrolytic", "diode", "led"].map(k => `<div class="row"><span class="lab" style="width:110px">${KIND_NAMES[k].toUpperCase()}</span>
    <div class="seg" data-mark="${k}"><button data-end="pin1" class="${mk[k] === "pin1" ? "on" : ""}">AT PIN 1</button><button data-end="other" class="${mk[k] === "other" ? "on" : ""}">AT PIN 2</button></div></div>`).join("");
  const rs = S.profile.rot_sign;
  $$("#rot-sign button").forEach(b => b.classList.toggle("on", String(rs ?? "auto") === b.dataset.v));
  const votes = S.design.rotation_votes;
  $("#rot-note").textContent = rs == null
    ? (votes["1"] || votes["-1"] ? `Auto picked ${S.design.rot_sign === -1 ? "clockwise" : "anticlockwise"} from the pin 1 positions (${votes["-1"]} part(s) say clockwise, ${votes["1"]} anticlockwise).`
      : "Auto: the design's pins can't tell, so the photo step shows a check if any part would look different.")
    : "Set by hand.";
  $("#rot-note-short").textContent = rs == null ? `Auto: ${S.design.rot_sign === -1 ? "clockwise" : "anticlockwise"}` : `Set by hand: ${rs === -1 ? "clockwise" : "anticlockwise"}`;
  if (document.activeElement !== $("#minpx")) $("#minpx").value = S.profile.min_px;
  $("#minpx-v").textContent = `${$("#minpx").value} px`;
  $("#use-tant").checked = !!S.profile.use_tantalums;
  // parts added by hand
  if (S.project) loadPartList(S.project.uid);
  if (!$("#xp-kind").options.length) $("#xp-kind").innerHTML = kindOptions();
  $("#xp-list").innerHTML = (S.extras || []).length ? `<div class="lab">ADDED ONE BY ONE (${S.extras.length})</div>` : "";
  $("#xp-list").innerHTML += (S.extras || []).map(x => `<div><span class="grow"><b>${esc(x.ref)}</b> ${pchip(x.ref)} ${esc(KIND_NAMES[x.kind] || x.kind)} · ${x.side}
      <span class="dim">${esc(x.value || x.package)}${x.in_bom ? "" : " · not in the BOM"}${x.missing ? " · not in this design!" : ""} · ${x.good} good, ${x.bad} bad${x.wait && !(x.good + x.bad) ? " · waiting for a new photo" : ""}</span></span>
    <button class="small" data-xtrain="${esc(x.ref)}" data-side="${x.side}">TRAIN</button>
    <button class="small ${x.wait ? "on" : ""}" data-xwait="${esc(x.ref)}" title="Ask about it on the next check of its side until it's answered">WAIT FOR PHOTO</button>
    <button class="small" data-xdel="${esc(x.ref)}">REMOVE</button></div>`).join("");
}
const kindOptions = (sel = "look") => Object.entries(KIND_NAMES).map(([k, t]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${esc(t)}</option>`).join("");
$("#parts-legend").addEventListener("click", e => { const b = e.target.closest("[data-stat]"); if (b) { ui.partsStat = ui.partsStat === b.dataset.stat ? "all" : b.dataset.stat; renderParts(); } });
$("#minpx").addEventListener("input", e => { $("#minpx-v").textContent = `${e.target.value} px`; });
$("#minpx").addEventListener("change", e => act(() => post("api/option", { key: "min_px", value: +e.target.value })));
// whether a BOM line's parts are checked on the board open here, by side
const SIDE_SHORT = { front: "FRONT", back: "BACK" };
const LINE_COLS = [
  { head: "In the check", tip: "Tick to put the line's parts in the check", cls: "tick",
    cell: l => `<label class="tickbox ${l.chosen_kind ? "on" : ""}"><input type="checkbox" data-tick ${l.chosen_kind ? "checked" : ""} ${l.on_board.length ? "" : "disabled"}
      title="${l.chosen_kind ? "In the check: untick to take its parts out" : "Tick to check its parts"}">${l.chosen_kind ? "IN" : "ADD"}</label>` },
  { head: "Value", cell: l => `${l.dnp ? `<span class="tag">DNP</span> ` : ""}<b>${esc(l.value)}</b>` },
  { head: "Description", cell: l => `${esc(l.description)}${l.why && !l.chosen_kind ? ` <span class="dim">(suggested: ${esc(l.why)})</span>` : ""}` },
  { head: "Parts", cls: "lparts", attr: l => `data-expand title="${esc(l.refs.join(", "))}"`,
    cell: (l, open) => `${open ? "▾" : "▸"} ${l.on_board.length}${l.on_board.length !== l.refs.length ? ` of ${l.refs.length}` : ""}` },
  { head: "Kind", cell: l => `<select data-kind>${["", ...Object.keys(KIND_NAMES)].map(k => `<option value="${k}" ${(l.chosen_kind || "") === k ? "selected" : ""}>${k ? KIND_NAMES[k] : "Not checked"}</option>`).join("")}</select>` },
  { head: "Hidden because", tip: "Parts that can't be seen (under a heatsink or shield)",
    cell: l => `<input data-hidden value="${esc(l.hidden || "")}" placeholder="${l.chosen_kind ? "visible" : "why not (optional)"}">` },
  { head: "On this board", cls: "lstat", attr: () => `data-expand title="Show each part"`, cell: (l, open) => `${open ? "▾" : "▸"} ${lineSummary(l)}` },
];
// a part of a BOM line: the line's own status (fresh with the state), the answers from the viewer's status
function lineStat(r) {
  const o = PS.parts[r.ref];
  return o && (["good", "bad", "ns", "covered"].includes(o.st) || (r.status === "off" && o.st !== "off")) ? ustat(o) : ustat({ st: r.status });
}
// a line's parts by side, counted by status, as chips
function lineSummary(l) {
  const st = l.status || [];
  const g = st.reduce((a, r) => a + r.good, 0), b = st.reduce((a, r) => a + r.bad, 0);
  const learned = g + b ? ` <span class="dim">· learned ${g} good, ${b} bad</span>` : "";
  if (!st.some(r => lineStat(r) !== "off"))
    return `${chip("off")} <span class="dim">${!l.on_board.length ? "not on the board" : l.dnp ? "DNP, not ticked" : "not ticked"}${l.hidden && !l.chosen_kind ? ": " + esc(l.hidden) : ""}</span>${learned}`;
  const sides = ["front", "back"].filter(s => st.some(r => r.side === s)).map(s => {
    const rs = st.filter(r => r.side === s);
    if (rs.every(r => r.status === "nophoto")) return `<span class="lab">${SIDE_SHORT[s]}</span> ${chip("in", ` ${rs.length}`)} <span class="dim">no photo yet</span>`;
    const n = {};
    for (const r of rs) { const k = lineStat(r); n[k] = (n[k] || 0) + 1; }
    return `<span class="lab">${SIDE_SHORT[s]}</span> ${Object.keys(STATUS).filter(k => n[k]).map(k => chip(k, ` ${n[k]}`)).join(" ")}`;
  });
  return `<span class="chips">${sides.join(" <span class='dim'>·</span> ")}</span>${learned}`;
}
// each part of a line, with a small picture of it on this board's photo of its side when there is one
// (lazy: only open lines, and the browser fetches only what scrolls into view; the Pi keeps the crops)
const thumbSrc = (side, ref) => `api/part/thumb?src=${encodeURIComponent(sideSrc(side))}&ref=${encodeURIComponent(ref)}&size=120&v=${S.sides[side].stamp || ""}`;
function lineRefs(l) {
  const st = l.status || [];
  if (!st.length) return `<div class="dim">No parts of this line are on the board.</div>`;
  const noPhoto = ["front", "back"].filter(s => st.some(r => r.side === s) && !(S.sides[s] && S.sides[s].aligned));
  return (noPhoto.length ? `<div class="dim">${noPhoto.map(s => `${SIDE_SHORT[s]}: ${S.sides[s] && S.sides[s].photo ? "photo not lined up yet" : "no photo yet"}`).join(" · ")}</div>` : "")
    + `<div class="refs">${st.map(r => {
    const can = r.side && S.sides[r.side] && S.sides[r.side].aligned;
    const k = lineStat(r);
    const info = `${chip(k)}<span class="why">${r.side ? SIDE_SHORT[r.side].toLowerCase() : ""}${r.why ? " · " + esc(r.why) : ""}${r.extra ? " · added one by one" : ""}${r.good + r.bad ? ` · ${r.good} good, ${r.bad} bad` : ""}</span>`;
    const add = k === "off" && r.side ? `<button class="small" data-addref="${esc(r.ref)}" title="Put just this part in the check (Look only); tick the line for all of them">+ ADD TO THE CHECK</button>` : "";
    return (can
      ? `<span class="ref thumb"><a href="#" class="ref thumb" data-look="${esc(r.ref)}" data-side="${r.side}" title="Select ${esc(r.ref)} on the ${r.side}">
          <img loading="lazy" decoding="async" alt="" src="${thumbSrc(r.side, r.ref)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'nopic',textContent:'no picture'}))">
          <b>${esc(r.ref)}</b></a>${info}${add}</span>`
      : `<span class="ref thumb"><span class="nopic">${r.side ? "no photo yet" : ""}</span><b>${esc(r.ref)}</b>${info}${add}</span>`);
  }).join("")}</div>`;
}

const XP = { ref: null, side: null };
async function openTrain(ref, side) {
  XP.ref = ref; XP.side = side;
  $("#xp-train").hidden = false;
  $("#xp-train-title").textContent = `TEACH ${ref} (${side.toUpperCase()})`;
  const x = (S.extras || []).find(e => e.ref === ref);
  $("#xp-wait").classList.toggle("on", !!(x && x.wait));
  $("#xp-wait").textContent = x && x.wait ? "WAITING FOR A NEW PHOTO ✓" : "WAIT FOR A NEW PHOTO";
  let srcs = [];
  try { srcs = (await api(`api/part/sources?board=${encodeURIComponent(S.name)}&side=${side}`)).sources; } catch (e) { say(e.message, true); }
  $("#xp-src").innerHTML = srcs.map(r => `<option value="${esc(r.id)}">${esc(r.label)} · ${esc(r.date.slice(0, 16))}</option>`).join("")
    || `<option value="">No lined-up ${side} photo yet</option>`;
  showTrain();
}
const showTrain = () => $("#xp-src").value ? showPart($("#xp-insp"), $("#xp-src").value, XP.ref, refreshState)
  : ($("#xp-insp").innerHTML = `<div class="dim">No photo of the ${XP.side} yet: press Wait for a new photo, or line up a photo of the ${XP.side} first.</div>`);
async function refreshState() { const st = await api("api/state"); S = st; S._r = Date.now(); render(); }
$("#xp-src").addEventListener("change", showTrain);
$("#xp-close").addEventListener("click", () => { $("#xp-train").hidden = true; XP.ref = null; });
$("#xp-wait").addEventListener("click", () => {
  const x = (S.extras || []).find(e => e.ref === XP.ref);
  act(() => post("api/part/extra", { ref: XP.ref, wait: !(x && x.wait) }), x && x.wait ? `${XP.ref} no longer waits for a photo.` : `${XP.ref} will be asked about on the next check of the ${XP.side}, until it's answered.`)
    .then(() => XP.ref && openTrain(XP.ref, XP.side));
});
$("#xp-add").addEventListener("click", () => {
  const ref = typedRef($("#xp-ref").value); if (!ref) return;
  const p = PL.parts.find(q => q.ref === ref);
  if (!p) return say(`${ref} isn't a part in this design.`, true);
  act(() => post("api/part/extra", { ref, kind: $("#xp-kind").value }), `${ref} added to the check. Train it now on a photo, or wait for a new one.`)
    .then(() => { $("#xp-ref").value = ""; PL.uid = null; loadPartList(S.project.uid); openTrain(ref, p.side === "top" ? "front" : "back"); });
});
$("#xp-list").addEventListener("click", e => {
  const t = e.target.closest("[data-xtrain]"); if (t) return openTrain(t.dataset.xtrain, t.dataset.side);
  const w = e.target.closest("[data-xwait]");
  if (w) { const x = S.extras.find(q => q.ref === w.dataset.xwait); return act(() => post("api/part/extra", { ref: x.ref, wait: !x.wait })); }
  const d = e.target.closest("[data-xdel]");
  if (d && confirm(`Take ${d.dataset.xdel} out of the check? What it has learned is kept.`))
    act(() => post("api/part/extra", { ref: d.dataset.xdel, remove: true }), `${d.dataset.xdel} taken out of the check.`).then(() => { if (XP.ref === d.dataset.xdel) $("#xp-train").hidden = true; });
});
$("#line-filter").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { ui.lineFilter = b.dataset.f; keepUi(); renderParts(); } });
$("#line-search").addEventListener("input", e => { ui.lineSearch = e.target.value; renderParts(); });
const REDRAWN = "; the Front and Back maps are redrawn";
$("#lines").addEventListener("change", e => {
  const tr = e.target.closest("tr"); if (!tr) return;
  const key = tr.dataset.key, ch = {}, l = S.lines.find(x => x.key === key);
  let done = "";
  if (e.target.matches("[data-tick]")) {
    ch.kind = e.target.checked ? (l.kind || "look") : "";
    done = e.target.checked ? `${l.value || l.description}: ${l.on_board.length} part(s) added to the check as ${KIND_NAMES[ch.kind]}${REDRAWN}. Change the kind on the line if needed.`
      : `${l.value || l.description} taken out of the check${REDRAWN}. What it has learned is kept.`;
  }
  if (e.target.matches("[data-kind]")) { ch.kind = e.target.value; done = (ch.kind ? `${l.value || l.description} checked as ${KIND_NAMES[ch.kind]}` : `${l.value || l.description} taken out of the check`) + REDRAWN + "."; }
  if (e.target.matches("[data-hidden]")) ch.hidden = e.target.value.trim();
  act(() => post("api/lines", { [key]: ch }), done || undefined);
});
$("#lines").addEventListener("click", e => {
  const ad = e.target.closest("[data-addref]");
  if (ad) return act(() => post("api/part/extra", { ref: ad.dataset.addref, kind: "look" }), `${ad.dataset.addref} put in the check (Look only).`).then(() => { PL.uid = null; });
  const a = e.target.closest("[data-look]");
  if (a) { e.preventDefault(); e.stopPropagation(); const side = a.dataset.side, ref = a.dataset.look; goTab(side); return lookAt(side, ref); }
  const x = e.target.closest("[data-expand]"); if (!x) return;
  const key = x.closest("tr").dataset.key;
  ui.openLines.has(key) ? ui.openLines.delete(key) : ui.openLines.add(key);
  renderParts();
});
$("#markings").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return;
  act(() => post("api/option", { key: "marking", value: { kind: b.closest("[data-mark]").dataset.mark, end: b.dataset.end } })); });
$("#rot-sign").addEventListener("click", e => { const b = e.target.closest("button"); if (b) act(() => post("api/option", { key: "rot_sign", value: b.dataset.v })); });
$("#use-tant").addEventListener("change", e => act(() => post("api/option", { key: "use_tantalums", value: e.target.checked })));

// ---- 3/4 a side: photo, fiducials, closeups ------------------------------------------------
async function loadAnchors(side) {
  try { ui.anchors[side] = await api(`api/side/${side}/anchors`); } catch (e) { say(e.message, true); }
  const st = S.sides[side];
  ui.pts[side] = { ...(st && st.points || {}) };
  ui.cur[side] = nextPrompt(side);
  if (st && st.aligned) loadRotation(side);
  renderSide(side);
  if (ui.look[side]) selectPart(side, ui.look[side]);
}
function prompts(side) {
  if (ui.closeup) {
    const c = S.sides[side].closeups.find(c => c.index === ui.closeup.index);
    const t = c || S.sides[side].tiles.find(t => t.index === ui.closeup.index);
    return (t ? t.anchors : []).map(a => ({ ref: a.ref, what: a.what, where: "" }));
  }
  return ui.anchors[side] ? ui.anchors[side].prompts : [];
}
const curPts = side => ui.closeup ? ui.closeup.pts : ui.pts[side];
function nextPrompt(side) {
  const p = prompts(side), pts = curPts(side);
  const i = p.findIndex(q => !pts[q.ref]);
  return i < 0 ? p.length : i;
}
// every part on the side (in the check or not), and the ones the list shows: by status and search
const sideRows = side => { const st = S.sides[side]; return st && st.aligned ? st.placed.concat(st.others || []) : []; };
function shownRows(side) {
  const q = $("#side-q").value.trim().toLowerCase(), pick = STAT_PICK[ui.sideStat] || STAT_PICK.all;
  return sideRows(side).filter(r => pick(pstat(r.ref)) && (!q || r.ref.toLowerCase().startsWith(q) || (r.value || "").toLowerCase().includes(q)))
    .sort((a, b) => natural(a.ref, b.ref));
}
const orientName = side => { const o = S.profile.orient[side], x = ORIENTS[side].find(x => x.v[0] === o[0] && x.v[1] === o[1]); return x ? x.t.toLowerCase() : ""; };

function renderSide(side) {
  const st = S.sides[side] || { photo: false };
  const cu = ui.closeup, cst = cu ? (st.closeups || []).find(c => c.index === cu.index) : null;
  $("#side-title").textContent = (side === "front" ? "FRONT" : "BACK") + (cu ? `  ·  CLOSEUP ${cu.index + 1}` : "");
  // setting up the photo: open until it's lined up, then one line with CHANGE
  const done = !!st.aligned && !cu, setup = $("#setup");
  if (!done) ui.setupOpen = false;
  setup.open = !done || ui.setupOpen;
  setup.classList.toggle("todo", !done);
  ui.pick = done && !ui.setupOpen;
  const nPts = Object.keys(st.points || {}).length;
  $("#setup-sum").textContent = cu ? "Line up the closeup" : done
    ? `Lined up on ${plural(nPts, prompts(side)[0] && prompts(side)[0].what === "corner" ? "corner" : "fiducial")} · ${orientName(side)} · ${S.design.rot_sign === -1 ? "clockwise" : "anticlockwise"}`
    : !st.photo ? "Set up this photo: take or choose one" : "Set up this photo: click the points, then LINE UP";
  $("#setup-change").hidden = !done;
  const o = S.profile.orient[side];
  $("#orient").innerHTML = ORIENTS[side].map(x => `<button data-o="${x.v.join(",")}" title="${esc(x.d)}" class="${x.v[0] === o[0] && x.v[1] === o[1] ? "on" : ""}">${x.t}</button>`).join("");
  // viewer
  const hasPhoto = cu ? !!cst : st.photo;
  const live = !!ui.stream;
  $("#video").hidden = !live || ui.stream.canon;
  $("#live").hidden = !live || !ui.stream.canon;
  const img = $("#photo"), ghost = $("#ghost");
  img.hidden = live || !hasPhoto;
  $("#placeholder").hidden = live || hasPhoto;
  const t = Date.now();
  if (hasPhoto && !live) {
    const aligned = cu ? cst.aligned : st.aligned;
    const base = cu ? `api/side/${side}/closeup/${cu.index}` : `api/side/${side}`;
    // the outlines are drawn here (by status); the server adds pin 1 dots and polarity marks
    const src = aligned ? (cu ? `${base}/map.jpg?w=1800` : `${base}/map.jpg?w=1800&outlines=0&marks=${ui.marks ? 1 : 0}`) : `${base}/photo.jpg`;
    const key = src + "|" + (S._v || "") + "|" + (cu ? S._r : `${st.stamp}|${(st.placed || []).length}`);
    if (img.dataset.key !== key) { img.dataset.key = key; img.src = src + (src.includes("?") ? "&" : "?") + "t=" + t; }
  }
  // the stage is the photo (or the camera's picture) at its own size
  if (live) {
    const v = $("#video"), l = $("#live");
    ZS.size(ui.stream.canon ? l.naturalWidth : v.videoWidth, ui.stream.canon ? l.naturalHeight : v.videoHeight);
  } else if (hasPhoto) ZS.size(cu ? cst.width : st.width, cu ? cst.height : st.height);
  // ghost over the live camera: the last photo of this side, or the design drawn as it should lie
  ghost.hidden = !(live && ui.ghostOn);
  if (live && ui.ghostOn) {
    const gsrc = cu ? "" : (st.photo ? `api/side/${side}/photo.jpg` : `api/side/${side}/ghost.svg?o=${o.join(",")}`);
    if (gsrc && ghost.dataset.src !== gsrc) { ghost.dataset.src = gsrc; ghost.src = gsrc + (gsrc.includes("?") ? "&" : "?") + "t=" + t; }
    ghost.hidden = !gsrc;
    ghost.style.opacity = ui.ghostOp / 100;
  }
  $("#viewer").classList.toggle("clicking", hasPhoto && !live && !ui.pick);
  $("#cam-start").textContent = live ? "STOP CAMERA" : "START CAMERA";
  $("#cam-take").hidden = !live;
  $("#cam-list").hidden = !live || $("#cam-list").options.length < 2;
  $("#ghost-on").checked = ui.ghostOn;
  $("#ghost-op").value = ui.ghostOp; $("#ghost-op").disabled = !ui.ghostOn;
  // alignment
  $("#align-box").hidden = !hasPhoto;
  const p = prompts(side), pts = curPts(side), cur = cu ? cu.cur : ui.cur[side];
  $("#align-help").textContent = cu ? "Click each point on the closeup, in order. Clicks snap to pads; for a part, click the middle of its body."
    : (p.length && p[0].what === "corner" ? "This side has fewer than 2 fiducials, so click the board's corners instead."
      : "Click each fiducial on the photo, in order. Clicks snap to the pad. At least 2 are needed; all of them is best.");
  $("#prompts").innerHTML = p.map((q, i) => `<div data-i="${i}" class="${i === cur ? "cur" : ""}"><span class="grow">${q.what === "centre of" ? "Centre of " : ""}<b>${esc(q.ref)}</b>${q.where ? ` <span class="dim">${esc(q.where)}</span>` : ""}</span>
    <span class="${pts[q.ref] ? "ok" : "dim"}">${pts[q.ref] ? "✓" : i === cur ? "click it" : "—"}</span></div>`).join("");
  $("#auto-find").hidden = !!cu;
  $("#do-align").disabled = Object.keys(pts).length < 2;
  let res = "";
  if (!cu && st.aligned) {
    res += `<div class="dim">Lined up${st.how ? ": " + esc(st.how) : ""}.${st.errors.length > 3 ? " Largest point error " + Math.max(...st.errors).toFixed(1) + " px." : ""}</div>`;
    for (const w of st.warnings) res += `<div class="warn">${esc(w)}</div>`;
  }
  if (!cu && S.project && (S.project.replaced_reference || S.project.reference_change))
    res += `<div class="warn">This board was the reference for checks. The reference as it was stays in use until you review the differences${st.aligned ? "" : " (line this photo up first)"}.
      ${st.aligned ? `<button class="small accent" data-rvgo>REVIEW THE REFERENCE CHANGE</button>` : ""}</div>`;
  if (cu && cst && cst.aligned) res += `<div class="dim">Closeup lined up.</div>`;
  if (cu) res += `<div class="row"><button id="back-full">BACK TO THE FULL PHOTO</button></div>`;
  $("#align-result").innerHTML = res;
  // rotation check
  const rc = ui.rot[side];
  $("#rot-box").hidden = cu || !st.aligned || !rc || !rc.needed;
  if (rc && rc.needed) {
    $("#rot-help").textContent = `Which outline sits on ${rc.ref}? This tells the app which way the design's rotations run. ${rc.votes["1"] || rc.votes["-1"] ? "(The design's pins already suggest one; check it.)" : ""}`;
    const cur = S.design.rot_sign;
    $("#rot-crops").innerHTML = [1, -1].map((sgn, i) => `<figure><img src="${rc.crops[i]}" alt=""><button data-sign="${sgn}" class="${cur === sgn ? "on" : ""}">${sgn === -1 ? "CLOCKWISE" : "ANTICLOCKWISE"}</button></figure>`).join("");
  }
  // closeups
  const tiles = cu ? [] : (st.tiles || []);
  $("#closeup-box").hidden = cu || !st.aligned || !tiles.length;
  $("#tiles").innerHTML = tiles.map(t => `<div><span class="grow"><b>Area ${t.index + 1}</b> <span class="dim">${esc(t.refs.join(", "))}</span></span>
    <span class="${t.aligned ? "ok" : "dim"}">${t.aligned ? "✓" : t.taken ? "line up" : ""}</span><button class="small" data-tile="${t.index}">${t.taken ? "OPEN" : "TAKE"}</button></div>`).join("");
  if (st.aligned && !cu && S.project) loadPartList(S.project.uid);
  // the parts: legend with counts, the list, the outlines on the photo, the selected part
  const showParts = !cu && !!st.aligned;
  $("#parts-box").hidden = !showParts;
  $("#side-legend").hidden = !showParts;
  if (showParts) {
    statusBar($("#side-legend"), sideRows(side).map(r => pstat(r.ref)), ui.sideStat, `parts on the ${side}`);
    $("#side-legend").insertAdjacentHTML("beforeend", `<span class="grow"></span><span class="hintkeys">Wheel: zoom · drag: move · click a part · ↑↓: next part</span>`);
    $("#marks-on").checked = ui.marks;
    const sel = ui.look[side];
    $("#side-parts").innerHTML = shownRows(side).map(r => {
      const k = pstat(r.ref);
      return `<div data-ref="${esc(r.ref)}" class="${r.ref === sel ? "sel" : ""}" title="${esc((PS.parts[r.ref] || {}).why || r.reason || "")}"><span class="r">${esc(r.ref)}</span>
        <span class="v grow ell">${esc(r.value || r.package || "")}${r.kind ? " · " + esc(KIND_NAMES[r.kind] || r.kind) : ""}</span>${newTag(r.ref)}${pchip(r.ref)}${k === "off" ? `<button class="small" data-add="${esc(r.ref)}" title="Put it in the check (Look only)">+ ADD</button>` : ""}</div>`;
    }).join("") || `<div class="dim">No parts match.</div>`;
  }
  renderSel(side);
  drawParts(side);
  drawMarks(side);
}

// a part put in the check on another board of the design since this one was last open (until answered here)
const NEW_VERDICT = { wrong: " · LOOKS WRONG", unsure: " · NOT SURE" };
function newTag(ref) {
  const o = PS.parts[ref]; if (!o || !o.new) return "";
  return `<span class="newtag" title="New since this board was last checked: answered on another board of the design${o.verdict_why ? ". The check here: " + esc(o.verdict_why) : ""}">NEW${NEW_VERDICT[o.verdict] || ""}</span>`;
}
// what happens on every board of the design, in plain words, for the selected part
const designName = () => (S.group && S.group.name) || (S.project && S.project.board) || "";
function carryLine(d, inCheck, ans) {
  const g = esc(designName()), isRef = S.project && S.project.reference;
  if (d && d.ns === "all") return `Not stuffed on every ${g} board: it isn't asked about on any of them.`;
  if (ans === "covered") return isRef ? `Covered on this reference, so its picture here isn't the standard: other ${g} boards still check it, against good answers from boards where it can be seen.`
    : `Covered on this board only: not a fault, not asked about here. It is still checked on every other ${g} board.`;
  if (!inCheck) return `Answer it (GOOD, BAD, MISSING, NS or COVERED) and it will be checked on every ${g} board from now on.`;
  let t = `This part is checked on every ${g} board.`;
  if (isRef && ans === "good") t += " This reference's picture of it is the standard.";
  if (isRef && (ans === "bad" || ans === "missing")) t += " The reference shows it wrong, so other boards are compared with good examples of it instead.";
  if (isRef && ans === "ns") t += " Not stuffed on this reference only.";
  return t;
}

// the outlines of every part on the side, coloured by status; in pick mode a click selects one
function drawParts(side) {
  const svg = $("#parts-svg"), st = S.sides[side];
  if (!st || !st.aligned || ui.closeup || ui.stream) { svg.innerHTML = ""; return; }
  svg.setAttribute("viewBox", `0 0 ${st.width} ${st.height}`);
  svg.classList.toggle("pick", ui.pick);
  const shown = new Set(shownRows(side).map(r => r.ref)), filtered = ui.sideStat !== "all" || $("#side-q").value.trim();
  const area = r => { const p = r.poly; let a = 0; for (let i = 0; i < p.length; i++) { const [x1, y1] = p[i], [x2, y2] = p[(i + 1) % p.length]; a += x1 * y2 - x2 * y1; } return Math.abs(a / 2); };
  const rows = sideRows(side).filter(r => r.poly).map(r => [r, area(r)]).sort((a, b) => b[1] - a[1]);   // small parts on top
  const sel = ui.look[side];
  let polys = "", labels = "";
  for (const [r] of rows) {
    const k = pstat(r.ref), dim = filtered && !shown.has(r.ref) && r.ref !== sel ? " dimmed" : "";
    polys += `<polygon data-ref="${esc(r.ref)}" class="st-${k}${dim}${r.ref === sel ? " sel" : ""}" style="stroke:${STATUS_COL[k]}" points="${r.poly.map(q => q.join(",")).join(" ")}"><title>${esc(r.ref)} · ${STATUS[k].t}${covNote(PS.parts[r.ref])}${(PS.parts[r.ref] || {}).why ? " · " + esc(PS.parts[r.ref].why) : ""}</title></polygon>`;
    if (k !== "off" || r.ref === sel) {
      const x = Math.min(...r.poly.map(q => q[0])), y = Math.min(...r.poly.map(q => q[1]));
      labels += `<text class="${dim}" x="${x}" y="${y}" dy="-0.35em" style="fill:${r.ref === sel ? "var(--st-sel)" : STATUS_COL[k]}">${esc(r.ref)}</text>`;
    }
  }
  svg.innerHTML = polys + labels;
}
// the selection, without drawing everything again: photo outline, list row, and both scrolled to it
function markSel(side, scroll = true) {
  const ref = ui.look[side];
  $$("#side-parts > div[data-ref]").forEach(d => d.classList.toggle("sel", d.dataset.ref === ref));
  drawParts(side);
  if (!ref || !scroll) return;
  const row = $$("#side-parts > div[data-ref]").find(d => d.dataset.ref === ref);
  if (row) row.scrollIntoView({ block: "nearest" });
  const r = sideRows(side).find(x => x.ref === ref);
  if (r && r.poly) ZS.show(r.poly.reduce((a, q) => a + q[0], 0) / r.poly.length, r.poly.reduce((a, q) => a + q[1], 0) / r.poly.length);
}
// the clicked points drawn over the photo, in photo pixels
function drawMarks(side) {
  const svg = $("#marks"), img = $("#photo");
  const st = S.sides[side];
  const cu = ui.closeup, cst = cu ? st.closeups.find(c => c.index === cu.index) : null;
  const W = cu ? cst && cst.width : st.width, H = cu ? cst && cst.height : st.height;
  if (!W || img.hidden || ui.pick) { svg.innerHTML = ""; return; }
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const r = W / 120, pts = curPts(side);
  svg.innerHTML = Object.entries(pts).map(([ref, [x, y]]) => `<circle class="pt" cx="${x}" cy="${y}" r="${r}" stroke-width="${W / 600}"/>
    <line x1="${x - r * 1.6}" y1="${y}" x2="${x + r * 1.6}" y2="${y}" stroke="currentColor" class="pt" stroke-width="${W / 900}"/>
    <line x1="${x}" y1="${y - r * 1.6}" x2="${x}" y2="${y + r * 1.6}" class="pt" stroke-width="${W / 900}"/>
    <text x="${x + r * 1.4}" y="${y - r * 1.2}" style="font-size:${W / 70}px">${esc(ref)}</text>`).join("");
}
$("#photo").addEventListener("load", () => (tab === "front" || tab === "back") && drawMarks(tab));
$("#video").addEventListener("loadedmetadata", () => (tab === "front" || tab === "back") && renderSide(tab));
$("#live").addEventListener("load", () => { if ((tab === "front" || tab === "back") && !ZS.w) renderSide(tab); });

const sideSrc = side => `project/${S.project.uid}/${side}`;
// ---- the selected part: what it is, what the check and the models make of it, and the answers
const SEL = { side: null, ref: null, d: null };
async function selectPart(side, ref, scroll = true) {
  ui.look[side] = ref || null; ui.highlight = ref || null; ui.badPick = false;
  SEL.side = side; SEL.ref = ref || null; SEL.d = null;
  if (tab === side) { renderSel(side); markSel(side, scroll); }
  if (!ref || !S.sides[side] || !S.sides[side].aligned) return;
  let d;
  try { d = await api(`api/part/info?src=${encodeURIComponent(sideSrc(side))}&ref=${encodeURIComponent(ref)}`); }
  catch (e) { d = { ref, error: e.message }; }
  if (SEL.ref === ref && SEL.side === side) { SEL.d = d; if (tab === side) renderSel(side); }
}
const lookAt = (side, ref) => selectPart(side, ref);
// what was answered on this board's photo: GOOD, BAD, MISSING, NS or COVERED
function selAnswer(d, k, o) {
  if (d && !d.error) return d.ns ? "ns" : d.covered ? "covered" : d.taught ? (d.taught.label === "good" ? "good" : d.taught.reason === "missing" ? "missing" : "bad") : null;
  if (o && o.st === "covered") return "covered";
  return ["good", "bad", "missing", "ns"].includes(k) ? k : null;
}
// what covers a part: one click picks one, or type your own
function coverPick(cur, attr) {
  const rs = Object.entries((PL.cover_reasons && Object.keys(PL.cover_reasons).length ? PL.cover_reasons : S && S.cover_reasons) || { heatsink: "heatsink" });
  return `<div class="answers"><span class="lab">COVERED BY</span>${rs.map(([r, t]) => `<button class="small a-covered ${cur === r ? "on" : ""}" ${attr}="${esc(r)}">${esc(t.toUpperCase())}</button>`).join("")}
    <input class="note cover-own" placeholder="or type what covers it" value="${esc(cur && !rs.some(([r]) => r === cur) ? cur : "")}"></div>`;
}
// the check thinks it looks covered, as on another board: one click says so
const coverHint = (sug, what, attr) => sug === "covered" ? `<button class="a-covered hint" ${attr}="${esc(what || "heatsink")}" title="It looks like a part answered COVERED on another board">LOOKS COVERED (${esc((what || "heatsink").toUpperCase())}?): CONFIRM</button>` : "";
function renderSel(side) {
  const el = $("#sel-part"), ref = ui.look[side], st = S.sides[side];
  el.hidden = !ref || !st || !st.aligned || !!ui.closeup;
  if (el.hidden) { el.innerHTML = ""; $("#side-col").style.setProperty("--selh", "0px"); return; }
  const row = sideRows(side).find(r => r.ref === ref) || {}, o = PS.parts[ref] || {}, k = pstat(ref);
  const d = SEL.d && SEL.d.ref === ref && SEL.side === side ? SEL.d : null, ok = d && !d.error;
  const src = sideSrc(side), v = st.stamp || "", ans = selAnswer(d, k, o), inCheck = k !== "off";
  const rows = shownRows(side), i = rows.findIndex(r => r.ref === ref);
  const kind = (ok && d.kind) || row.kind;
  const what = [ok ? d.value : row.value, kind ? KIND_NAMES[kind] || kind : "not in the check", ok ? d.package : row.package, ok && d.description].filter(Boolean).map(esc).join(" · ");
  const reasons = Object.entries(Object.keys(PL.reasons).length ? PL.reasons : (S.reasons || {})).filter(([r]) => r !== "missing");
  // on the reference, NS is for every board of the design unless said otherwise
  const isRef = !!(S.project && S.project.reference), nsMain = isRef ? "nsall" : "ns", nsOther = isRef ? "nsone" : "nsall";
  const nsOn = ok && (isRef ? d.ns === "all" : d.ns === "board");
  el.innerHTML = `<div class="head"><span class="ref">${esc(ref)}</span>${chip(k, covNote(o), "big solid")}<span class="grow"></span>
      <button class="small" data-a="prev" title="The part before in the list (key ↑)">‹</button><span class="dim" style="margin:0">${i >= 0 ? `${i + 1} of ${rows.length}` : ""}</span>
      <button class="small" data-a="next" title="The next part in the list (key ↓)">›</button><button class="small" data-a="close" title="Clear the selection (Esc)">✕</button></div>
    <div class="what">${what}${ok && !d.in_bom ? " · not in the BOM" : ""}</div>
    <div class="pics"><figure><img src="${partCrop(src, ref, "board", v)}" alt="">this board</figure>
      ${ok && d.reference && d.verdict !== "reference" ? `<figure><img src="${partCrop(src, ref, "reference", v)}" alt="">reference</figure>` : ""}
      <div class="learned dim">Learned so far:<br><b>${ok ? d.good : o.good || 0}</b> good<br><b>${ok ? d.bad : o.bad || 0}</b> bad${ok && d.pending ? "<br>waiting for a new photo" : ""}</div></div>
    <div class="opinions">
      <div class="op" title="${esc(ok ? d.why || "" : "")}"><b>THE CHECK</b> ${!d ? "Looking..." : d.error ? `<span class="st-bad">${esc(d.error)}</span>` : `<span class="verdict v-${d.verdict}">${VERDICT_TEXT[d.verdict] || d.verdict}</span> ${esc(d.why || o.why || "")}`}</div>
      ${ok && d.model_text ? `<div class="op" title="${esc(d.model_text)}"><b>THE MODELS</b> ${esc(d.model_text)}</div>` : ""}
      ${ok && !d.covered ? coverHint(d.suggest, d.cover, "data-cover") : ""}
    </div>
    <div class="answers">
      <button data-a="good" class="a-good ${ans === "good" ? "on" : ""}" title="It's right (key G)${inCheck ? "" : "; puts it in the check"}">GOOD</button>
      <button data-a="bad" class="a-bad ${ans === "bad" ? "on" : ""}" title="It's wrong: pick why (key B)">BAD${ans === "bad" && ok && d.taught && d.taught.reason ? ": " + esc((PL.reasons[d.taught.reason] || d.taught.reason).toUpperCase()) : ""}</button>
      <button data-a="missing" class="a-missing ${ans === "missing" ? "on" : ""}" title="It should be there but isn't: a fault (key M)">MISSING</button>
      <button data-a="${nsMain}" class="a-ns ${ans === "ns" ? "on" : ""}" title="${isRef ? "Not stuffed on every board of this design: this is the reference" : "Not stuffed: left off this board on purpose, not a fault"} (key N)">NS${isRef ? " ON EVERY BOARD" : ""}</button>
      <button data-a="covered" class="a-covered ${ans === "covered" ? "on" : ""}" title="Can't be seen on this board (a heatsink, a label...): not a fault, and still checked on every other board. Press again to undo (key C)">COVERED${ans === "covered" && ok ? ": " + esc((d.covered || "").toUpperCase()) : ""}</button>
      ${ans ? `<button data-a="undo" title="Take the answer back">UNDO</button>` : ""}
      <details class="more"><summary>MORE</summary><div class="menu">
        ${isRef ? `<button data-a="nsone" class="${ok && d.ns === "board" ? "on" : ""}">${ok && d.ns === "board" ? "NS ON THIS BOARD ONLY ✓ (UNDO)" : "NS ON THIS BOARD ONLY"}</button>`
          : `<button data-a="nsall" class="${ok && d.ns === "all" ? "on" : ""}">${ok && d.ns === "all" ? "NS ON EVERY BOARD ✓ (UNDO)" : "NS ON EVERY BOARD OF THIS DESIGN"}</button>`}
        ${ok && (d.extra || !d.in_check) ? `<button data-a="wait" class="${d.wait ? "on" : ""}">${d.wait ? "WAITING FOR A NEW PHOTO ✓" : "WAIT FOR A NEW PHOTO"}</button>` : ""}
        <button data-a="map">SHOW ON THE MAP</button></div></details>
    </div>
    ${ui.badPick ? `<div class="answers"><span class="lab">WHY BAD?</span>${reasons.map(([r, t]) => `<button class="small a-bad" data-reason="${esc(r)}">${esc(t.toUpperCase())}</button>`).join("")}
      <input class="note" id="sel-note" placeholder="Note (optional)" value="${esc(ok && d.note || "")}"></div>` : ""}
    ${ans === "covered" && ok ? coverPick(d.covered, "data-cover") : ""}
    <div class="carry">${o.new ? `<span class="newtag">NEW SINCE LAST CHECK</span> ` : ""}${carryLine(ok ? d : null, inCheck, ans)}</div>
    <div class="addrow">${inCheck
      ? `<span class="dim grow">${ok && d.extra ? "Added one by one" : "In the check"}${kind ? " as " + esc(KIND_NAMES[kind] || kind) : ""}</span><button class="small" data-a="remove">TAKE OUT</button>
        <button class="small accent" data-a="todo" title="The next part in the check with no answer (key Space)">NEXT NOT ANSWERED ›</button>`
      : `<span class="dim grow">Not in the check${o.why && o.why !== "not in the check" ? ": " + esc(o.why) : ""}</span><select id="sel-kind" title="Check it as">${kindOptions()}</select><button class="accent" data-a="add" title="Key A">+ ADD TO THE CHECK</button>`}</div>
    <div class="dim keys">Keys: G good · B bad · M missing · N NS · C covered · A add · ↑↓ step · Space next not answered · Esc</div>`;
  $("#side-col").style.setProperty("--selh", el.offsetHeight + 8 + "px");
}
// step through the list as shown: dir +1/-1, or the next part in the check not answered yet
function stepPart(side, dir, todo = false) {
  const rows = shownRows(side); if (!rows.length) return;
  let i = rows.findIndex(r => r.ref === ui.look[side]);
  if (i < 0) i = dir < 0 ? 0 : -1;
  for (let n = 0; n < rows.length; n++) {
    i = (i + (dir || 1) + rows.length) % rows.length;
    if (!todo || pstat(rows[i].ref) === "in") return selectPart(side, rows[i].ref);
  }
  if (todo) say(`Every part in the check on the ${side} that's in this list is answered.`);
}
async function selDo(side, a, extra = {}) {
  const ref = ui.look[side], d = SEL.d && SEL.d.ref === ref ? SEL.d : null, src = sideSrc(side), k = pstat(ref);
  if (!ref) return;
  if (a === "close") return selectPart(side, null);
  if (a === "prev" || a === "next") return stepPart(side, a === "prev" ? -1 : 1);
  if (a === "todo") return stepPart(side, 1, true);
  if (a === "map") { goTab("map"); return bvSelect(ref, null, true); }
  if (a === "bad" && !extra.reason) { ui.badPick = !ui.badPick; return renderSel(side); }
  const kind = $("#sel-kind") ? $("#sel-kind").value : "look";
  let done = "";
  try {
    if (a === "good" || a === "bad" || a === "missing") {
      const reason = a === "missing" ? "missing" : extra.reason || "", note = $("#sel-note") ? $("#sel-note").value : "";
      SEL.d = await post("api/part/teach", { src, ref, answer: a === "good" ? "good" : "bad", reason, note, kind: k === "off" ? kind : null });
      done = `${ref}: ${a === "good" ? "GOOD" : a === "missing" ? "MISSING" : "BAD"}, kept as a learned example. This part will now be checked on every ${designName()} board.`;
    } else if (a === "ns" || a === "nsall" || a === "nsone") {
      const every = a === "nsall", on = every ? !(d && d.ns === "all") : !(d && d.ns === "board");
      SEL.d = await post("api/part/ns", { src, ref, on, all: every });
      done = !on ? `${ref}: NS undone, it's checked again.` : every ? `${ref}: not stuffed on every ${designName()} board.`
        : `${ref}: not stuffed on this board. It will be checked on every other ${designName()} board.`;
    } else if (a === "covered") {      // pressed again: undone; extra.reason: covered by that
      const on = !!extra.reason || !(d && d.covered), reason = extra.reason || "heatsink";
      SEL.d = await post("api/part/covered", { src, ref, on, reason });
      done = !on ? `${ref}: COVERED undone, it's checked again.` : `${ref}: covered (${reason}) on this board. Not a fault; it is still checked on every other ${designName()} board.`;
    } else if (a === "undo") {
      if (d && d.ns) SEL.d = await post("api/part/ns", { src, ref, on: false });
      else if (d && d.covered) SEL.d = await post("api/part/covered", { src, ref, on: false });
      else if (d && d.taught) await post(`api/check/learned/${encodeURIComponent(d.board)}/${side}/delete`, { id: d.taught.id });
      done = `${ref}: answer taken back.`;
    } else if (a === "add") {
      await post("api/part/extra", { ref, kind });
      done = `${ref} put in the check as ${KIND_NAMES[kind]}.`;
    } else if (a === "wait") {
      await post("api/part/extra", { ref, kind: d && d.in_check ? null : kind, board: d && d.board, wait: !(d && d.wait) });
      done = d && d.wait ? `${ref} no longer waits for a photo.` : `${ref} will be asked about on the next check of the ${side}.`;
    } else if (a === "remove") {
      if (d && d.extra) await post("api/part/extra", { ref, remove: true });
      else {
        const l = S.lines.find(x => x.refs.includes(ref));
        if (!l) return say(`${ref} isn't on a BOM line; take it out on the Parts tab.`, true);
        if (!confirm(`${ref} is in the check through its BOM line ${l.value || l.description} (${plural(l.on_board.length, "part")}). Take the whole line out of the check? What it has learned is kept.`)) return;
        await post("api/lines", { [l.key]: { kind: "" } });
      }
      done = `${ref} taken out of the check. What it has learned is kept.`;
    }
  } catch (e) { return say(e.message, true); }
  ui.badPick = false; PL.uid = null;
  say(done);
  await refreshState();
  selectPart(side, ref, false);
}
$("#sel-part").addEventListener("change", e => {
  const i = e.target.closest(".cover-own"); if (i && i.value.trim()) selDo(tab, "covered", { reason: i.value.trim() });
});
$("#sel-part").addEventListener("click", e => {
  const r = e.target.closest("[data-reason]"); if (r) return selDo(tab, "bad", { reason: r.dataset.reason });
  const c = e.target.closest("[data-cover]"); if (c) return selDo(tab, "covered", { reason: c.dataset.cover });
  const b = e.target.closest("[data-a]"); if (b) selDo(tab, b.dataset.a);
});
document.addEventListener("keydown", e => {
  if ((tab !== "front" && tab !== "back") || e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, select, textarea")) return;
  const side = tab, k = e.key, on = !!ui.look[side];
  const keys = { ArrowDown: "next", ArrowRight: "next", ArrowUp: "prev", ArrowLeft: "prev", " ": "todo", Escape: "close", g: "good", b: "bad", m: "missing",
    n: S.project && S.project.reference ? "nsall" : "ns", c: "covered", a: "add" };
  if (k === "0") return ZS.fit();
  if (!keys[k] && !keys[k.toLowerCase()]) return;
  const a = keys[k] || keys[k.toLowerCase()];
  if (!on && !["next", "prev", "todo"].includes(a)) return;
  if (a === "add" && pstat(ui.look[side]) !== "off") return;
  e.preventDefault();
  if (!on) return stepPart(side, a === "prev" ? -1 : 1, a === "todo");
  selDo(side, a);
});
$("#parts-svg").addEventListener("click", e => {
  const p = e.target.closest("polygon[data-ref]"); if (!p || !ui.pick) return;
  e.stopPropagation();
  selectPart(tab, p.dataset.ref === ui.look[tab] ? null : p.dataset.ref, false);
  const row = $$("#side-parts > div[data-ref]").find(d => d.dataset.ref === p.dataset.ref);
  if (row) row.scrollIntoView({ block: "nearest" });
});
$("#photo").addEventListener("click", async e => {
  const side = tab; if (side !== "front" && side !== "back") return;
  if (!ui.closeup && S.sides[side].aligned && ui.pick) {
    const rect = $("#photo").getBoundingClientRect();
    try {
      const r = await post("api/part/pick", { src: sideSrc(side), fx: (e.clientX - rect.left) / rect.width, fy: (e.clientY - rect.top) / rect.height });
      if (r.ref) selectPart(side, r.ref); else say("No part there. Click on a part's body.", true);
    } catch (err) { say(err.message, true); }
    return;
  }
  const p = prompts(side); if (!p.length) return;
  const cu = ui.closeup;
  let cur = cu ? cu.cur : ui.cur[side];
  if (cur >= p.length) cur = p.length - 1;
  const st = S.sides[side], cst = cu ? st.closeups.find(c => c.index === cu.index) : null;
  const W = cu ? cst.width : st.width;
  const rect = $("#photo").getBoundingClientRect(), k = W / rect.width;
  const xy = [(e.clientX - rect.left) * k, (e.clientY - rect.top) * k];
  // record the click straight away (so quick clicks each get their own point), then snap it
  const ref = p[cur].ref, pts = curPts(side);
  pts[ref] = xy;
  const n = nextPrompt(side);
  if (cu) cu.cur = n; else ui.cur[side] = n;
  renderSide(side);
  try {
    const url = cu ? `api/side/${side}/closeup/${cu.index}/snap` : `api/side/${side}/snap`;
    const sn = await post(url, { xy });
    if (pts[ref] === xy) { pts[ref] = sn.xy; drawMarks(side); }
  } catch (err) { /* keep the click as it was */ }
});
$("#setup").addEventListener("toggle", () => {
  const side = tab, st = S.sides[side];
  if ((side !== "front" && side !== "back") || !st || !st.aligned || ui.closeup || $("#setup").open === ui.setupOpen) return;
  ui.setupOpen = $("#setup").open;
  if (ui.setupOpen) { ui.pts[side] = { ...(st.points || {}) }; ui.cur[side] = nextPrompt(side); }
  renderSide(side);
});
$("#prompts").addEventListener("click", e => {
  const d = e.target.closest("[data-i]"); if (!d) return;
  const side = tab, i = +d.dataset.i;
  delete curPts(side)[prompts(side)[i].ref];           // pick a row to redo it
  if (ui.closeup) ui.closeup.cur = i; else ui.cur[side] = i;
  renderSide(side);
});
$("#clear-pts").addEventListener("click", () => {
  const side = tab;
  if (ui.closeup) { ui.closeup.pts = {}; ui.closeup.cur = 0; } else { ui.pts[side] = {}; ui.cur[side] = 0; }
  renderSide(side);
});
$("#auto-find").addEventListener("click", async () => {
  const side = tab;
  try {
    const r = await post(`api/side/${side}/auto`, {});
    if (!r.points) return say("The fiducials couldn't be found automatically. Click each one.", true);
    ui.pts[side] = r.points; ui.cur[side] = nextPrompt(side);
    say("Found the fiducials. Check each mark sits on its pad, then press Line up.");
    renderSide(side);
  } catch (e) { say(e.message, true); }
});
$("#do-align").addEventListener("click", () => {
  const side = tab, cu = ui.closeup;
  if (cu) return act(() => post(`api/side/${side}/closeup/${cu.index}/align`, { points: cu.pts }), "Closeup lined up.");
  act(async () => { const s = await post(`api/side/${side}/align`, { points: ui.pts[side] }); S = s; ui.setupOpen = false; await loadRotation(side); return s; },
    "Lined up. Each part is outlined in its status colour (see the legend under the photo). Click any part to select it.");
});
async function loadRotation(side) {
  try { ui.rot[side] = await api(`api/side/${side}/rotation`); } catch (e) { ui.rot[side] = null; }
  if (tab === side) renderSide(side);
}
$("#rot-crops").addEventListener("click", e => { const b = e.target.closest("[data-sign]"); if (b) act(() => post("api/option", { key: "rot_sign", value: +b.dataset.sign }), "Rotation direction set."); });
$("#orient").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  const side = tab, v = b.dataset.o.split(",").map(Number);
  act(async () => { const s = await post(`api/side/${side}/orient`, { orient: v }); S = s; await loadAnchors(side); return S; });
});
$("#tiles").addEventListener("click", e => {
  const b = e.target.closest("[data-tile]"); if (!b) return;
  const side = tab, i = +b.dataset.tile, c = S.sides[side].closeups.find(c => c.index === i);
  ui.closeup = { side, index: i, pts: c ? { ...c.points } : {}, cur: 0 };
  ui.closeup.cur = nextPrompt(side);
  ZS.w = 0;
  say(c ? "" : "Frame just this area of the board, as close as the camera will focus, then take the photo or choose a file.");
  renderSide(side);
});
$("#align-result").addEventListener("click", e => { if (e.target.id === "back-full") { ui.closeup = null; ZS.w = 0; stopCamera(); renderSide(tab); } });
$("#side-legend").addEventListener("click", e => { const b = e.target.closest("[data-stat]"); if (b) { ui.sideStat = ui.sideStat === b.dataset.stat ? "all" : b.dataset.stat; renderSide(tab); } });
$("#marks-on").addEventListener("change", e => { ui.marks = e.target.checked; keepUi(); renderSide(tab); });
$("#side-parts").addEventListener("click", e => {
  const a = e.target.closest("[data-add]");
  if (a) { e.stopPropagation(); ui.look[tab] = a.dataset.add; return selDo(tab, "add"); }
  const r = e.target.closest("div[data-ref]"); if (r) selectPart(tab, r.dataset.ref === ui.look[tab] ? null : r.dataset.ref);
});
$("#side-q").addEventListener("input", () => renderSide(tab));
$("#side-q").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const rows = shownRows(tab), r = rows.length === 1 ? rows[0].ref : typedRef($("#side-q").value);
  if (r) { $("#side-q").value = ""; renderSide(tab); selectPart(tab, r); }
});
$("#ghost-op").addEventListener("input", e => { ui.ghostOp = +e.target.value; $("#ghost").style.opacity = ui.ghostOp / 100; keepUi(); });

// ---- the camera ------------------------------------------------------------------------------
// A webcam (in the browser), or a Canon EOS on USB driven by the server with gphoto2: its live view
// is shown as a stream of frames in an <img>, and the photo is taken at full resolution by the camera.
const CANON = "canon";
async function openCamera(want, video, liveImg, list) {
  let canon = { available: false };
  try { canon = await api("api/canon"); } catch (e) { /* older server */ }
  let cam;
  // the Canon unless a webcam was chosen
  if (canon.available && (want === CANON || !want || !navigator.mediaDevices)) {
    cam = canonStream(liveImg);
  } else {
    if (!navigator.mediaDevices) throw new Error(canon.available ? "choose the Canon from the list" : "the browser has no camera access here");
    const stream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: want && want !== CANON ? { ideal: want } : undefined, width: { ideal: 4096 }, height: { ideal: 3072 } }, audio: false });
    video.srcObject = stream;
    cam = { canon: false, stream, deviceId: stream.getVideoTracks()[0].getSettings().deviceId, stop: () => stream.getTracks().forEach(t => t.stop()) };
  }
  let devs = [];
  try { devs = navigator.mediaDevices ? (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput" && d.deviceId) : []; } catch (e) { }
  const opts = devs.map((d, i) => `<option value="${esc(d.deviceId)}" ${!cam.canon && d.deviceId === cam.deviceId ? "selected" : ""}>${esc(d.label || "Camera " + (i + 1))}</option>`);
  if (canon.available) opts.unshift(`<option value="${CANON}" ${cam.canon ? "selected" : ""}>${esc(canon.name || "Canon EOS")} (USB, full resolution)</option>`);
  list.innerHTML = opts.join("");
  video.hidden = cam.canon; liveImg.hidden = !cam.canon;
  return cam;
}
function canonStream(img) {
  const cam = { canon: true, deviceId: CANON, on: true, stop() { cam.on = false; img.removeAttribute("src"); } };
  const next = () => {
    if (!cam.on) return;
    img.onload = () => setTimeout(next, 150);       // about 5 frames a second keeps the Pi's CPU low
    img.onerror = async () => {
      if (!cam.on) return;
      try { await api("api/canon/preview.jpg"); } catch (e) { say(`Canon live view: ${e.message}`, true); }
      setTimeout(next, 2000);
    };
    img.src = `api/canon/preview.jpg?t=${Date.now()}`;
  };
  next();
  return cam;
}
async function grab(cam, video) {
  if (cam.canon) {
    say("Taking the photo on the Canon...");
    const r = await fetch("api/canon/capture", { method: "POST" });
    if (!r.ok) { let m = `Error ${r.status}`; try { m = (await r.json()).error || m; } catch (e) { } throw new Error(m); }
    return await r.blob();
  }
  const c = document.createElement("canvas");
  c.width = video.videoWidth; c.height = video.videoHeight;
  c.getContext("2d").drawImage(video, 0, 0);
  return await new Promise(res => c.toBlob(res, "image/jpeg", 0.95));
}
const camFailed = e => say(location.hostname === "localhost" || location.hostname === "127.0.0.1" || e.message.includes("Canon")
  ? `The camera couldn't start: ${e.message}` : "The webcam only works when this page is opened at http://localhost. A Canon on USB works from anywhere.", true);

async function startCamera() {
  try {
    ui.stream = await openCamera(S.profile.camera, $("#video"), $("#live"), $("#cam-list"));
    say("Line the board up with the ghost, then take the photo.");
  } catch (e) { ui.stream = null; camFailed(e); }
  renderSide(tab);
}
function stopCamera() { if (ui.stream) { ui.stream.stop(); ui.stream = null; } $("#live").hidden = true; }
$("#cam-start").addEventListener("click", () => { if (ui.stream) { stopCamera(); renderSide(tab); } else startCamera(); });
$("#cam-list").addEventListener("change", e => { stopCamera(); post("api/option", { key: "camera", value: e.target.value }).then(s => { S = s; startCamera(); }); });
$("#cam-take").addEventListener("click", async () => {
  try { const b = await grab(ui.stream, $("#video")); stopCamera(); sendPhoto(b); } catch (e) { say(e.message, true); }
});
$("#photo-file").addEventListener("change", e => { const f = e.target.files[0]; e.target.value = ""; if (f) sendPhoto(f); });
function sendPhoto(blob) {
  const side = tab, cu = ui.closeup;
  const url = cu ? `api/side/${side}/closeup/${cu.index}/photo` : `api/side/${side}/photo`;
  act(async () => {
    const s = await upload(url, blob); S = s; S._v = Date.now();
    if (cu) { cu.pts = {}; cu.cur = 0; } else { ui.pts[side] = {}; ui.cur[side] = 0; ui.rot[side] = null; ui.look[side] = null; }
    ZS.w = 0;
    return s;
  }, cu ? "Now click each point listed, on the closeup." : "Now click each fiducial, in the order listed.");
}
$("#ghost-on").addEventListener("change", e => { ui.ghostOn = e.target.checked; keepUi(); renderSide(tab); });

// ---- looking at one part on a photo, and teaching the check -------------------------------
// Used on the Front/Back tabs (the mapped photo), Check a board (the lined-up photo of the board
// checked) and the Parts tab (any photo of the board). src names the photo, see teach.py.
const PL = { uid: null, parts: [], reasons: {}, cover_reasons: {} };
async function loadPartList(uid) {
  if (!uid || PL.uid === uid) return;
  PL.uid = uid;
  try {
    const r = await api(`api/part/list?uid=${encodeURIComponent(uid)}`);
    PL.parts = r.parts; PL.reasons = r.reasons; PL.cover_reasons = r.cover_reasons || {};
    $("#dl-parts").innerHTML = r.parts.map(p => `<option value="${esc(p.ref)}">${esc([p.value, p.description, p.package].filter(Boolean).join(" · "))}${p.kind ? "" : p.in_bom ? " (not in the check)" : " (not in the BOM)"}</option>`).join("");
  } catch (e) { PL.uid = null; say(e.message, true); }
}
// what was typed: a ref, or a value that only one part has
function typedRef(text) {
  const t = text.trim().split(/\s+/)[0] || "";
  if (!t) return "";
  const hit = PL.parts.find(p => p.ref.toLowerCase() === t.toLowerCase());
  if (hit) return hit.ref;
  const byVal = PL.parts.filter(p => (p.value || "").toLowerCase() === text.trim().toLowerCase());
  return byVal.length ? byVal[0].ref : t;
}
const VERDICT_TEXT = { correct: "LOOKS RIGHT", wrong: "LOOKS WRONG", unsure: "NOT SURE", skipped: "NOT COMPARED", reference: "REFERENCE PHOTO", ns: "NOT STUFFED", covered: "CAN'T BE SEEN" };
const partCrop = (src, ref, which, v) => `api/part/crop?src=${encodeURIComponent(src)}&ref=${encodeURIComponent(ref)}&which=${which}&v=${v || ""}`;
function reasonSelect(cls, cur) {
  const rs = Object.keys(PL.reasons).length ? PL.reasons : (S && S.reasons) || {};
  return `<select class="${cls}"><option value="">Why bad?</option>${Object.entries(rs).map(([k, t]) => `<option value="${k}" ${cur === k ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
}
async function showPart(el, src, ref, after) {
  if (!ref) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="dim">Looking at ${esc(ref)}...</div>`;
  let d;
  try { d = await api(`api/part/info?src=${encodeURIComponent(src)}&ref=${encodeURIComponent(ref)}`); }
  catch (e) { el.innerHTML = `<div class="warn">${esc(e.message)}</div>`; return; }
  const v = Date.now(), ans = d.answer;
  const a = d.ns === "board" ? "ns" : d.covered ? "covered" : ans === "correct" ? "good" : ans === "wrong" ? (d.reason === "missing" ? "missing" : "bad") : selAnswer({ taught: d.taught }, null);
  el.innerHTML = `<div class="insp">
    <div><b style="font-size:17px">${esc(d.ref)}</b> ${d.in_check ? esc(KIND_NAMES[d.kind] || d.kind) : chip("off")} · ${esc(d.value || "")}
      ${d.description ? `<span class="dim">${esc(d.description)}</span>` : ""} <span class="dim">${esc(d.package || "")}${d.in_bom ? "" : " · not in the BOM"}</span></div>
    <div class="row"><span class="verdict v-${d.verdict}">${VERDICT_TEXT[d.verdict] || d.verdict}</span><span class="dim grow">${esc(d.why)}</span>${d.covered ? "" : coverHint(d.suggest, d.cover, "data-tcover")}</div>
    <div class="pics"><figure><img src="${partCrop(src, d.ref, "board", v)}" alt="">${esc(d.label)}</figure>
      ${d.reference && !d.verdict.startsWith("ref") ? `<figure><img src="${partCrop(src, d.ref, "reference", v)}" alt="">reference</figure>` : ""}</div>
    ${modelLine(d)}
    <div class="dim">Learned so far: ${d.good} good, ${d.bad} bad${d.pending ? " · waiting for a new photo to be taught on" : ""}${ans ? ` · this board answered ${ans === "correct" ? "GOOD" : "BAD"}` : ""}</div>
    <div class="carry">${d.ns === "all" ? "Not stuffed on every board of this design." : d.covered ? "Covered on this board only: not a fault. It is still checked on every other board of this design." : d.in_check ? "This part is checked on every board of this design." : "Answer it and it will be checked on every board of this design from now on."}</div>
    <div class="answers">
      <button data-t="good" class="a-good ${a === "good" ? "on" : ""}">GOOD</button><button data-t="bad" class="a-bad ${a === "bad" ? "on" : ""}">BAD</button>
      <button data-t="missing" class="a-missing ${a === "missing" ? "on" : ""}" title="It should be there but isn't: a fault, kept as a bad example">MISSING</button>
      <button data-t="ns" class="a-ns ${a === "ns" ? "on" : ""}" title="Not stuffed: left off this board on purpose, not a fault. Press again to undo">NS</button>
      <button data-t="covered" class="a-covered ${a === "covered" ? "on" : ""}" title="Can't be seen on this board (a heatsink, a label...): not a fault, still checked on every other board. Press again to undo">COVERED${d.covered ? ": " + esc(d.covered.toUpperCase()) : ""}</button>
      <details class="more"><summary>MORE</summary><div class="menu">
        <button data-t="nsall" class="${d.ns === "all" ? "on" : ""}" title="Not stuffed on every board of this design from now on. Press again to undo">${d.ns === "all" ? "NS ON EVERY BOARD ✓ (UNDO)" : "NS ON EVERY BOARD OF THIS DESIGN"}</button>
        ${d.in_check && !d.extra ? "" : `<button data-t="wait" class="${d.wait ? "on" : ""}">${d.wait ? "WAITING FOR A NEW PHOTO ✓" : "WAIT FOR A NEW PHOTO"}</button>`}</div></details></div>
    ${d.covered ? coverPick(d.covered, "data-tcover") : ""}
    <div class="row">${reasonSelect("t-reason", d.reason)}<input class="note t-note" value="${esc(d.note || "")}" placeholder="Note (kept with the example)"></div>
    ${d.in_check ? "" : `<div class="row"><select class="t-kind" title="What kind of part to check it as">${kindOptions()}</select><button data-t="add" class="accent">+ ADD TO THE CHECK</button></div>`}
    ${d.examples.length ? `<div class="dim">${d.examples.map(e => `<span class="${e.label === "good" ? "st-ok" : e.label === "covered" ? "st-cant" : "st-wrong"}">${e.label.toUpperCase()}</span>${e.reason ? " " + esc(PL.reasons[e.reason] || e.reason) : ""}${e.note ? " · " + esc(e.note) : ""} <span class="dim">${esc(e.serial || "")} ${esc(e.date)}</span>`).join("<br>")}</div>` : ""}
  </div>`;
  const cover = async (on, reason) => {
    try {
      await post("api/part/covered", { src, ref: d.ref, on, reason });
      say(on ? `${d.ref}: covered (${reason}) on this board. Not a fault; it is still checked on every other board.` : `${d.ref}: COVERED undone, it's checked again.`);
      PL.uid = null;
      if (after) await after(d.ref);
      showPart(el, src, d.ref, after);
    } catch (err) { say(err.message, true); }
  };
  el.onchange = e => { const i = e.target.closest(".cover-own"); if (i && i.value.trim()) cover(true, i.value.trim()); };
  el.onclick = async e => {
    const tc = e.target.closest("[data-tcover]"); if (tc) return cover(true, tc.dataset.tcover);
    const b = e.target.closest("[data-t]"); if (!b) return;
    if (b.dataset.t === "covered") return cover(!d.covered, "heatsink");
    const kind = $(".t-kind", el) ? $(".t-kind", el).value : null;
    try {
      if (b.dataset.t === "ns" || b.dataset.t === "nsall") {
        const every = b.dataset.t === "nsall", on = every ? d.ns !== "all" : d.ns !== "board";
        await post("api/part/ns", { src, ref: d.ref, on, all: every });
        say(!on ? `${d.ref}: NS undone, it's checked again.` : every ? `${d.ref}: not stuffed on every board of this design.` : `${d.ref}: not stuffed on this board.`);
      } else if (b.dataset.t === "add" || b.dataset.t === "wait") {
        await post("api/part/extra", { ref: d.ref, kind: d.in_check ? null : kind, board: d.board, ...(b.dataset.t === "wait" ? { wait: !d.wait } : {}) });
        say(b.dataset.t === "add" ? `${d.ref} added to the check.` : d.wait ? `${d.ref} no longer waits for a photo.` : `${d.ref} will be asked about on the next check of the ${d.side}.`);
      } else {
        const missing = b.dataset.t === "missing", note = $(".t-note", el).value;
        const reason = missing ? "missing" : $(".t-reason", el).value, answer = missing ? "bad" : b.dataset.t;
        if (answer === "bad" && !reason) { say("Pick why it's bad (reversed, missing...) first.", true); return; }
        await post("api/part/teach", { src, ref: d.ref, answer, reason, note, kind });
        say(`Learned: ${d.ref} kept as a ${missing ? "missing (bad)" : answer} example${d.in_check ? "" : ", and added to the check"}.`);
      }
      PL.uid = null;
      if (after) await after(d.ref);
      showPart(el, src, d.ref, after);
    } catch (err) { say(err.message, true); }
  };
}

// the trained models' extra opinion (models.py): shown, never used to pass or fail
const modelLine = r => r.model_text ? `<div class="dim" title="An extra opinion from the models trained on this machine (MODELS tab). The check's own answer above is what counts.">Models think: ${esc(r.model_text)}</div>` : "";

// ---- 5 map ---------------------------------------------------------------------------------
function renderMap() {
  const d = S.design, rows = [];
  let html = `<div class="kv">`;
  for (const side of ["front", "back"]) {
    const st = S.sides[side], n = d.parts.filter(p => p.side === SIDES[side]).length;
    html += `<b>${side === "front" ? "Front" : "Back"}</b><span>${n} in the check · ${st && st.aligned ? `${st.summary.checkable} checkable${st.summary.via_closeup ? ` (${st.summary.via_closeup} on closeups)` : ""}` : st && st.photo ? "photo not lined up yet" : "no photo yet"}</span>`;
    if (st && st.aligned) st.placed.filter(r => !r.checkable).forEach(r => rows.push({ side, ...r }));
  }
  html += `</div>`;
  $("#map-summary").innerHTML = html;
  const isRef = S.project && S.project.reference, rv = S.project && S.project.reference_change, swap = S.project && S.project.replaced_reference;
  $("#map-ref").textContent = rv ? `REVIEW THE REFERENCE CHANGE${rv.left ? ` (${rv.left} LEFT)` : ""}` : swap ? "REVIEW THE NEW REFERENCE PHOTO"
    : isRef ? "THE REFERENCE FOR CHECKS ✓" : "USE AS THE REFERENCE FOR CHECKS";
  $("#map-ref").classList.toggle("accent", !isRef); $("#map-ref").classList.toggle("on", !!isRef && !rv);
  $("#map-ref-note").textContent = rv ? `This board will become the reference once every difference from the one in use (${rv.old_label}) has a decision. Until then the old one is used.`
    : swap ? "This board's photo changed while it was the reference. The reference as it was stays in use until you review the differences."
    : isRef ? `Other ${S.project.board} boards are checked against this one. Press again to stop.`
    : "Once this board has been checked by hand, make it the reference for other boards of this type. If there is one already, you review the differences first.";
  $("#map-folder").textContent = `Everything for this board is kept with the project in the app's data folder under projects/${S.project ? S.project.uid : ""}/ (the ODB++ and BOM, design.json, parts_map.csv, and each side's reference.jpg, alignment.json, parts.json and map.jpg), and listed on the History tab.`;
  renderViewer();
  $("#unchecked tbody").innerHTML = rows.map(r => `<tr class="click" data-bvref="${esc(r.ref)}"><td>${r.side}</td><td><b>${esc(r.ref)}</b></td><td>${KIND_NAMES[r.kind]}</td><td>${esc(r.value)}</td><td>${chip("cant")} <span class="dim">${esc(r.reason)}</span></td></tr>`).join("")
    || `<tr><td colspan="5" class="no">${S.sides.front && S.sides.front.aligned || S.sides.back && S.sides.back.aligned ? "Every part in the check can be checked on the photos." : "Nothing yet. Line up a photo of each side first."}</td></tr>`;
}

$("#unchecked").addEventListener("click", e => { const r = e.target.closest("[data-bvref]"); if (r) bvSelect(r.dataset.bvref, null, true); });
document.addEventListener("click", e => { if (e.target.closest("[data-rvgo]")) $("#map-ref").click(); });
$("#map-ref").addEventListener("click", async () => {
  if (!S || !S.project) return;
  try {
    if (S.project.reference_change) return openReview(S.project.reference_change.id);
    if (S.project.replaced_reference) return reviewStarted(await post("api/refchange/start", { uid: S.project.uid }));
    const on = !S.project.reference;
    const r = await post(`api/history/${encodeURIComponent(S.project.uid)}/reference`, { on });
    if (r.review) return reviewStarted(r);
    S.project.reference = on; recentAt = 0;
    say(on ? "This board is now the reference. Check other boards of this type on the Check a board tab." : "No longer the reference.");
    render();
  } catch (e) { say(e.message, true); }
});

// ---- reviewing a change of reference (refchange.py) ------------------------------------------
// The reference is what right looks like, so it never changes silently: every part that differs from
// the reference in use gets a decision, and the old one is used until they all have one.
const RV = { id: null, c: null, filter: "todo", timer: null };
function reviewStarted(r) {
  if (!r.review) { refreshState(); return say("This board is now the reference for checks."); }
  say("This design has a reference already. Each part that differs between the two is listed for you to decide; the old reference is used until you finish.");
  openReview(r.review.id);
}
function openReview(id) { RV.id = id; RV.filter = "todo"; goTab("review"); }
async function loadReview() {
  clearTimeout(RV.timer);
  if (!RV.id) return;
  try { RV.c = await api(`api/refchange/${encodeURIComponent(RV.id)}`); } catch (e) { return say(e.message, true); }
  renderReview();
  if (RV.c.building && tab === "review") RV.timer = setTimeout(loadReview, 1500);
}
const rvCrop = (c, r, which) => `api/refchange/${encodeURIComponent(c.id)}/crop?key=${encodeURIComponent(r.key)}&which=${which}`;
function renderReview() {
  const c = RV.c; if (!c) return;
  const open = c.status === "open", dec = c.decisions || {};
  $("#rv-title").textContent = `REVIEW REFERENCE CHANGE  ·  ${c.board}: ${c.old_label} → ${c.new_label}`;
  $("#rv-left").textContent = c.building ? `COMPARING ${c.progress ? c.progress.done + " OF " + c.progress.total : "..."}`
    : open ? (c.left ? `${c.left} LEFT` : "ALL DECIDED") : c.status.toUpperCase();
  $("#rv-left").className = "badge " + (open && !c.left && !c.building ? "PASS" : open ? "OPEN" : "");
  $("#rv-sub").textContent = c.building ? "Comparing every part of the new reference with the one in use, and asking the models about both..."
    : c.error ? c.error
    : `${c.same_count} part(s) look the same and switch over without questions. ${plural(c.differences, "difference")} to decide.`
      + (open ? ` ${c.old_label} stays the reference until every one has a decision.` : c.summary ? " " + c.summary : "");
  $("#rv-finish").disabled = !open || !!c.left || c.building;
  $("#rv-later").hidden = !open; $("#rv-cancel").hidden = !open; $("#rv-undo").hidden = c.status !== "done"; $("#rv-approve").hidden = !open;
  const kinds = {};
  c.rows.forEach(r => r.kinds.forEach(k => kinds[k] = (kinds[k] || 0) + 1));
  const F = { todo: [`TO DECIDE (${c.left})`, r => !dec[r.key]], decided: [`DECIDED (${c.rows.length - c.left})`, r => !!dec[r.key]], all: [`ALL (${c.rows.length})`, () => true] };
  Object.entries(kinds).forEach(([k, n]) => F[k] = [`${c.kinds_text[k].toUpperCase()} (${n})`, r => r.kinds.includes(k)]);
  if (!F[RV.filter]) RV.filter = "all";
  $("#rv-filter").innerHTML = Object.entries(F).map(([k, [t]]) => `<button data-f="${k}" class="${RV.filter === k ? "on" : ""}">${esc(t)}</button>`).join("");
  const rows = c.rows.filter(F[RV.filter][1]);
  $("#rv-rows").innerHTML = rows.map(r => { const d = dec[r.key]; return `<div class="rvrow ${d ? "decided" : ""}">
      <figure><img loading="lazy" src="${rvCrop(c, r, "old")}" alt="">OLD · ${esc(c.old_label)}</figure>
      <figure><img loading="lazy" src="${rvCrop(c, r, "new")}" alt="">NEW · ${esc(c.new_label)}</figure>
      <div><div class="row tight"><b style="font-size:17px">${esc(r.ref)}</b><span class="dim ell" style="margin:0">${r.side} · ${esc(r.kind ? KIND_NAMES[r.kind] || r.kind : "not in the check")} ${esc(r.value)}</span></div>
        <div class="chips">${r.kinds.map(k => `<span class="chip st-in">${esc(c.kinds_text[k].toUpperCase())}</span>`).join("")}</div>
        <div class="dim">${esc(r.why)}</div>
        ${r.model_old || r.model_new ? `<div class="dim">Models think: old ${esc(r.model_old || "–")} · new ${esc(r.model_new || "–")}</div>` : ""}
        <div class="answers">${Object.entries(c.choices).map(([k, t]) => `<button data-rv="${k}" data-key="${esc(r.key)}" class="${k === "accept" ? "a-good" : k === "wrong" ? "a-bad" : k === "ns" ? "a-ns" : ""} ${d && d.choice === k ? "on" : ""}" ${open ? "" : "disabled"}>${esc(t)}${d && d.choice === k && d.default ? " (SUGGESTED)" : ""}</button>`).join("")}</div></div></div>`; }).join("")
    || `<p class="dim" style="margin:12px">${c.building ? "Comparing..." : c.rows.length ? "Nothing in this list." : "No differences: finish to switch over."}</p>`;
}
$("#rv-filter").addEventListener("click", e => { const b = e.target.closest("[data-f]"); if (b) { RV.filter = b.dataset.f; renderReview(); } });
$("#rv-rows").addEventListener("click", async e => {
  const b = e.target.closest("[data-rv]"); if (!b || b.disabled) return;
  const d = (RV.c.decisions || {})[b.dataset.key];
  try { RV.c = await post(`api/refchange/${encodeURIComponent(RV.id)}/decide`, { keys: [b.dataset.key], choice: d && d.choice === b.dataset.rv ? null : b.dataset.rv }); renderReview(); }
  catch (err) { say(err.message, true); }
});
$("#rv-approve").addEventListener("click", async () => {
  try { const n = RV.c.left; RV.c = await post(`api/refchange/${encodeURIComponent(RV.id)}/approve`, {}); renderReview(); say(`${n - RV.c.left} part(s) that match accepted.`); }
  catch (e) { say(e.message, true); }
});
async function rvAfter(r, text) {
  RV.c = r; S = await api("api/state"); S._r = Date.now(); recentAt = 0; renderReview(); say(text);
}
$("#rv-finish").addEventListener("click", async () => {
  try { const r = await post(`api/refchange/${encodeURIComponent(RV.id)}/finish`, {}); await rvAfter(r, `${r.new_label} is now the reference for checks. ${r.summary || ""}`); }
  catch (e) { say(e.message, true); }
});
$("#rv-later").addEventListener("click", () => { say("Saved. The old reference stays in use until you finish; open this again from the Map tab or History."); goTab(S && S.design ? "map" : "history"); });
$("#rv-cancel").addEventListener("click", async () => {
  if (!confirm("Cancel this change? The old reference stays in use and these decisions are kept in the history only.")) return;
  try { await rvAfter(await post(`api/refchange/${encodeURIComponent(RV.id)}/cancel`, {}), "Cancelled. The old reference stays."); } catch (e) { say(e.message, true); }
});
$("#rv-undo").addEventListener("click", async () => {
  if (!confirm("Put the old reference back? What these decisions added (examples, NS, parts marked wrong) is taken away again.")) return;
  try { await rvAfter(await post(`api/refchange/${encodeURIComponent(RV.id)}/undo`, {}), "Undone: the old reference is back."); } catch (e) { say(e.message, true); }
});

// ---- 5 map: the board viewer ----------------------------------------------------------------
// The design's parts, pins and nets drawn on a canvas, on the lined-up photo of the side when there
// is one. Everything is drawn in view coordinates: board units with the side's orientation applied
// (x' = sx x, y' = sy y, see viewer.py), the photo already warped into that frame by the server.
const BV = {
  uid: null, g: null, pins: null, st: {}, side: "front", photoOn: true, photoOp: 85, rats: false, stat: "all",
  sel: { ref: null, net: null, list: null }, view: null, img: {}, parts: {}, netPins: null, frame: 0, loading: false,
};
try { Object.assign(BV, JSON.parse(localStorage.getItem("aoimap-bv") || "{}"), { sel: { ref: null, net: null, list: null } }); } catch (e) { }
const keepBv = () => { try { localStorage.setItem("aoimap-bv", JSON.stringify({ side: BV.side, photoOn: BV.photoOn, photoOp: BV.photoOp, rats: BV.rats })); } catch (e) { } };
const natural = (a, b) => a.localeCompare(b, undefined, { numeric: true });
const sideOf = p => p.s === "top" ? "front" : "back";

async function bvOpen() {
  if (!S || !S.project) return;
  const uid = S.project.uid;
  if (BV.loading) return;
  BV.loading = true;
  try {
    const g = await api("api/viewer/geometry");
    const fresh = BV.uid !== uid;
    BV.g = g; BV.uid = uid; BV.parts = {};
    for (const p of g.parts) {
      const t = p.a * Math.PI / 180, c = Math.cos(t), s = Math.sin(t), m = p.m ? -1 : 1, [x0, y0, x1, y1] = p.b;
      p.c = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(([u, v]) => [p.x + c * m * u - s * v, p.y + s * m * u + c * v]);
      p.cos = c; p.sin = s;
      BV.parts[p.r] = p;
    }
    if (fresh) { BV.sel = { ref: null, net: null, list: null }; BV.view = null; BV.pins = null; BV.netPins = null; BV.img = {}; }
    $("#dl-bv").innerHTML = g.parts.filter(p => !p.f).map(p => `<option value="${esc(p.r)}">${esc(p.v || p.k)}</option>`).join("")
      + g.nets.map(n => `<option value="${esc(n)}">net</option>`).join("");
    $("#bv-notes").textContent = g.notes.join(" ");
    await bvStatus();
    bvPhoto();
    bvDraw();
    if (!BV.pins) {
      const r = await api("api/viewer/pins");
      BV.pins = r.pins; BV.netPins = {};
      for (const [ref, rows] of Object.entries(BV.pins)) rows.forEach((q, i) => { if (q[3] >= 0) (BV.netPins[q[3]] = BV.netPins[q[3]] || []).push([ref, i]); });
      bvInfo(); bvDraw();
    }
  } catch (e) { say(e.message, true); }
  finally { BV.loading = false; }
}
async function bvStatus() {
  BV.st = await loadStatus();
  bvInfo(); bvDraw(); mapLegend();
}
// the legend with counts for the side shown; a count shows only those parts brightly
function mapLegend() {
  if (!BV.g) return;
  const layer = SIDES[BV.side];
  statusBar($("#map-legend"), BV.g.parts.filter(p => p.s === layer && !p.f).map(p => ustat(BV.st[p.r])), BV.stat, `parts on the ${BV.side}`);
  $("#map-legend").insertAdjacentHTML("beforeend", `<span class="grow"></span><span class="hintkeys">Wheel: zoom · drag: move · click a part or pin · F: flip · 0: fit · Esc: clear</span>`);
}
$("#map-legend").addEventListener("click", e => { const b = e.target.closest("[data-stat]"); if (b) { BV.stat = BV.stat === b.dataset.stat ? "all" : b.dataset.stat; mapLegend(); bvDraw(); } });
// the side's photo, warped into the viewer's frame by the server
function bvPhoto() {
  const sd = BV.g && BV.g.sides[BV.side];
  if (!sd || !sd.photo || BV.img[BV.side] && BV.img[BV.side].stamp === sd.stamp) return;
  const im = new Image();
  im.onload = () => bvDraw();
  im.src = `api/viewer/photo/${BV.side}.jpg?v=${encodeURIComponent(BV.uid + "-" + sd.stamp)}`;
  BV.img[BV.side] = { stamp: sd.stamp, im };
}
function bvCanvas() {
  const cv = $("#bv-canvas"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  return { cv, w, h, dpr };
}
const bvO = () => BV.g.sides[BV.side].orient;
const toW = (x, y) => { const [sx, sy] = bvO(); return [sx * x, sy * y]; };           // board -> view
function bvFit() {
  const { w, h } = bvCanvas(); if (!w || !BV.g) return;
  const [x0, y0, x1, y1] = BV.g.sides[BV.side].rect;
  const s = Math.min(w / (x1 - x0), h / (y1 - y0));
  BV.view = { s, tx: w / 2 - s * (x0 + x1) / 2, ty: h / 2 - s * (y0 + y1) / 2 };
}
const toS = (x, y) => { const [wx, wy] = toW(x, y), v = BV.view; return [wx * v.s + v.tx, wy * v.s + v.ty]; };   // board -> screen
const fromS = (px, py) => { const v = BV.view, [sx, sy] = bvO(); return [sx * (px - v.tx) / v.s, sy * (py - v.ty) / v.s]; };
function bvDraw() {
  if (tab !== "map" || !BV.g || BV.frame) return;
  BV.frame = requestAnimationFrame(() => { BV.frame = 0; bvPaint(); });
}
function bvPaint() {
  const { cv, w, h, dpr } = bvCanvas(); if (!w) return;
  if (!BV.view) bvFit();
  const ctx = cv.getContext("2d"), g = BV.g, layer = SIDES[BV.side], v = BV.view, sd = g.sides[BV.side];
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#141518"; ctx.fillRect(0, 0, w, h);
  const pic = BV.photoOn && sd.photo && BV.img[BV.side] && BV.img[BV.side].im.complete && BV.img[BV.side].im.naturalWidth ? BV.img[BV.side].im : null;
  const path = pts => { ctx.beginPath(); pts.forEach(([x, y], i) => { const [a, b] = toS(x, y); i ? ctx.lineTo(a, b) : ctx.moveTo(a, b); }); ctx.closePath(); };
  // the board: its outline, plain, with the photo over it
  path(g.outline); ctx.fillStyle = "#1B2620"; ctx.fill();
  if (pic) {
    const [x0, y0, x1, y1] = sd.rect;
    ctx.globalAlpha = BV.photoOp / 100;
    ctx.imageSmoothingEnabled = v.s * (x1 - x0) < pic.naturalWidth * 2;
    ctx.drawImage(pic, x0 * v.s + v.tx, y0 * v.s + v.ty, (x1 - x0) * v.s, (y1 - y0) * v.s);
    ctx.globalAlpha = 1;
  }
  path(g.outline); ctx.strokeStyle = pic ? "rgba(224,163,58,.35)" : "#4F6B58"; ctx.lineWidth = 1.2; ctx.stroke();
  const sel = BV.sel, selPart = sel.ref && BV.parts[sel.ref];
  const netSet = sel.net != null && BV.netPins ? new Set((BV.netPins[sel.net] || []).map(([r]) => r)) : null;
  // the other side's parts, faint (plain view only)
  if (!pic) {
    ctx.strokeStyle = "rgba(140,150,165,.13)"; ctx.lineWidth = 1;
    for (const p of g.parts) if (p.s !== layer) { path(p.c); ctx.stroke(); }
  }
  // pins: shown once they are big enough to see, and always those of the chosen part or net
  const pinPx = BV.pins ? v.s : 0;
  const drawPin = (p, q, mode) => {
    const [px, py] = toS(q[1], q[2]);
    let pw = q[4] * v.s, ph = q[5] * v.s;
    if (!pw || !ph) { pw = ph = Math.max(3, 0.02 * v.s); }
    if (px < -pw - 40 || py < -ph - 40 || px > w + pw + 40 || py > h + ph + 40) return;
    const [sx, sy] = bvO(), ang = Math.atan2(sy * p.sin, sx * p.cos);
    ctx.save(); ctx.translate(px, py); ctx.rotate(ang);
    ctx.beginPath();
    if (q[6]) ctx.ellipse(0, 0, Math.max(pw, 1.5) / 2, Math.max(ph, 1.5) / 2, 0, 0, 2 * Math.PI); else ctx.rect(-pw / 2, -ph / 2, Math.max(pw, 1.5), Math.max(ph, 1.5));
    if (mode === "net") { ctx.fillStyle = "#FFD45E"; ctx.fill(); ctx.strokeStyle = "#141518"; ctx.lineWidth = 1; ctx.stroke(); }
    else if (mode === "part") { ctx.fillStyle = pic ? "rgba(224,163,58,.55)" : "#B08A45"; ctx.fill(); }
    else if (mode === "far") { ctx.strokeStyle = "#FFD45E"; ctx.lineWidth = 1.5; ctx.stroke(); }
    else if (pic) { ctx.strokeStyle = "rgba(220,220,210,.45)"; ctx.lineWidth = 1; ctx.stroke(); }
    else { ctx.fillStyle = q[3] >= 0 ? "#7D7C70" : "#5A5A55"; ctx.fill(); }
    ctx.restore();
  };
  if (BV.pins) {
    const showAll = 0.012 * pinPx > 2;
    for (const p of g.parts) {
      if (p.s !== layer) continue;
      const rows = BV.pins[p.r] || [];
      const isSel = p === selPart;
      if (!showAll && !isSel && !(netSet && netSet.has(p.r))) continue;
      rows.forEach(q => {
        const mode = sel.net != null && q[3] === sel.net ? "net" : isSel ? "part" : "";
        if (showAll || mode) drawPin(p, q, mode);
      });
    }
    // net pins on the other side, as rings
    if (netSet) for (const [r, i] of BV.netPins[sel.net] || []) { const p = BV.parts[r]; if (p && p.s !== layer) drawPin(p, BV.pins[r][i], "far"); }
  }
  // the parts on this side, coloured by their check status
  ctx.lineJoin = "round";
  const lab = [];
  const pickSt = STAT_PICK[BV.stat] || STAT_PICK.all;
  for (const p of g.parts) {
    if (p.s !== layer) continue;
    const code = p.f ? "fid" : ustat(BV.st[p.r]);
    const col = p.f ? "#8A909C" : STATUS_COL[code] || STATUS_COL.off;
    const on = p === selPart, inNet = netSet && netSet.has(p.r), dim = !on && !inNet && !p.f && BV.stat !== "all" && !pickSt(code);
    path(p.c);
    if (on) { ctx.fillStyle = "rgba(255,224,102,.2)"; ctx.fill(); }
    ctx.setLineDash(code === "ns" ? [5, 4] : []);
    ctx.strokeStyle = on ? "#FFE066" : inNet ? "#FFE066" : col;
    ctx.globalAlpha = dim ? .12 : code === "off" && !on && !inNet ? (pic ? .55 : .7) : 1;
    ctx.lineWidth = on ? 3 : inNet ? 2 : code === "off" || code === "fid" ? 1 : 1.8;
    ctx.stroke();
    ctx.globalAlpha = 1;
    const [a, b] = toS(p.x, p.y), size = Math.min(Math.abs(p.b[2] - p.b[0]), Math.abs(p.b[3] - p.b[1])) * v.s;
    if (on || inNet || (size > 22 && !dim)) lab.push([p, a, b, size, on || inNet, col]);
  }
  ctx.setLineDash([]);
  // net lines: the shortest tree joining the chosen net's pins
  if (BV.rats && netSet) {
    const pts = (BV.netPins[sel.net] || []).map(([r, i]) => toS(BV.pins[r][i][1], BV.pins[r][i][2]));
    ctx.strokeStyle = "rgba(255,212,94,.75)"; ctx.lineWidth = 1; ctx.beginPath();
    for (const [a, b] of bvTree(pts)) { ctx.moveTo(...pts[a]); ctx.lineTo(...pts[b]); }
    ctx.stroke();
  }
  // labels: refs on parts big enough, pin names (and nets) when zoomed in
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (const [p, a, b, size, hi, col] of lab) {
    const fs = Math.max(10, Math.min(16, size / 4));
    ctx.font = `${hi ? 700 : 600} ${fs}px ${getComputedStyle(document.body).fontFamily}`;
    ctx.lineWidth = 3; ctx.strokeStyle = "rgba(15,15,18,.85)"; ctx.strokeText(p.r, a, b);
    ctx.fillStyle = hi ? "#FFD45E" : col; ctx.fillText(p.r, a, b);
  }
  if (BV.pins) {
    for (const p of g.parts) {
      if (p.s !== layer) continue;
      const isSel = p === selPart;
      for (const q of BV.pins[p.r] || []) {
        const pw = Math.min(q[4] || 0.02, q[5] || 0.02) * v.s, big = Math.max(q[4], q[5]) * v.s;
        const mine = isSel || (sel.net != null && q[3] === sel.net);
        if (pw < (mine ? 9 : 14)) continue;
        const [a, b] = toS(q[1], q[2]);
        if (a < -50 || b < -50 || a > w + 50 || b > h + 50) continue;
        const fs = Math.max(9, Math.min(14, pw * 0.6));
        ctx.font = `600 ${fs}px ${getComputedStyle(document.body).fontFamily}`;
        ctx.lineWidth = 3; ctx.strokeStyle = "rgba(15,15,18,.85)"; ctx.fillStyle = "#F2F2EA";
        const net = big > 60 && q[3] >= 0 ? g.nets[q[3]] : "";
        ctx.strokeText(q[0], a, net ? b - fs * 0.55 : b); ctx.fillText(q[0], a, net ? b - fs * 0.55 : b);
        if (net) { ctx.font = `${Math.max(8, fs - 2)}px ${getComputedStyle(document.body).fontFamily}`; ctx.fillStyle = "#FFD45E"; ctx.strokeText(net, a, b + fs * 0.6); ctx.fillText(net, a, b + fs * 0.6); }
      }
    }
  }
  $("#bv-side").textContent = BV.side === "front" ? "FRONT" : "BACK";
}
// Prim's shortest tree over screen points: [[i, j]]
function bvTree(pts) {
  const n = pts.length, out = [];
  if (n < 2) return out;
  const best = new Float64Array(n).fill(Infinity), from = new Int32Array(n), done = new Uint8Array(n);
  let cur = 0; done[0] = 1;
  for (let k = 1; k < n; k++) {
    let nb = -1, nd = Infinity;
    for (let j = 0; j < n; j++) {
      if (done[j]) continue;
      const d = (pts[j][0] - pts[cur][0]) ** 2 + (pts[j][1] - pts[cur][1]) ** 2;
      if (d < best[j]) { best[j] = d; from[j] = cur; }
      if (best[j] < nd) { nd = best[j]; nb = j; }
    }
    done[nb] = 1; out.push([from[nb], nb]); cur = nb;
  }
  return out;
}
// what is under a point on the screen: a pin (when pins are drawn big enough) or the smallest part
function bvHit(px, py) {
  const [x, y] = fromS(px, py), layer = SIDES[BV.side], v = BV.view;
  let pin = null, pd = Infinity;
  if (BV.pins) for (const p of BV.g.parts) {
    if (p.s !== layer) continue;
    (BV.pins[p.r] || []).forEach((q, i) => {
      const r = Math.max(q[4] || 0, q[5] || 0, 0.012) / 2, d = Math.hypot(q[1] - x, q[2] - y);
      if (d <= Math.max(r, 4 / v.s) && d < pd && r * v.s > 2.5) { pd = d; pin = { ref: p.r, i }; }
    });
  }
  if (pin) return pin;
  let hit = null, area = Infinity;
  for (const p of BV.g.parts) {
    if (p.s !== layer) continue;
    let inside = true, sgn = 0;
    for (let i = 0; i < 4 && inside; i++) {
      const [ax, ay] = p.c[i], [bx, by] = p.c[(i + 1) % 4], cr = (bx - ax) * (y - ay) - (by - ay) * (x - ax);
      if (Math.abs(cr) < 1e-12) continue;
      const s = Math.sign(cr); if (sgn && s !== sgn) inside = false; sgn = s;
    }
    const a = Math.abs((p.b[2] - p.b[0]) * (p.b[3] - p.b[1]));
    if (inside && a < area) { area = a; hit = { ref: p.r }; }
  }
  return hit;
}
function bvSelect(ref, net = null, centre = false) {
  BV.sel = { ref, net, list: null };
  const p = ref && BV.parts[ref];
  if (p && sideOf(p) !== BV.side && net == null) { BV.side = sideOf(p); keepBv(); bvPhoto(); BV.view = null; mapLegend(); }
  if (centre && p) {
    if (!BV.view) bvFit();
    const { w, h } = bvCanvas(), [wx, wy] = toW(p.x, p.y);
    const size = Math.max(Math.abs(p.b[2] - p.b[0]), Math.abs(p.b[3] - p.b[1]), 0.05);
    BV.view.s = Math.max(BV.view.s, Math.min(w, h) / (size * 8));
    BV.view.tx = w / 2 - wx * BV.view.s; BV.view.ty = h / 2 - wy * BV.view.s;
  }
  bvInfo(); bvDraw();
}
function bvFlip() {
  if (!BV.g) return;
  const { w, h } = bvCanvas();
  const c = BV.view ? fromS(w / 2, h / 2) : null, s = BV.view && BV.view.s;
  BV.side = BV.side === "front" ? "back" : "front"; keepBv(); bvPhoto(); mapLegend();
  if (c) { const [wx, wy] = toW(...c); BV.view = { s, tx: w / 2 - wx * s, ty: h / 2 - wy * s }; } else BV.view = null;
  bvDraw();
}
function bvFind(text) {
  const t = text.trim(); if (!t || !BV.g) return;
  const lo = t.toLowerCase();
  const p = BV.g.parts.find(q => q.r.toLowerCase() === lo);
  if (p) return bvSelect(p.r, null, true);
  const n = BV.g.nets.findIndex(q => q.toLowerCase() === lo);
  if (n >= 0) return bvSelect(null, n);
  const byVal = BV.g.parts.filter(q => !q.f && (q.v || "").toLowerCase() === lo);
  const like = byVal.length ? byVal : BV.g.parts.filter(q => !q.f && ((q.v || "").toLowerCase().includes(lo) || q.r.toLowerCase().startsWith(lo)));
  const nets = BV.g.nets.map((q, i) => [q, i]).filter(([q]) => q.toLowerCase().includes(lo));
  if (like.length === 1 && !nets.length) return bvSelect(like[0].r, null, true);
  if (nets.length === 1 && !like.length) return bvSelect(null, nets[0][1]);
  BV.sel = { ref: null, net: null, list: { text: t, parts: like.slice(0, 200).map(q => q.r), nets: nets.slice(0, 100) } };
  bvInfo(); bvDraw();
}
const bvRefLink = r => `<a class="ref" data-bvref="${esc(r)}">${esc(r)}</a>`;
function bvInfo() {
  if (tab !== "map" || !BV.g) return;
  const el = $("#bv-info"), sel = BV.sel, g = BV.g;
  if (sel.list) {
    const L = sel.list;
    el.innerHTML = `<p class="dim">“${esc(L.text)}”: ${plural(L.parts.length, "part")}, ${plural(L.nets.length, "net")}.</p>
      ${L.parts.length ? `<div class="bv-net">${L.parts.map(r => `${bvRefLink(r)} <span class="dim">${esc(BV.parts[r].v || "")}</span>`).join("<br>")}</div>` : ""}
      ${L.nets.length ? `<h3>NETS</h3><div class="bv-net">${L.nets.map(([n, i]) => `<a data-bvnet="${i}">${esc(n)}</a>`).join(" · ")}</div>` : ""}`;
    return;
  }
  const p = sel.ref && BV.parts[sel.ref];
  let html = "";
  if (p) {
    const st = BV.st[p.r] || { st: "off", why: "" }, ln = p.l ? g.lines[p.l] : null, side = sideOf(p), k = ustat(st);
    const kind = st.kind ? KIND_NAMES[st.kind] || st.kind : null;
    const aligned = S.sides[side] && S.sides[side].aligned;
    html += `<div class="selpanel" style="position:static"><div class="head"><span class="ref">${esc(p.r)}</span>${p.f ? `<span class="chip st-off">FIDUCIAL</span>` : chip(k, covNote(st), "big solid")}</div>
      <div class="what">${esc(p.v || "")} · ${kind ? esc(kind) : p.f ? "used to line the photo up" : "not in the check"} · ${esc(p.k)} · ${side}</div>
      <div class="kv">
        <b>BOM line</b><span>${ln ? `${esc(ln.value || "")} ${ln.description ? `<span class="dim">${esc(ln.description)}</span>` : ""} · ${plural(ln.qty, "part")}${ln.dnp ? " · DNP" : ""}` : `<span class="dim">not in the BOM</span>`}</span>
        ${p.f ? "" : `<b>Why</b><span>${esc(st.why || STATUS[k].tip)}</span><b>Learned</b><span>${st.good || 0} good, ${st.bad || 0} bad${st.extra ? " · added one by one" : ""}</span>`}
      </div>
      ${p.f ? "" : `<div class="addrow"><button id="bv-look" class="${k === "off" ? "" : "accent"}" ${aligned ? "" : `disabled title="Line up a photo of the ${side} first"`}>SELECT ON THE ${side.toUpperCase()} PHOTO</button>
        ${k !== "off" ? "" : `<span class="grow"></span><select id="bv-kind" title="Check it as">${kindOptions()}</select><button id="bv-add" class="accent">+ ADD TO THE CHECK</button>`}</div>`}</div>`;
    const rows = BV.pins ? (BV.pins[p.r] || []).map((q, i) => [q, i]).sort((a, b) => natural(a[0][0], b[0][0])) : null;
    html += `<h3>PINS${rows ? ` (${rows.length})` : ""}</h3>` + (!rows ? `<p class="dim">Reading the pins...</p>`
      : `<div class="bv-pins"><table><thead><tr><th>Pin</th><th>Net</th><th>Parts on it</th></tr></thead><tbody>${rows.map(([q]) => {
        const n = q[3], cnt = n >= 0 ? new Set((BV.netPins[n] || []).map(([r]) => r)).size : 0;
        return `<tr class="${n >= 0 && n === sel.net ? "on" : ""}"><td>${esc(q[0])}</td><td class="net">${n >= 0 ? `<a data-bvnet="${n}" data-bvkeep="1">${esc(g.nets[n])}</a>` : `<span class="dim">not connected</span>`}</td><td>${n >= 0 ? cnt : ""}</td></tr>`;
      }).join("")}</tbody></table></div>`);
  }
  if (sel.net != null && BV.netPins) {
    const pins = BV.netPins[sel.net] || [], by = {};
    for (const [r, i] of pins) (by[r] = by[r] || []).push(BV.pins[r][i][0]);
    const refs = Object.keys(by).sort(natural);
    const nFront = refs.filter(r => BV.parts[r].s === "top").length;
    html += `<h3>NET ${esc(g.nets[sel.net])}</h3><p class="dim">${plural(pins.length, "pin")} on ${plural(refs.length, "part")}: ${nFront} on the front, ${refs.length - nFront} on the back.
      Pins on the other side show as yellow rings; FLIP to see them.</p>
      <div class="bv-pins"><table><thead><tr><th>Part</th><th>Side</th><th>Value</th><th>Pins</th></tr></thead><tbody>${refs.map(r => `<tr><td>${bvRefLink(r)}</td><td>${sideOf(BV.parts[r])}</td><td>${esc(BV.parts[r].v || "")}</td><td>${esc(by[r].sort(natural).join(", "))}</td></tr>`).join("")}</tbody></table></div>`;
  }
  el.innerHTML = html || `<p class="dim">Click a part or pin on the board, or find a ref, value or net above.</p>`;
}
$("#bv-info").addEventListener("click", e => {
  const a = e.target.closest("[data-bvref]"); if (a) return bvSelect(a.dataset.bvref, null, true);
  const n = e.target.closest("[data-bvnet]");
  if (n) return bvSelect(n.dataset.bvkeep ? BV.sel.ref : null, +n.dataset.bvnet);
  const p = BV.sel.ref && BV.parts[BV.sel.ref]; if (!p) return;
  if (e.target.id === "bv-look") { const side = sideOf(p); goTab(side); lookAt(side, p.r); }
  if (e.target.id === "bv-add") act(() => post("api/part/extra", { ref: p.r, kind: $("#bv-kind").value }), `${p.r} put in the check. Answer it on the ${sideOf(p)} tab, or wait for a new photo.`).then(() => { PL.uid = null; bvStatus(); });
});
$("#bv-q").addEventListener("change", e => bvFind(e.target.value));
$("#bv-q").addEventListener("keydown", e => { if (e.key === "Enter") bvFind(e.target.value); });
$("#bv-flip").addEventListener("click", bvFlip);
$("#zoom-map").addEventListener("click", e => {
  const b = e.target.closest("[data-z]"); if (!b || !BV.view) return;
  if (b.dataset.z === "fit") { BV.view = null; return bvDraw(); }
  const { w, h } = bvCanvas(), f = b.dataset.z === "in" ? 1.4 : 1 / 1.4, v = BV.view;
  v.tx = w / 2 - (w / 2 - v.tx) * f; v.ty = h / 2 - (h / 2 - v.ty) * f; v.s *= f; bvDraw();
});
$("#bv-op").addEventListener("input", e => { BV.photoOp = +e.target.value; keepBv(); bvDraw(); });
$("#bv-photo-on").addEventListener("change", e => { BV.photoOn = e.target.checked; $("#bv-op").disabled = !BV.photoOn; keepBv(); bvDraw(); });
$("#bv-rats").addEventListener("change", e => { BV.rats = e.target.checked; keepBv(); bvDraw(); });
document.addEventListener("keydown", e => {
  if (tab !== "map" || !BV.g || e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, select, textarea")) return;
  if (e.key === "f" || e.key === "F") { e.preventDefault(); bvFlip(); }
  else if (e.key === "0") { BV.view = null; bvDraw(); }
  else if (e.key === "Escape") bvSelect(null);
});
(() => {
  const box = $("#bv"), cv = $("#bv-canvas"), tip = $("#bv-hover");
  let drag = null;
  cv.addEventListener("wheel", e => {
    if (!BV.view) return;
    e.preventDefault();
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const k = Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)), v = BV.view;
    const ns = Math.min(Math.max(v.s * k, 5), 40000), f = ns / v.s;
    v.tx = mx - (mx - v.tx) * f; v.ty = my - (my - v.ty) * f; v.s = ns;
    bvDraw();
  }, { passive: false });
  cv.addEventListener("pointerdown", e => { if (!BV.view) return; drag = { x: e.clientX, y: e.clientY, tx: BV.view.tx, ty: BV.view.ty, moved: false }; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener("pointermove", e => {
    const r = cv.getBoundingClientRect();
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; box.classList.add("drag"); }
      if (drag.moved) { BV.view.tx = drag.tx + dx; BV.view.ty = drag.ty + dy; bvDraw(); }
      tip.hidden = true; return;
    }
    if (!BV.g || !BV.view) return;
    const h = bvHit(e.clientX - r.left, e.clientY - r.top);
    if (!h) { tip.hidden = true; return; }
    const p = BV.parts[h.ref], q = h.i != null ? BV.pins[h.ref][h.i] : null;
    tip.textContent = q ? `${p.r} pin ${q[0]} · ${q[3] >= 0 ? BV.g.nets[q[3]] : "not connected"}` : `${p.r} · ${p.v || p.k}`;
    tip.hidden = false; tip.style.left = (e.clientX - r.left + 14) + "px"; tip.style.top = (e.clientY - r.top + 12) + "px";
  });
  cv.addEventListener("pointerleave", () => { tip.hidden = true; });
  cv.addEventListener("pointerup", e => {
    const d = drag; drag = null; box.classList.remove("drag");
    if (!d || d.moved || !BV.g) return;
    const r = cv.getBoundingClientRect(), h = bvHit(e.clientX - r.left, e.clientY - r.top);
    if (!h) return bvSelect(null);
    if (h.i != null) { const q = BV.pins[h.ref][h.i]; return bvSelect(h.ref, q[3] >= 0 ? q[3] : null); }
    bvSelect(h.ref);
  });
  window.addEventListener("resize", () => bvDraw());
})();
function renderViewer() {
  $("#bv-op").value = BV.photoOp; $("#bv-op").disabled = !BV.photoOn;
  $("#bv-photo-on").checked = BV.photoOn; $("#bv-rats").checked = BV.rats;
  if (!S.project) return;
  if (BV.uid !== S.project.uid || !BV.g) bvOpen(); else { bvStatus(); }
}

// ---- models: one per job, trained on this machine (models.py) ----------------------------------
const MD = { timer: null, all: false };
const pct = v => v == null ? "–" : `${Math.round(v * 100)}%`;
const AUTO_NOTE = "Accuracy measured mostly on auto-made examples is a rough guide until more of your own answers exist.";
async function loadModels() {
  clearTimeout(MD.timer);
  let r;
  try { r = await api(`api/models${MD.all ? "?group=all" : ""}`); } catch (e) { say(e.message, true); return; }
  if (tab !== "models") return;
  const busy = r.boards.some(b => b.progress && b.progress.state === "running");
  $("#md-list").innerHTML = r.boards.map(b => {
    const p = b.progress, run = p && p.state === "running";
    return `<h2>${esc(b.board)}</h2>
      <div class="row"><button data-train="${esc(b.board)}" class="accent" ${run ? "disabled" : ""}>RETRAIN ALL THREE</button>
        <span class="dim grow">${run ? `Training: ${esc(p.step || "")}${p.pct != null ? ` (${p.pct}%)` : ""}. It runs in the background at low priority; you can keep working.`
          : p && p.state === "failed" ? `<span class="st-wrong">The last training stopped: ${esc(p.error || "")}</span>`
          : p && p.state === "done" ? `Last trained ${esc(p.finished || "")}.` : "Not trained yet."} Pictures described by: ${esc(b.features)}.</span></div>
      <div><table class="md"><thead><tr><th>Model</th><th>Examples</th><th>Accuracy on parts it wasn't trained on</th><th>Trained</th><th></th></tr></thead><tbody>
      ${b.jobs.map(j => {
        const m = j.metrics;
        const auto = m ? Object.entries(m.auto || {}).map(([k, v]) => `${esc(k)} ${v}`).join(", ") : "";
        const kinds = m ? Object.entries(m.kinds || {}).map(([k, v]) => `${esc(KIND_NAMES[k] || k)} ${v.pos}${j.job === "orientation" ? "" : "/" + v.neg}`).join(" · ") : "";
        const bk = m ? Object.entries(m.by_kind || {}).filter(([, v]) => v.total).map(([k, v]) => `${esc(KIND_NAMES[k] || k)} ${pct(v.right / v.total)} of ${v.total}`).join(" · ") : "";
        return `<tr><td><b>${esc(j.title)}</b><div class="dim">${esc(j.question)} ${esc(j.learns)}</div></td>
          <td>${!m ? `<span class="dim">–</span>` : `${j.job === "orientation" ? `${m.pos} pictures of ${m.parts} square parts` : `${m.pos} ${esc(j.pos)}, ${m.neg} ${esc(j.neg)}`}
            <div class="dim">Your own answers: <b>${m.own}</b>${auto ? ` · auto-made: ${auto}` : ""}</div>
            ${kinds ? `<div class="dim">By kind (${esc(j.pos)}${j.job === "orientation" ? "" : "/" + esc(j.neg)}): ${kinds}</div>` : ""}`}</td>
          <td>${!m ? "" : m.enough ? `<b>${pct(m.accuracy)}</b> <span class="dim">(guessing would get ${pct(m.baseline)})</span>${bk ? `<div class="dim">${bk}</div>` : ""}${m.rough ? `<div class="st-check">${AUTO_NOTE}</div>` : ""}${m.used ? `<div class="st-ok">Its opinion is shown on checks.</div>` : `<div class="st-check">Not shown on checks until its accuracy is ${pct(b.use_accuracy)} or more.</div>`}`
            : `<span class="st-check">Too few examples: it needs ${esc(j.need)}.</span>${m.short && m.short.length ? `<div class="dim">Still needed: ${esc(m.short.join("; "))}.</div>` : ""}${m.accuracy != null ? `<div class="dim">So far ${pct(m.accuracy)} (guessing would get ${pct(m.baseline)}); not used until it has enough.</div>` : ""}`}</td>
          <td>${m ? esc(m.trained) : `<span class="dim">never</span>`}</td>
          <td><button data-train="${esc(b.board)}" data-job="${j.job}" ${run ? "disabled" : ""}>RETRAIN</button></td></tr>`;
      }).join("")}</tbody></table></div>`;
  }).join("") || `<p class="dim">No board design in this project yet. Load its ODB++ and BOM on Files.</p>`;
  if (busy) MD.timer = setTimeout(loadModels, 2000);
}
$("#md-list").addEventListener("click", async e => {
  const b = e.target.closest("[data-train]"); if (!b) return;
  try {
    await post("api/models/train", { board: b.dataset.train, jobs: b.dataset.job ? [b.dataset.job] : null });
    say("Training started in the background. This page shows how it's going.");
  } catch (err) { say(err.message, true); }
  loadModels();
});
$("#md-all").addEventListener("change", e => { MD.all = e.target.checked; loadModels(); });

// ---- history -------------------------------------------------------------------------------
const H = { q: "", sel: null, list: [], timer: null, kind: "projects", all: false };
const histGroup = () => H.all ? "&group=all" : "";
$("#hist-all").addEventListener("change", e => { H.all = e.target.checked; H.sel = null; $("#hist-detail").innerHTML = ""; loadHistory(); });
function setHistKind(k, load = true) {
  if (H.kind !== k) { H.kind = k; H.sel = null; $("#hist-detail").innerHTML = ""; }
  $$("#hist-kind button").forEach(b => b.classList.toggle("on", b.dataset.k === k));
  const cols = { projects: ["Updated", "Board", "Serial", "Front", "Back"], checks: ["Updated", "Board", "Serial", "Result", "Flagged"],
    learned: ["Board", "Front good", "Front bad", "Back good", "Back bad"] }[k];
  cols.forEach((t, i) => { $(`#hist-c${i + 1}`).textContent = t; });
  if (load) loadHistory();
}
$("#hist-kind").addEventListener("click", e => { const b = e.target.closest("button"); if (b) setHistKind(b.dataset.k); });
const kb = n => n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} kB`;
const fileUrl = (uid, path, extra = "") => `api/history/${encodeURIComponent(uid)}/file/${path.split("/").map(encodeURIComponent).join("/")}${extra}`;
async function loadHistory() {
  if (H.kind === "checks") return loadChecks();
  if (H.kind === "learned") return loadLearned();
  try {
    const h = await api(`api/history?limit=200&q=${encodeURIComponent(H.q)}${histGroup()}`);
    H.list = h.projects;
    $("#hist-status").textContent = (h.source === "database" ? `${h.total} board(s) mapped${H.all ? "" : " in this project"}, from the database.` : h.status) +
      (H.q ? ` ${h.projects.length} match.` : "");
    histRows(h.projects.map(r => `<tr class="click ${r.uid === H.sel ? "sel" : ""}" data-uid="${esc(r.uid)}">
      <td>${esc(r.updated.slice(0, 16))}</td><td>${esc(r.board)}${r.uid === h.current ? ` <span class="ok">OPEN</span>` : ""}${r.reference ? ` <span class="dim">REFERENCE</span>` : ""}</td><td>${esc(r.serial)}</td>
      <td>${sideCell(r.front)}</td><td>${sideCell(r.back)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="no">${H.q ? "Nothing matches." : "No boards mapped yet."}</td></tr>`);
    if (!H.sel && h.projects.length) showProject(h.projects[0].uid);
    else if (H.sel) showProject(H.sel);
  } catch (e) { say(e.message, true); }
}
// the chosen row opens under itself (one scroller for the list and what's chosen)
function placeDetail(uid) {
  $$("#hist-list tr[data-uid]").forEach(tr => tr.classList.toggle("sel", tr.dataset.uid === uid));
  const det = $("#hist-detail"), old = $("#hist-list tr.detailrow");
  const tr = $$("#hist-list tr[data-uid]").find(x => x.dataset.uid === uid);
  if (!tr) { $("#hist-scroll").append(det); if (old) old.remove(); return; }
  let row = old;
  if (!row) { row = document.createElement("tr"); row.className = "detailrow"; row.innerHTML = `<td colspan="5"></td>`; }
  if (tr.nextElementSibling !== row) tr.after(row);
  row.firstElementChild.append(det);
}
function histRows(html) { $("#hist-scroll").append($("#hist-detail")); $("#hist-list tbody").innerHTML = html; }
const sideCell = s => !s.parts ? `<span class="dim">none</span>` : s.aligned ? `${s.checkable}/${s.parts}` : s.photo ? "photo" : `<span class="dim">–</span>`;
$("#hist-search").addEventListener("input", e => { H.q = e.target.value; clearTimeout(H.timer); H.timer = setTimeout(loadHistory, 300); });
$("#hist-list").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-uid]"); if (!tr || e.target.closest("#hist-detail")) return;
  if (H.sel === tr.dataset.uid && $("#hist-list tr.detailrow")) { H.sel = null; return placeDetail(null); }     // click again to fold it
  ({ checks: showCheck, learned: showLearned }[H.kind] || showProject)(tr.dataset.uid);
});

async function loadChecks() {
  try {
    const h = await api(`api/history/checks?limit=200&q=${encodeURIComponent(H.q)}${histGroup()}`);
    $("#hist-status").textContent = (h.source === "database" ? `${h.total} check(s)${H.all ? "" : " in this project"}, from the database.` : h.status) + (H.q ? ` ${h.checks.length} match.` : "");
    const flagged = r => { const n = r.front.flagged + r.back.flagged, w = r.front.wrong.length + r.back.wrong.length; return w ? `<span class="st-wrong">${w} wrong</span>` : n ? `${n} looked at` : "none"; };
    histRows(h.checks.map(r => `<tr class="click ${r.uid === H.sel ? "sel" : ""}" data-uid="${esc(r.uid)}">
      <td>${esc(r.updated.slice(0, 16))}</td><td>${esc(r.board)}</td><td>${esc(r.serial)}</td>
      <td><span class="badge ${esc(r.result)}">${esc(r.result)}</span></td><td>${flagged(r)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="no">${H.q ? "Nothing matches." : "No boards checked yet."}</td></tr>`);
    if (!H.sel && h.checks.length) showCheck(h.checks[0].uid);
    else if (H.sel) showCheck(H.sel);
  } catch (e) { say(e.message, true); }
}

const reasonName = k => (PL.reasons[k] || (S && S.reasons && S.reasons[k]) || k);
const STATUS_TEXT = { ok: "right way round", reversed: "looks reversed", rotated: "looks turned", check: "look at it", fault: "known fault", ns: "not stuffed", covered: "covered" };
const FINE = r => r.status === "ok" || r.status === "ns" || r.status === "covered";     // needs no answer
const ckFile = (uid, side, name, extra = "") => `api/check/${encodeURIComponent(uid)}/file/${side}/${name}${extra}`;
const ckCrop = (uid, side, ref, which) => `api/check/${encodeURIComponent(uid)}/crop/${side}/${encodeURIComponent(ref)}?which=${which}`;
// a part of a board check, in the words and colours used everywhere
function ckChip(r) {
  if (r.answer === "correct") return chip("good", "", "solid");
  if (r.answer === "wrong") return chip(r.reason === "missing" ? "missing" : "bad", "", "solid");
  if (r.status === "ns") return chip("ns");
  if (r.status === "covered") return chip("cant", ` · covered: ${esc(r.cover || "heatsink")}`);
  if (r.missed) return `<span class="chip st-bad">MISSED BY THE CHECK</span>`;
  if (r.new) return `<span class="chip st-in">NEW PART, TEACH ME</span>`;
  if (r.since) return `<span class="chip st-in" title="Put in the check (answered on another board of the design) after this board was checked">NEW SINCE LAST CHECK</span>`;
  return `<span class="chip ${r.status === "check" ? "st-in" : "st-bad"}" title="Not answered yet">${esc(STATUS_TEXT[r.status].toUpperCase())}</span>`;
}
function ckLegend(s) {
  const rs = s.results || [], n = f => rs.filter(f).length;
  const item = (k, v, label) => `<span class="count st-${k}"><i></i><b>${v}</b><span>${label}</span></span>`;
  $("#ck-legend").innerHTML = !s.aligned ? "" : `<span class="count all"><b>${rs.length}</b><span>parts checked</span></span>`
    + item("in", n(r => !FINE(r) && !r.answer), "to look at") + item("good", n(r => r.answer === "correct" || (r.status === "ok" && !r.answer)), "good")
    + item("bad", n(r => r.answer === "wrong" && r.reason !== "missing"), "bad") + item("missing", n(r => r.answer === "wrong" && r.reason === "missing"), "missing")
    + item("ns", n(r => r.status === "ns"), "NS") + item("cant", (s.skipped || []).length + n(r => r.status === "covered"), "can't be seen");
}
function flaggedHtml(c, side, editable) {
  const st = c.sides[side] || {}, rs = (st.results || []).filter(r => !FINE(r) || r.missed);
  return rs.map(r => `<div class="flag ${r.answer ? "answered" : ""}">
      <div class="row tight" style="gap:8px"><b style="font-size:17px">${esc(r.ref)}</b>${ckChip(r)}<span class="dim ell" style="margin:0">${KIND_NAMES[r.kind] || esc(r.kind)} ${esc(r.value)}</span></div><div>
        <div class="dim">${esc(r.why || "")}</div>${modelLine(r)}${editable ? coverHint(r.suggest, r.cover, `data-ref="${esc(r.ref)}" data-cov`) : ""}</div>
      <div class="pics"><figure><img loading="lazy" src="${ckCrop(c.uid, side, r.ref, "reference")}" alt="">reference</figure>
        <figure><img loading="lazy" src="${ckCrop(c.uid, side, r.ref, "board")}" alt="">this board</figure></div>
      ${editable ? `<div class="answers">${r.missed ? "" : `<button data-ans="correct" data-ref="${esc(r.ref)}" class="a-good ${r.answer === "correct" ? "on" : ""}" title="It's right: the right way round">GOOD</button>`}
        <button data-ans="wrong" data-ref="${esc(r.ref)}" class="a-bad ${r.answer === "wrong" && r.reason !== "missing" ? "on" : ""}" title="It's wrong (pick why beside)">BAD</button>
        <button data-ans="missing" data-ref="${esc(r.ref)}" class="a-missing ${r.answer === "wrong" && r.reason === "missing" ? "on" : ""}" title="It should be there but isn't: a fault">MISSING</button>
        ${r.missed ? "" : `<button data-ns="board" data-ref="${esc(r.ref)}" class="a-ns" title="Not stuffed: left off this board on purpose, not a fault">NS</button>
        <button data-cov="heatsink" data-ref="${esc(r.ref)}" class="a-covered" title="Can't be seen on this board (a heatsink, a label...): not a fault, still checked on every other board">COVERED</button>
        <details class="more"><summary>MORE</summary><div class="menu"><button data-ns="all" data-ref="${esc(r.ref)}" title="Not stuffed on every board of this design from now on">NS ON EVERY BOARD</button></div></details>`}</div>
        <div class="row">${reasonSelect("reason", r.reason).replace("<select ", `<select data-reason="${esc(r.ref)}" `)}
        <input class="note" data-note="${esc(r.ref)}" value="${esc(r.note || "")}" placeholder="Note (kept with the example)"></div>
        ${r.answer ? `<div class="dim learnt">Learned as a ${r.answer === "correct" ? "good" : "bad"} example of ${esc(r.ref)}${r.example ? "" : " (since deleted)"}.</div>` : ""}`
      : `<div>${r.answer === "wrong" ? `<span class="st-wrong">Answered: wrong${r.reason ? " · " + esc(reasonName(r.reason)) : ""}${r.note ? " · " + esc(r.note) : ""}</span>` : r.answer === "correct" ? `Answered: right way round${r.note ? " · " + esc(r.note) : ""}` : `<span class="st-check">Not answered</span>`}</div>`}
    </div>`).join("");
}

async function showCheck(uid) {
  H.sel = uid;
  placeDetail(uid);
  let c;
  try { c = await api(`api/check/${encodeURIComponent(uid)}`); } catch (e) { $("#hist-detail").innerHTML = `<p class="dim">${esc(e.message)}</p>`; return; }
  if (H.sel !== uid) return;
  const sides = Object.keys(c.sides);
  $("#hist-detail").innerHTML = `
    <h2>${esc(c.board)}${c.serial ? "  ·  " + esc(c.serial) : ""}  <span class="badge ${esc(c.result)}">${esc(c.result)}</span></h2>
    <div class="row"><button class="accent" data-ckopen="${esc(uid)}">OPEN IN CHECK A BOARD</button></div>
    <div class="kv">
      <b>Check</b><span>${esc(c.uid)}</span>
      <b>Started</b><span>${esc(c.created)} on ${esc(c.station)}</span>
      <b>Reference</b><span>${c.reference_serial === null ? esc(c.reference) + " (no longer there)" : `<a href="#" data-showproj="${esc(c.reference)}">${esc(c.reference)}</a>${c.reference_serial ? " · " + esc(c.reference_serial) : ""}`}</span>
      ${sides.map(s => { const st = c.sides[s]; const rs = st.results || []; return `<b>${s === "front" ? "Front" : "Back"}</b><span>${st.aligned ? `${rs.length} parts checked, ${rs.filter(r => !FINE(r)).length} flagged` : st.photo ? "photo not lined up" : "not checked"}</span>`; }).join("")}
      <b>Notes</b><span>${esc(c.notes) || "–"}</span>
      <b>Folder</b><span class="mono">${esc(c.folder)}</span>
    </div>
    <h3>RESULT PHOTOS</h3>
    <div class="gallery">${sides.filter(s => c.sides[s].aligned).map(s => `<a href="${ckFile(uid, s, "result.jpg")}" target="_blank" rel="noopener"><img loading="lazy" src="${ckFile(uid, s, "result.jpg", `?thumb=360&v=${encodeURIComponent(c.updated)}`)}" alt=""><span>${s}</span></a>`).join("") || `<p class="dim">No side was lined up.</p>`}</div>
    ${sides.map(s => { const h = flaggedHtml(c, s, false); return h ? `<h3>FLAGGED ON THE ${s.toUpperCase()}</h3>${h}` : ""; }).join("")}
    ${sides.map(s => { const cv = (c.sides[s].results || []).filter(r => r.status === "covered"); return cv.length ? `<h3>COVERED ON THE ${s.toUpperCase()}</h3>
      <p class="dim">${cv.map(r => `<b>${esc(r.ref)}</b> ${chip("cant", ` · covered: ${esc(r.cover || "heatsink")}`)}`).join(" ")}</p>` : ""; }).join("")}
    <h3>LOG</h3>
    <div class="list log">${c.log.map(e => `<div><span class="dim">${esc(e.at)}</span><span class="grow">${esc(e.what)}</span></div>`).join("")}</div>`;
}
document.addEventListener("click", e => {
  const o = e.target.closest("[data-ckopen]");
  if (o) {    // a check of another project (from All projects) chooses that project
    post("api/check/open", { uid: o.dataset.ckopen }).then(async st => { CK.st = st; S = await api("api/state"); S._r = Date.now(); recentAt = 0; loadGroups(); goTab("check"); })
      .catch(err => say(err.message, true));
  }
  const pr = e.target.closest("[data-showproj]");
  if (pr) { e.preventDefault(); setHistKind("projects", false); H.sel = pr.dataset.showproj; loadHistory(); }
});

// what the check has learned, per board design and side
async function loadLearned() {
  try {
    const h = await api(`api/check/learned?${histGroup().slice(1)}`), q = H.q.toLowerCase();
    const rows = h.boards.filter(b => !q || b.board.toLowerCase().includes(q));
    $("#hist-status").textContent = "Examples the testers' answers have taught the board check, per board design.";
    histRows(rows.map(b => `<tr class="click ${b.board === H.sel ? "sel" : ""}" data-uid="${esc(b.board)}">
      <td>${esc(b.board)}</td><td>${b.front.good}</td><td>${b.front.bad ? `<span class="st-wrong">${b.front.bad}</span>` : 0}</td>
      <td>${b.back.good}</td><td>${b.back.bad ? `<span class="st-wrong">${b.back.bad}</span>` : 0}</td></tr>`).join("")
      || `<tr><td colspan="5" class="no">${H.q ? "Nothing matches." : "No board types yet."}</td></tr>`);
    if (!rows.some(b => b.board === H.sel)) H.sel = rows.length ? rows[0].board : null;
    if (H.sel) showLearned(H.sel);
  } catch (e) { say(e.message, true); }
}
const exUrl = (board, side, id) => `api/check/learned/${encodeURIComponent(board)}/${side}/${encodeURIComponent(id)}`;
async function showLearned(board) {
  H.sel = board; H.lside = H.lside || "front";
  placeDetail(board);
  const side = H.lside;
  let d;
  try { d = await api(`api/check/learned/${encodeURIComponent(board)}/${side}`); } catch (e) { $("#hist-detail").innerHTML = `<p class="dim">${esc(e.message)}</p>`; return; }
  if (H.sel !== board || H.lside !== side) return;
  const byRef = {};
  d.examples.forEach(e => (byRef[e.ref] = byRef[e.ref] || []).push(e));
  const refs = Object.keys(byRef).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const wrong = Object.entries(d.reference_wrong);
  $("#hist-detail").innerHTML = `
    <h2>WHAT THE CHECK HAS LEARNED  ·  ${esc(board)}</h2>
    <div class="row"><div class="seg" id="lr-side">${["front", "back"].map(s => `<button data-s="${s}" class="${s === side ? "on" : ""}">${s.toUpperCase()}</button>`).join("")}</div>
      <span class="dim">${d.examples.filter(e => e.label === "good").length} good · ${d.examples.filter(e => e.label === "bad").length} bad${d.examples.some(e => e.label === "covered") ? ` · ${d.examples.filter(e => e.label === "covered").length} covered` : ""}</span></div>
    <p class="dim">Each answer on Check a board, and each part taught GOOD or BAD from a photo, is kept here: a good example, or a bad one
      with its reason and note. Later boards of this design are compared with these as
      well as the reference: a part like a good example isn't flagged again, and one like a bad example is flagged as a <b>known fault</b>.
      Up to 20 of each are kept per part, the oldest dropped. Delete any that were answered by mistake.</p>
    ${wrong.length ? `<h3>THE REFERENCE SHOWS THESE WRONG</h3><p class="dim">A tester flagged them as wrong though the check passed them, so the reference turned round counts as their right look. Trust the reference again to undo it.</p>
      ${wrong.map(([ref, rs]) => `<div class="row"><span class="dim">reference ${esc(ref)}:</span>${rs.map(r => `<button data-trust="${esc(r)}" data-reference="${esc(ref)}" title="Trust the reference for this part again">${esc(r)} ✕</button>`).join("")}</div>`).join("")}` : ""}
    ${refs.length ? refs.map(ref => `<h3>${esc(ref)} <span class="dim">${byRef[ref].length} example(s)</span></h3><div class="examples">${byRef[ref].map(e => `
      <div class="ex ${e.label}"><img loading="lazy" src="${exUrl(board, side, e.id)}" alt="${esc(e.ref)}, ${e.label}">
        <div class="grow"><span class="${e.label === "good" ? "st-ok" : e.label === "covered" ? "st-cant" : "st-wrong"}">${e.label === "good" ? "GOOD · right way round" : e.label === "covered" ? `COVERED · ${esc(e.note || "heatsink")} (only used to ask "covered?" on later boards)` : "BAD · wrong"}</span>
          ${e.reason ? `<div class="st-wrong">${esc(reasonName(e.reason))}</div>` : ""}${e.note && e.label !== "covered" ? `<div>${esc(e.note)}</div>` : ""}
          <div class="dim">${esc(e.serial || "no serial")} · ${esc(e.date)}${e.check ? ` · <a href="#" data-showck="${esc(e.check)}">check</a>` : e.source ? " · taught from a mapped photo" : ""}</div></div>
        <button data-delex="${esc(e.id)}" class="small">DELETE</button></div>`).join("")}</div>`).join("")
      : `<p class="dim">Nothing learned for the ${side} yet. Answer the flagged parts on Check a board and they appear here.</p>`}
    <p class="dim">Kept in <span class="mono">${esc(d.folder)}</span></p>`;
  $("#lr-side").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { H.lside = b.dataset.s; showLearned(board); } });
  $$("#hist-detail [data-delex]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Delete this example? The check stops using it.")) return;
    try { await post(`api/check/learned/${encodeURIComponent(board)}/${side}/delete`, { id: b.dataset.delex }); say("Example deleted."); loadLearned(); } catch (e) { say(e.message, true); }
  }));
  $$("#hist-detail [data-trust]").forEach(b => b.addEventListener("click", async () => {
    try { await post(`api/check/learned/${encodeURIComponent(board)}/${side}/trust`, { reference: b.dataset.reference, ref: b.dataset.trust }); say(`The reference is trusted again for ${b.dataset.trust}.`); showLearned(board); } catch (e) { say(e.message, true); }
  }));
  $$("#hist-detail [data-showck]").forEach(a => a.addEventListener("click", e => { e.preventDefault(); setHistKind("checks", false); H.sel = a.dataset.showck; loadHistory(); }));
}

async function showProject(uid) {
  H.sel = uid;
  placeDetail(uid);
  let d;
  try { d = await api(`api/history/${encodeURIComponent(uid)}`); } catch (e) { $("#hist-detail").innerHTML = `<p class="dim">${esc(e.message)}</p>`; return; }
  if (H.sel !== uid) return;
  const isOpen = S && S.project && S.project.uid === uid;
  const sideLine = side => { const s = d[side]; return !s.parts ? "no polarised parts" : s.aligned ? `${s.checkable} of ${s.parts} checkable` : s.photo ? `${s.parts} polarised · photo not lined up` : `${s.parts} polarised · no photo`; };
  const named = f => f.kind === "odb" && d.odb_file ? d.odb_file : f.kind === "bom" && d.bom_file ? d.bom_file : f.path;
  const imgs = d.files.filter(f => ["photo", "map", "closeup"].includes(f.kind));
  const label = f => `${f.side} ${f.kind === "photo" ? "photo" : f.kind === "map" ? "map" : "closeup " + (parseInt(f.path.split("/").pop(), 10) + 1)}`;
  const order = { photo: 0, map: 1, closeup: 2 };
  imgs.sort((a, b) => a.side.localeCompare(b.side) * -1 || order[a.kind] - order[b.kind] || a.path.localeCompare(b.path));
  const csv = d.files.find(f => f.kind === "csv");
  $("#hist-detail").innerHTML = `
    <h2>${esc(d.board)}${d.serial ? "  ·  " + esc(d.serial) : ""}</h2>
    <div class="row">
      ${isOpen ? `<button data-goto="files" class="accent">OPEN NOW</button>` : `<button class="accent" data-reopen="${esc(uid)}">REOPEN</button>`}
      <button data-newfrom="${esc(uid)}">NEW BOARD, SAME DESIGN</button>
      <button id="hd-ref" class="${d.reference ? "on" : ""}">${d.reference ? "REFERENCE FOR CHECKS ✓" : d.replaced_reference ? "REVIEW THE NEW REFERENCE PHOTO" : "USE AS REFERENCE"}</button>
      ${csv ? `<a class="btn" href="${fileUrl(uid, csv.path, `?download=${encodeURIComponent(d.board + (d.serial ? "_" + d.serial : "") + "_parts_map.csv")}`)}">DOWNLOAD CSV</a>` : ""}
    </div>
    <div class="kv">
      <b>Project</b><span>${esc(d.uid)}${d.in_db === false ? " · not in the database yet" : ""}</span>
      <b>Started</b><span>${esc(d.created)} on ${esc(d.station)}</span>
      <b>Last change</b><span>${esc(d.updated)}</span>
      <b>ODB++ · BOM</b><span>${esc(d.odb_file || "–")} · ${esc(d.bom_file || "–")}</span>
      <b>Polarised parts</b><span>${d.parts}${d.units ? " · units " + esc(d.units) : ""}</span>
      <b>Front</b><span>${sideLine("front")}</span>
      <b>Back</b><span>${sideLine("back")}</span>
      <b>Closeups</b><span>${d.closeups}</span>
      <b>Folder</b><span class="mono">${esc(d.folder)}</span>
    </div>
    ${d.reference_changes && d.reference_changes.length ? `<h3>REFERENCE CHANGES</h3>
      <div class="list">${d.reference_changes.map(c => `<div><span class="grow">${esc(c.created.slice(0, 16))} · ${esc(c.old_label)} → ${esc(c.new_label)}
        <span class="dim">${c.status === "open" ? `waiting: ${c.left} of ${c.differences} left` : c.status === "done" ? `done ${esc((c.finished || "").slice(0, 16))}` : esc(c.status)} · ${c.same_count} the same</span></span>
        <button class="small ${c.status === "open" ? "accent" : ""}" data-review="${esc(c.id)}">${c.status === "open" ? "REVIEW" : "SEE"}</button></div>`).join("")}</div>` : ""}
    <h3>SERIAL AND NOTES</h3>
    <div class="grid2">
      <span class="lab">SERIAL</span><input id="hd-serial" value="${esc(d.serial)}" placeholder="Serial number or label">
      <span class="lab">NOTES</span><textarea id="hd-notes" rows="2">${esc(d.notes)}</textarea>
    </div>
    <div class="row"><button id="hd-save">SAVE</button></div>
    <h3>PHOTOS AND MAPS</h3>
    ${imgs.length ? `<div class="gallery">${imgs.map(f => `<a href="${fileUrl(uid, f.path)}" target="_blank" rel="noopener"><img loading="lazy" src="${fileUrl(uid, f.path, `?thumb=360&v=${encodeURIComponent(f.modified)}`)}" alt=""><span>${esc(label(f))}</span></a>`).join("")}</div>`
      : `<p class="dim">No photos were taken for this project.</p>`}
    <h3>CAN'T BE CHECKED</h3>
    ${d.unchecked.length ? `<div><table><thead><tr><th>Side</th><th>Ref</th><th>Kind</th><th>Value</th><th>Why</th></tr></thead><tbody>
      ${d.unchecked.map(r => `<tr><td>${r.side}</td><td>${esc(r.ref)}</td><td>${KIND_NAMES[r.kind] || esc(r.kind)}</td><td>${esc(r.value)}</td><td>${esc(r.reason)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="dim">${d.front.aligned || d.back.aligned ? "Every placed part could be checked." : "No side was lined up."}</p>`}
    <h3>FILES</h3>
    <div><table><thead><tr><th>File</th><th>What</th><th>Size</th><th>Saved</th></tr></thead><tbody>
      ${d.files.map(f => `<tr><td><a href="${fileUrl(uid, f.path, `?download=${encodeURIComponent(named(f).split("/").pop())}`)}">${esc(f.path)}</a>${named(f) !== f.path ? ` <span class="dim">${esc(named(f))}</span>` : ""}</td><td>${esc(f.kind)}${f.side ? " · " + f.side : ""}</td><td>${kb(f.bytes)}</td><td>${esc(f.modified.slice(0, 16))}</td></tr>`).join("")}</tbody></table></div>
    <h3>LOG</h3>
    <div class="list log">${d.log.map(e => `<div><span class="dim">${esc(e.at)}</span><span class="grow">${esc(e.what)}</span></div>`).join("") || `<div class="dim">Nothing logged.</div>`}</div>`;
  $$("#hist-detail [data-review]").forEach(b => b.addEventListener("click", () => openReview(b.dataset.review)));
  $("#hd-ref").addEventListener("click", async () => {
    try {
      if (d.replaced_reference) return reviewStarted(await post("api/refchange/start", { uid }));
      const r = await post(`api/history/${encodeURIComponent(uid)}/reference`, { on: !d.reference });
      if (r.review) return reviewStarted(r);
      if (isOpen) S.project.reference = !d.reference;
      say(!d.reference ? "Now the reference for checking other boards of this type." : "No longer the reference.");
      loadHistory();
    } catch (e) { say(e.message, true); }
  });
  $("#hd-save").addEventListener("click", async () => {
    const r = await saveInfo(uid, { serial: $("#hd-serial").value, notes: $("#hd-notes").value });
    if (!r) return;
    if (isOpen) { S.project.serial = r.serial; S.project.notes = r.notes; }
    say("Saved."); loadHistory();
  });
}

// ---- check a board --------------------------------------------------------------------------
const CK = { st: null, side: "front", view: "result", stream: null, pts: {}, cur: 0, anchors: null, ghostOn: true };
async function loadCheck() {
  try { CK.st = await api("api/check/state"); } catch (e) { say(e.message, true); }
  renderCheck();
}
function ckAct(fn, done) {
  return (async () => {
    try { const st = await fn(); if (st && st.references) CK.st = st; if (done) say(done); }
    catch (e) { say(e.message, true); }
    renderCheck();
  })();
}
function stopCk() { if (CK.stream) { CK.stream.stop(); CK.stream = null; } $("#ck-live").hidden = true; }

function renderCheck() {
  const st = CK.st; if (!st) return;
  const c = st.check;
  $("#ck-start").hidden = !!c; $("#ck-run").hidden = !c;
  if (!c) {
    const refs = st.references.filter(r => Object.keys(r.sides).length && inGroup(r.board));
    $("#ck-ref").innerHTML = refs.map(r => `<option value="${esc(r.uid)}">${esc(r.board)} · reference ${esc(r.serial || r.uid)} (${Object.keys(r.sides).join(" and ")})</option>`).join("");
    $("#ck-noref").hidden = !!refs.length; $("#ck-go").disabled = !refs.length;
  } else {
    const sides = Object.keys(c.sides);
    if (!sides.includes(CK.side)) CK.side = sides[0];
    const s = c.sides[CK.side] || {};
    $("#ck-title").textContent = `${c.board}${c.serial ? "  ·  " + c.serial : ""}`;
    $("#ck-result").className = `badge ${c.result}`; $("#ck-result").textContent = c.result;
    const rs = Object.values(c.sides).flatMap(x => x.results || []), open = rs.filter(r => !FINE(r) && !r.answer).length;
    $("#ck-sub").textContent = c.result === "OPEN" ? (rs.length ? `${open} flagged part(s) to look at` : "Take a photo of the board") :
      c.result === "PASS" ? `All ${rs.length} parts checked are the right way round${sides.some(x => !c.sides[x].aligned) ? " (" + sides.filter(x => c.sides[x].aligned).join(" and ") + " only)" : ""}` :
      `Wrong: ${rs.filter(r => r.answer === "wrong").map(r => r.ref).join(", ")}`;
    $("#ck-sides").innerHTML = sides.map(x => { const y = c.sides[x]; const f = (y.results || []).filter(r => !FINE(r) && !r.answer).length;
      return `<button data-side="${x}" class="${x === CK.side ? "on" : ""}">${x.toUpperCase()}${y.aligned ? (f ? ` · ${f}` : " ✓") : ""}</button>`; }).join("");
    $("#ck-msg").hidden = !s.message; $("#ck-msg").textContent = s.message || "";
    ckLegend(s);
    $("#ck-points").hidden = !(s.photo && !s.aligned);
    if (s.photo && !s.aligned) loadCkAnchors();
    $("#ck-look").hidden = !s.aligned;
    if (s.aligned) loadPartList(c.reference);
    const fl = flaggedHtml(c, CK.side, true);
    $("#ck-flagged").innerHTML = s.aligned ? (fl ? `<h3>FLAGGED: LOOK AT EACH ONE</h3>${fl}` : `<p class="st-ok">Nothing flagged on the ${CK.side}.</p>`) : "";
    const ok = (s.results || []).filter(r => r.status === "ok" && !r.missed);
    const byLearning = ok.filter(r => r.learned === "good").map(r => r.ref), ns = (s.results || []).filter(r => r.status === "ns"),
      cov = (s.results || []).filter(r => r.status === "covered");
    const since = ok.filter(r => r.since).map(r => r.ref);
    $("#ck-ok").innerHTML = s.aligned ? `<h3>GOOD: THE RIGHT WAY ROUND (${ok.length})</h3><p class="dim">${esc(ok.map(r => r.ref).join(", ") || "none")}</p>` +
      (byLearning.length ? `<p class="dim">Passed because they look like parts a tester confirmed: ${esc(byLearning.join(", "))}.</p>` : "") +
      (since.length ? `<p class="dim"><span class="newtag">NEW SINCE LAST CHECK</span> ${esc(since.join(", "))}: put in the check after this board was checked, compared now.</p>` : "") +
      (ok.length ? `<h3>DID THE CHECK MISS ONE?</h3><p class="dim">If one of these is wrong, flag it: it's kept as a bad example, and the reference's look of that part is no longer trusted.</p>
        <div class="row"><select id="ck-miss-ref">${ok.map(r => `<option>${esc(r.ref)}</option>`).join("")}</select>
        ${reasonSelect("reason").replace("<select ", `<select id="ck-miss-reason" `)}<input id="ck-miss-note" class="note" placeholder="Note (kept with the example)"><button id="ck-miss" class="a-bad">FLAG AS BAD</button></div>` : "") +
      (cov.length ? `<h3>COVERED ON THIS BOARD (${cov.length})</h3><p class="dim">Can't be seen on this board, not faults. Still checked on every other board.</p>` +
        cov.map(r => `<div class="row"><b>${esc(r.ref)}</b>${chip("cant", ` · covered: ${esc(r.cover || "heatsink")}`)}<span class="grow"></span><button data-uncov="${esc(r.ref)}">UNDO COVERED</button></div>`).join("") : "") +
      (ns.length ? `<h3>NOT STUFFED (${ns.length})</h3><p class="dim">Left off on purpose, not faults.</p>` +
        ns.map(r => `<div class="row"><b>${esc(r.ref)}</b><span class="dim grow">${esc(r.why)}</span><button data-unns="${esc(r.ref)}">UNDO NS</button></div>`).join("") : "") +
      ((s.skipped || []).length ? `<h3>CAN'T BE SEEN (${s.skipped.length})</h3><p class="dim">Look at these by eye: ${esc(s.skipped.map(r => r.ref).join(", "))}.</p>` : "") : "";
  }
  renderCkViewer();
}

function renderCkViewer() {
  const c = CK.st && CK.st.check, s = c ? (c.sides[CK.side] || {}) : {};
  const live = !!CK.stream;
  $("#ck-video").hidden = !live || CK.stream.canon;
  $("#ck-live").hidden = !live || !CK.stream.canon;
  $("#ck-cam-start").textContent = live ? "STOP CAMERA" : "START CAMERA";
  $("#ck-cam-take").hidden = !live; $("#ck-cam-list").hidden = !live;
  $("#ck-cam-row").style.visibility = c ? "visible" : "hidden";
  $$("#ck-view button").forEach(b => b.classList.toggle("on", b.dataset.v === CK.view));
  $("#ck-view").hidden = !s.aligned;
  const ghost = $("#ck-ghost");
  ghost.hidden = !(live && CK.ghostOn && c);
  if (!ghost.hidden) {
    const src = `api/history/${encodeURIComponent(c.reference)}/file/${CK.side}/reference.jpg?thumb=1600`;
    if (!ghost.src.endsWith(src)) ghost.src = src;
  }
  const img = $("#ck-photo");
  let src = null;
  if (!live && c && s.photo) {
    src = s.aligned && CK.view === "result" ? ckFile(c.uid, CK.side, "result.jpg") : ckFile(c.uid, CK.side, "photo.jpg");
    src += `?v=${encodeURIComponent(c.updated)}`;
  }
  img.hidden = !src;
  if (src && !img.src.endsWith(src)) img.src = src;
  if (live) { const v = $("#ck-video"), l = $("#ck-live"); ZC.size(CK.stream.canon ? l.naturalWidth : v.videoWidth, CK.stream.canon ? l.naturalHeight : v.videoHeight); }
  else if (src && img.naturalWidth && img.complete) ZC.size(img.naturalWidth, img.naturalHeight);
  $("#ck-placeholder").hidden = live || !!src;
  $("#ck-placeholder").textContent = c ? `No photo of the ${CK.side} yet. Start the camera, line the board up with the ghost, and take the photo, or choose a file.` : "Start a check, then take or choose a photo of the board.";
  $("#ck-viewer").classList.toggle("clicking", !!(src && s.photo && !s.aligned));
  drawCkMarks();
}

async function loadCkAnchors() {
  const key = `${CK.st.check.uid}/${CK.side}`;
  if (CK.anchors && CK.anchors.key === key) return renderCkPrompts();
  try { CK.anchors = { key, ...(await api(`api/check/side/${CK.side}/anchors`)) }; CK.pts = {}; CK.cur = 0; }
  catch (e) { CK.anchors = { key, prompts: [] }; $("#ck-prompts").innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  renderCkPrompts();
}
function renderCkPrompts() {
  const P = CK.anchors.prompts;
  $("#ck-prompts").innerHTML = P.map((p, i) => `<div class="${i === CK.cur ? "cur" : ""}"><span class="grow">${esc(p.ref)} <span class="dim">${esc(p.where || p.corner || "")}</span></span>${CK.pts[p.ref] ? `<span class="ok">✓</span>` : ""}</div>`).join("");
  drawCkMarks();
}
function drawCkMarks() {
  const svg = $("#ck-marks"), img = $("#ck-photo");
  if (img.hidden || !img.naturalWidth || !CK.anchors || !CK.st.check || CK.st.check.sides[CK.side].aligned) { svg.innerHTML = ""; return; }
  svg.setAttribute("viewBox", `0 0 ${img.naturalWidth} ${img.naturalHeight}`);
  const r = img.naturalWidth / 120;
  svg.innerHTML = Object.entries(CK.pts).map(([ref, [x, y]]) => `<circle class="pt" cx="${x}" cy="${y}" r="${r}" style="stroke-width:${r / 4}"/><text x="${x + r * 1.3}" y="${y - r * 1.3}" style="font-size:${r * 2}px">${esc(ref)}</text>`).join("");
}
$("#ck-photo").addEventListener("load", () => { ZC.size($("#ck-photo").naturalWidth, $("#ck-photo").naturalHeight); drawCkMarks(); });
$("#ck-video").addEventListener("loadedmetadata", () => renderCkViewer());
$("#ck-live").addEventListener("load", () => { if (!ZC.w) renderCkViewer(); });
const ckSrc = () => `check/${CK.st.check.uid}/${CK.side}`;
function ckLook(ref) {
  $("#ck-look-ref").value = ref;
  showPart($("#ck-insp"), ckSrc(), ref, async () => { CK.st = await api("api/check/state"); renderCheck(); });
}
$("#ck-look-go").addEventListener("click", () => { const r = typedRef($("#ck-look-ref").value); if (r) ckLook(r); });
$("#ck-look-ref").addEventListener("change", () => { const r = typedRef($("#ck-look-ref").value); if (r) ckLook(r); });
$("#ck-photo").addEventListener("click", async e => {
  const c = CK.st && CK.st.check;
  if (c && c.sides[CK.side].aligned) {        // pick a part on the result photo (lined up with the reference)
    if (CK.view !== "result") return say("Switch to RESULT to pick a part on the photo.", true);
    const b = e.target.getBoundingClientRect();
    try {
      const r = await post("api/part/pick", { src: ckSrc(), fx: (e.clientX - b.left) / b.width, fy: (e.clientY - b.top) / b.height });
      if (r.ref) ckLook(r.ref); else say("No part there. Click on a part's body.", true);
    } catch (err) { say(err.message, true); }
    return;
  }
  if (!c || !CK.anchors || !CK.anchors.prompts.length) return;
  const img = e.target, b = img.getBoundingClientRect(), k = img.naturalWidth / b.width;
  const xy = [(e.clientX - b.left) * k, (e.clientY - b.top) * k];
  const p = CK.anchors.prompts[CK.cur]; if (!p) return;
  try { const r = await post(`api/check/side/${CK.side}/snap`, { xy }); CK.pts[p.ref] = r.xy; } catch (err) { CK.pts[p.ref] = xy; }
  CK.cur = Math.min(CK.cur + 1, CK.anchors.prompts.length - 1);
  if (Object.keys(CK.pts).length === CK.anchors.prompts.length) CK.cur = CK.anchors.prompts.length;
  renderCkPrompts();
});
$("#ck-clear").addEventListener("click", () => { CK.pts = {}; CK.cur = 0; renderCkPrompts(); });
$("#ck-align").addEventListener("click", () => ckAct(() => post(`api/check/side/${CK.side}/align`, { points: CK.pts }), "Lined up. Look at each flagged part."));
$("#ck-sides").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { CK.side = b.dataset.side; CK.view = "result"; renderCheck(); } });
$("#ck-view").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { CK.view = b.dataset.v; renderCkViewer(); } });
const ckNote = ref => { const i = $$("#ck-flagged [data-note]").find(x => x.dataset.note === ref); return i ? i.value : ""; };
const ckReason = ref => { const i = $$("#ck-flagged [data-reason]").find(x => x.dataset.reason === ref); return i ? i.value : ""; };
$("#ck-flagged").addEventListener("click", e => {
  const cv = e.target.closest("[data-cov]");
  if (cv) return ckAct(() => post(`api/check/side/${CK.side}/covered`, { ref: cv.dataset.ref, on: true, reason: cv.dataset.cov || "heatsink" }),
    `${cv.dataset.ref}: covered (${cv.dataset.cov || "heatsink"}) on this board. Not a fault; still checked on every other board. Undo it under COVERED.`);
  const n = e.target.closest("[data-ns]");
  if (n) return ckAct(() => post(`api/check/side/${CK.side}/ns`, { ref: n.dataset.ref, on: true, all: n.dataset.ns === "all" }),
    `${n.dataset.ref}: not stuffed ${n.dataset.ns === "all" ? "on every board of this design" : "on this board"}. Undo it under NOT STUFFED.`);
  const b = e.target.closest("[data-ans]"); if (!b) return;
  const cur = (CK.st.check.sides[CK.side].results || []).find(r => r.ref === b.dataset.ref);
  const missing = b.dataset.ans === "missing", was = cur && cur.answer === "wrong" ? (cur.reason === "missing" ? "missing" : "wrong") : cur && cur.answer;
  const ans = was === b.dataset.ans ? null : missing ? "wrong" : b.dataset.ans;
  ckAct(() => post(`api/check/side/${CK.side}/answer`, { ref: b.dataset.ref, answer: ans, note: ckNote(b.dataset.ref), reason: missing ? "missing" : ckReason(b.dataset.ref), missed: !!(cur && cur.missed) }),
    ans ? `Learned: ${b.dataset.ref} kept as a ${ans === "correct" ? "good" : missing ? "missing (bad)" : "bad"} example.` : `${b.dataset.ref}: answer cleared and its example forgotten.`);
});
$("#ck-flagged").addEventListener("change", e => {
  const i = e.target.closest("[data-note], [data-reason]"); if (!i) return;
  const ref = i.dataset.note || i.dataset.reason;
  const cur = (CK.st.check.sides[CK.side].results || []).find(r => r.ref === ref);
  if (cur && cur.answer) ckAct(() => post(`api/check/side/${CK.side}/answer`, { ref, answer: cur.answer, note: ckNote(ref), reason: ckReason(ref), missed: !!cur.missed }), "Saved with the example.");
});
$("#ck-ok").addEventListener("click", e => {
  const uc = e.target.closest("[data-uncov]");
  if (uc) return ckAct(() => post(`api/check/side/${CK.side}/covered`, { ref: uc.dataset.uncov, on: false }), `${uc.dataset.uncov}: COVERED undone, it's checked again.`);
  const u = e.target.closest("[data-unns]");
  if (u) return ckAct(() => post(`api/check/side/${CK.side}/ns`, { ref: u.dataset.unns, on: false }), `${u.dataset.unns}: NS undone, it's checked again.`);
  if (!e.target.closest("#ck-miss")) return;
  const ref = $("#ck-miss-ref").value;
  ckAct(() => post(`api/check/side/${CK.side}/answer`, { ref, answer: "wrong", note: $("#ck-miss-note").value, reason: $("#ck-miss-reason").value, missed: true }),
    `${ref} flagged as wrong. The check will look for this next time.`);
});
$("#ck-learned").addEventListener("click", () => { const c = CK.st && CK.st.check; setHistKind("learned", false); H.sel = c ? c.board : null; H.lside = CK.side; goTab("history"); });
$("#ck-go").addEventListener("click", () => ckAct(() => post("api/check/start", { reference: $("#ck-ref").value, serial: $("#ck-serial").value, notes: $("#ck-notes").value }),
  "Check started. Take a photo of the board, lined up with the ghost.").then(() => { CK.side = Object.keys(CK.st.check ? CK.st.check.sides : { front: 1 })[0]; $("#ck-serial").value = ""; $("#ck-notes").value = ""; renderCheck(); }));
$("#ck-new").addEventListener("click", () => { stopCk(); ckAct(() => post("api/check/close", {})); });
$("#ck-ghost-on").addEventListener("change", e => { CK.ghostOn = e.target.checked; renderCkViewer(); });
async function startCk() {
  let want = null; try { want = localStorage.getItem("aoimap-ck-camera"); } catch (e) { }
  try {
    CK.stream = await openCamera(want, $("#ck-video"), $("#ck-live"), $("#ck-cam-list"));
    say("Line the board up with the ghost, then take the photo.");
  } catch (e) { CK.stream = null; camFailed(e); }
  renderCkViewer();
}
$("#ck-cam-start").addEventListener("click", () => { if (CK.stream) { stopCk(); renderCkViewer(); } else startCk(); });
$("#ck-cam-list").addEventListener("change", e => { try { localStorage.setItem("aoimap-ck-camera", e.target.value); } catch (x) { } stopCk(); startCk(); });
function sendCk(blob) {
  say("Lining up and checking every part...");
  CK.view = "result"; CK.anchors = null;
  ckAct(() => upload(`api/check/side/${CK.side}/photo`, blob)).then(() => {
    const s = CK.st.check && CK.st.check.sides[CK.side];
    if (s && s.aligned) say(s.message); else if (s) say(s.message + " Click the fiducials listed, in order.", true);
  });
}
$("#ck-cam-take").addEventListener("click", async () => {
  try { const b = await grab(CK.stream, $("#ck-video")); stopCk(); sendCk(b); } catch (e) { say(e.message, true); }
});
$("#ck-file").addEventListener("change", e => { const f = e.target.files[0]; e.target.value = ""; if (f) sendCk(f); });

// ---- start --------------------------------------------------------------------------------
loadGroups();
act(() => api("api/state")).then(() => { if (S && S.design) say(""); });
window.addEventListener("resize", () => (tab === "front" || tab === "back") && drawMarks(tab));
