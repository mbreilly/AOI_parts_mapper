"use strict";
// AOI parts mapper page. The server keeps all state; this page shows it and sends the user's clicks.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let S = null;                 // server state
let tab = "files";
const ui = {
  lineFilter: "pol", lineSearch: "", partsFilter: "all", highlight: null, marks: true,
  pts: { front: {}, back: {} }, cur: { front: 0, back: 0 },
  anchors: { front: null, back: null },
  closeup: null,              // {side, index, pts, cur} while working on a closeup
  ghostOn: true, ghostOp: 35, zoom: 100, stream: null, rot: {},
};
try { Object.assign(ui, JSON.parse(localStorage.getItem("aoimap-ui") || "{}"), { pts: { front: {}, back: {} }, closeup: null, stream: null, anchors: { front: null, back: null }, rot: {} }); } catch (e) { /* private window */ }
const keepUi = () => { try { localStorage.setItem("aoimap-ui", JSON.stringify({ lineFilter: ui.lineFilter, partsFilter: ui.partsFilter, ghostOn: ui.ghostOn, ghostOp: ui.ghostOp, zoom: ui.zoom, marks: ui.marks })); } catch (e) { } };

const KIND_NAMES = { ic: "IC", tantalum: "Tantalum", electrolytic: "Electrolytic", diode: "Diode", led: "LED", other: "Other" };
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
  m.hidden = false; m.textContent = text; m.className = "msg" + (err ? " err" : "");
}
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
}

// ---- knobs (hardware-panel style) ---------------------------------------------------------
function knob(el, value, onChange, fmt = v => v) {
  const min = +el.dataset.min, max = +el.dataset.max, step = +el.dataset.step || 1;
  el.innerHTML = `<div class="kl">${esc(el.dataset.label)}</div><svg viewBox="-50 -50 100 100"></svg><div class="kv2"></div>`;
  const svg = $("svg", el), box = $(".kv2", el);
  let v = value;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function draw() {
    const a0 = Math.PI * 1.25, a1 = Math.PI * 2.75, pos = (v - min) / (max - min), a = a0 + pos * (a1 - a0), r = 46;
    const P = (rr, aa) => [rr * Math.sin(aa), -rr * Math.cos(aa)];
    let s = "";
    for (let i = 0; i <= 10; i++) {
      const t = a0 + i / 10 * (a1 - a0), [x1, y1] = P(r * 0.92, t), [x2, y2] = P(r * (i % 5 ? 0.97 : 1), t);
      s += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${css("--dimInk")}" stroke-width="${i % 5 ? 1 : 1.6}"/>`;
    }
    const [ax, ay] = P(r * 0.84, a0), [bx, by] = P(r * 0.84, a);
    if (pos > 0.001) s += `<path d="M${ax} ${ay} A${r * 0.84} ${r * 0.84} 0 ${a - a0 > Math.PI ? 1 : 0} 1 ${bx} ${by}" fill="none" stroke="${css("--accent")}" stroke-width="2.5" stroke-linecap="round"/>`;
    const kr = r * 0.7, cap = kr * 0.78, kb = css("--knobBody");
    s += `<defs><linearGradient id="sk${el.id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="color-mix(in srgb, ${kb} 84%, white)"/><stop offset="1" stop-color="${kb}"/></linearGradient>
      <linearGradient id="cp${el.id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="color-mix(in srgb, ${kb} 90%, white)"/><stop offset="1" stop-color="color-mix(in srgb, ${kb} 40%, black)"/></linearGradient></defs>
      <circle cx="1.5" cy="3" r="${kr}" fill="black" opacity=".5"/><circle r="${kr}" fill="url(#sk${el.id})"/><circle r="${cap}" fill="url(#cp${el.id})"/>`;
    const [p1x, p1y] = P(kr * 0.25, a), [p2x, p2y] = P(kr * 0.92, a);
    s += `<line x1="${p1x}" y1="${p1y}" x2="${p2x}" y2="${p2y}" stroke="${css("--ink")}" stroke-width="2.2" stroke-linecap="round"/>`;
    svg.innerHTML = s;
    box.textContent = `${fmt(v)} ${el.dataset.unit || ""}`;
  }
  let drag = null;
  svg.addEventListener("pointerdown", e => { drag = { y: e.clientY, v }; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener("pointermove", e => {
    if (!drag) return;
    const nv = Math.min(max, Math.max(min, Math.round((drag.v + (drag.y - e.clientY) * (max - min) / 150) / step) * step));
    if (nv !== v) { v = nv; draw(); onChange(v, false); }
  });
  svg.addEventListener("pointerup", () => { if (drag) { drag = null; onChange(v, true); } });
  svg.addEventListener("wheel", e => { e.preventDefault(); v = Math.min(max, Math.max(min, v + (e.deltaY < 0 ? step : -step))); draw(); onChange(v, true); }, { passive: false });
  draw();
  return { set(nv) { v = nv; draw(); } };
}

// ---- tabs ----------------------------------------------------------------------------------
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.disabled) return;
  goTab(b.dataset.tab);
});
function goTab(t) {
  tab = t; ui.closeup = null; stopCamera(); stopCk(); say("");
  render();
  if (tab === "front" || tab === "back") loadAnchors(tab);
  if (tab === "history") loadHistory();
  if (tab === "check") loadCheck();
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-goto]"); if (!b) return;
  if (b.dataset.hist) setHistKind(b.dataset.hist, false);
  goTab(b.dataset.goto);
});

function render() {
  $$("#tabs button").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === tab);
    b.disabled = !["files", "history", "check"].includes(b.dataset.tab) && !(S && S.design);
  });
  $("#tab-files").hidden = tab !== "files";
  $("#tab-parts").hidden = tab !== "parts";
  $("#tab-side").hidden = !(tab === "front" || tab === "back");
  $("#tab-map").hidden = tab !== "map";
  $("#tab-history").hidden = tab !== "history";
  $("#tab-check").hidden = tab !== "check";
  if (S && S.design) $("#sub").textContent = `${S.design.name}${S.project && S.project.serial ? "  ·  " + S.project.serial : ""}  ·  ${S.design.units}  ·  ${countAll()} polarised parts`;
  if (tab === "files") renderFiles();
  if (tab === "parts") renderParts();
  if (tab === "front" || tab === "back") renderSide(tab);
  if (tab === "map") renderMap();
}
const countAll = () => S.design.parts.length;

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
    <b>Polarised, front</b><span>${sideLine("top")}</span>
    <b>Polarised, back</b><span>${sideLine("bot")}</span>
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
    $("#boards").innerHTML = h.projects.length ? h.projects.map(r => `<div class="${r.uid === h.current ? "cur" : ""}"><span class="grow">${esc(r.board)}${r.serial ? ` <span class="ok">${esc(r.serial)}</span>` : ""}<br><span class="dim">${esc(r.updated)} · ${sideBrief(r, "front")} · ${sideBrief(r, "back")}</span></span>${r.uid === h.current ? `<span class="dim">OPEN NOW</span>` : `<button data-reopen="${esc(r.uid)}">OPEN</button>`}</div>`).join("")
      : `<div class="dim">None yet. A project is saved as soon as its ODB++ and BOM are in.</div>`;
  } catch (e) { $("#boards").innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
}
const sideBrief = (r, side) => {
  const s = r[side], n = side === "front" ? "front" : "back";
  if (!s.parts) return `${n} none`;
  return s.aligned ? `${n} ${s.checkable}/${s.parts}` : s.photo ? `${n} photo` : `${n} –`;
};
function reopen(uid) {
  return act(() => post(`api/history/${encodeURIComponent(uid)}/open`, {}), "Reopened the project as it was left.").then(() => { recentAt = 0; if (S && S.design) goTab("files"); });
}
function newFrom(uid) {
  return act(() => post(`api/history/${encodeURIComponent(uid)}/new`, {}), "New project for another board of the same design. Its choices are carried over; take the photos.").then(() => { recentAt = 0; if (S && S.design) goTab("files"); });
}
document.addEventListener("click", e => {
  const o = e.target.closest("[data-reopen]"); if (o) reopen(o.dataset.reopen);
  const n = e.target.closest("[data-newfrom]"); if (n) newFrom(n.dataset.newfrom);
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
let minpxKnob = null;
function renderParts() {
  const lines = S.lines.filter(l => (ui.lineFilter === "all" || l.chosen_kind) &&
    (!ui.lineSearch || (l.value + " " + l.description).toLowerCase().includes(ui.lineSearch.toLowerCase())));
  $$("#line-filter button").forEach(b => b.classList.toggle("on", b.dataset.f === ui.lineFilter));
  $("#lines tbody").innerHTML = lines.map(l => `<tr data-key="${esc(l.key)}">
    <td>${esc(l.value)}</td><td>${esc(l.description)}${l.why && !l.chosen_kind ? ` <span class="dim">(${esc(l.why)})</span>` : ""}</td>
    <td title="${esc(l.refs.join(", "))}">${l.on_board.length}${l.on_board.length !== l.refs.length ? ` of ${l.refs.length}` : ""}</td>
    <td><select data-kind>${["", ...Object.keys(KIND_NAMES)].map(k => `<option value="${k}" ${(l.chosen_kind || "") === k ? "selected" : ""}>${k ? KIND_NAMES[k] : "Not polarised"}</option>`).join("")}</select></td>
    <td><input data-hidden value="${esc(l.hidden || "")}" placeholder="visible" ${l.chosen_kind ? "" : "disabled"}></td></tr>`).join("")
    || `<tr><td colspan="5" class="no">No lines match.</td></tr>`;
  const mk = S.profile.markings;
  $("#markings").innerHTML = ["tantalum", "electrolytic", "diode", "led"].map(k => `<div class="row"><span class="lab" style="width:100px">${KIND_NAMES[k].toUpperCase()}</span>
    <div class="seg" data-mark="${k}"><button data-end="pin1" class="${mk[k] === "pin1" ? "on" : ""}">AT PIN 1</button><button data-end="other" class="${mk[k] === "other" ? "on" : ""}">AT PIN 2</button></div></div>`).join("");
  const rs = S.profile.rot_sign;
  $$("#rot-sign button").forEach(b => b.classList.toggle("on", String(rs ?? "auto") === b.dataset.v));
  const votes = S.design.rotation_votes;
  $("#rot-note").textContent = rs == null
    ? (votes["1"] || votes["-1"] ? `Auto picked ${S.design.rot_sign === -1 ? "clockwise" : "anticlockwise"} from the pin 1 positions (${votes["-1"]} part(s) say clockwise, ${votes["1"]} anticlockwise).`
      : "Auto: the design's pins can't tell, so the photo step shows a check if any part would look different.")
    : "Set by hand.";
  if (!minpxKnob) minpxKnob = knob($("#knob-minpx"), S.profile.min_px, (v, end) => { if (end) act(() => post("api/option", { key: "min_px", value: v })); });
  else minpxKnob.set(S.profile.min_px);
  $("#use-tant").checked = !!S.profile.use_tantalums;
}
$("#line-filter").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { ui.lineFilter = b.dataset.f; keepUi(); renderParts(); } });
$("#line-search").addEventListener("input", e => { ui.lineSearch = e.target.value; renderParts(); });
$("#lines").addEventListener("change", e => {
  const tr = e.target.closest("tr"); if (!tr) return;
  const key = tr.dataset.key, ch = {};
  if (e.target.matches("[data-kind]")) ch.kind = e.target.value;
  if (e.target.matches("[data-hidden]")) ch.hidden = e.target.value.trim();
  act(() => post("api/lines", { [key]: ch }));
});
$("#markings").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return;
  act(() => post("api/option", { key: "marking", value: { kind: b.closest("[data-mark]").dataset.mark, end: b.dataset.end } })); });
$("#rot-sign").addEventListener("click", e => { const b = e.target.closest("button"); if (b) act(() => post("api/option", { key: "rot_sign", value: b.dataset.v })); });
$("#use-tant").addEventListener("change", e => act(() => post("api/option", { key: "use_tantalums", value: e.target.checked })));

// ---- 3/4 a side: photo, fiducials, closeups ------------------------------------------------
let ghostKnob = null, zoomKnob = null;
async function loadAnchors(side) {
  try { ui.anchors[side] = await api(`api/side/${side}/anchors`); } catch (e) { say(e.message, true); }
  const st = S.sides[side];
  ui.pts[side] = { ...(st && st.points || {}) };
  ui.cur[side] = nextPrompt(side);
  if (st && st.aligned) loadRotation(side);
  renderSide(side);
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

function renderSide(side) {
  const st = S.sides[side] || { photo: false };
  const cu = ui.closeup, cst = cu ? (st.closeups || []).find(c => c.index === cu.index) : null;
  $("#side-title").textContent = (side === "front" ? "FRONT" : "BACK") + (cu ? `  ·  CLOSEUP ${cu.index + 1}` : "");
  // orientation
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
    const src = aligned ? `${base}/map.jpg?w=1800&marks=${ui.marks ? 1 : 0}${ui.highlight && !cu ? "&highlight=" + encodeURIComponent(ui.highlight) : ""}` : `${base}/photo.jpg`;
    const key = src + "|" + (S._v || "") + "|" + (S._r || "");
    if (img.dataset.key !== key) { img.dataset.key = key; img.src = src + (src.includes("?") ? "&" : "?") + "t=" + t; }
  }
  // ghost over the live camera: the last photo of this side, or the design drawn as it should lie
  ghost.hidden = !(live && ui.ghostOn);
  if (live && ui.ghostOn) {
    const gsrc = cu ? "" : (st.photo ? `api/side/${side}/photo.jpg` : `api/side/${side}/ghost.svg?o=${o.join(",")}`);
    if (gsrc && ghost.dataset.src !== gsrc) { ghost.dataset.src = gsrc; ghost.src = gsrc + (gsrc.includes("?") ? "&" : "?") + "t=" + t; }
    ghost.hidden = !gsrc;
    ghost.style.opacity = ui.ghostOp / 100;
  }
  $(".viewer").style.setProperty("--zoom", ui.zoom);
  $("#viewer").classList.toggle("clicking", hasPhoto && !live);
  $("#cam-start").textContent = live ? "STOP CAMERA" : "START CAMERA";
  $("#cam-take").hidden = !live;
  $("#cam-list").hidden = !live || $("#cam-list").options.length < 2;
  $("#ghost-on").checked = ui.ghostOn;
  if (!ghostKnob) ghostKnob = knob($("#knob-ghost"), ui.ghostOp, v => { ui.ghostOp = v; $("#ghost").style.opacity = v / 100; keepUi(); });
  if (!zoomKnob) zoomKnob = knob($("#knob-zoom"), ui.zoom, v => { ui.zoom = v; applyZoom(); keepUi(); });
  applyZoom();
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
  const ast = cu ? cst : st;
  if (!cu && st.aligned) {
    res += `<div class="dim">Lined up: ${esc(st.how)}. ${st.errors.length > 3 ? "Largest point error " + Math.max(...st.errors).toFixed(1) + " px." : ""}</div>`;
    for (const w of st.warnings) res += `<div class="warn">${esc(w)}</div>`;
    res += `<div class="kv" style="margin-top:8px"><b>Parts on this side</b><span>${st.summary.parts}</span><b>Checkable</b><span>${st.summary.checkable}${st.summary.via_closeup ? ` (${st.summary.via_closeup} on closeups)` : ""}</span></div>`;
  }
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
    <span class="${t.aligned ? "ok" : "dim"}">${t.aligned ? "✓" : t.taken ? "line up" : ""}</span><button data-tile="${t.index}">${t.taken ? "OPEN" : "TAKE"}</button></div>`).join("");
  // parts
  $("#parts-box").hidden = cu || !st.aligned;
  if (st.aligned && !cu) {
    $$("#parts-filter button").forEach(b => b.classList.toggle("on", b.dataset.f === ui.partsFilter));
    $("#marks-on").checked = ui.marks;
    const rows = st.placed.filter(r => ui.partsFilter === "all" || (ui.partsFilter === "yes") === r.checkable);
    $("#side-parts tbody").innerHTML = rows.map(r => `<tr class="click ${r.ref === ui.highlight ? "sel" : ""}" data-ref="${esc(r.ref)}">
      <td>${esc(r.ref)}</td><td>${KIND_NAMES[r.kind] || r.kind}</td><td>${esc(r.value)}</td><td>${esc(r.pin1_dir || "?")}${r.pin1_how !== "named" ? "*" : ""}</td>
      <td>${r.px.toFixed(0)} px</td><td class="${r.checkable ? "" : "no"}" title="${esc(r.reason || r.note || "")}">${r.checkable ? (r.via !== undefined ? "closeup " + (r.via + 1) : "yes") : esc(r.reason)}</td></tr>`).join("");
  }
  drawMarks(side);
}
function applyZoom() {
  const z = ui.zoom / 100;
  for (const el of [$("#photo"), $("#video"), $("#live")]) el.style.width = (100 * z) + "%";
  $("#ghost").style.width = (100 * z) + "%";
  $("#marks").style.width = (100 * z) + "%";
}

// the clicked points drawn over the photo, in photo pixels
function drawMarks(side) {
  const svg = $("#marks"), img = $("#photo");
  const st = S.sides[side];
  const cu = ui.closeup, cst = cu ? st.closeups.find(c => c.index === cu.index) : null;
  const W = cu ? cst && cst.width : st.width, H = cu ? cst && cst.height : st.height;
  if (!W || img.hidden) { svg.innerHTML = ""; return; }
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.style.height = img.clientHeight + "px";
  const r = W / 120, pts = curPts(side);
  svg.innerHTML = Object.entries(pts).map(([ref, [x, y]]) => `<circle class="pt" cx="${x}" cy="${y}" r="${r}" stroke-width="${W / 600}"/>
    <line x1="${x - r * 1.6}" y1="${y}" x2="${x + r * 1.6}" y2="${y}" stroke="currentColor" class="pt" stroke-width="${W / 900}"/>
    <line x1="${x}" y1="${y - r * 1.6}" x2="${x}" y2="${y + r * 1.6}" class="pt" stroke-width="${W / 900}"/>
    <text x="${x + r * 1.4}" y="${y - r * 1.2}" style="font-size:${W / 70}px">${esc(ref)}</text>`).join("");
}
$("#photo").addEventListener("load", () => drawMarks(tab));

$("#photo").addEventListener("click", async e => {
  const side = tab; if (side !== "front" && side !== "back") return;
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
  act(async () => { const s = await post(`api/side/${side}/align`, { points: ui.pts[side] }); S = s; await loadRotation(side); return s; },
    "Lined up. Coloured outlines can be checked; grey ones can't (see the list).");
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
  say(c ? "" : "Frame just this area of the board, as close as the camera will focus, then take the photo or choose a file.");
  renderSide(side);
});
$("#align-result").addEventListener("click", e => { if (e.target.id === "back-full") { ui.closeup = null; stopCamera(); renderSide(tab); } });
$("#parts-filter").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { ui.partsFilter = b.dataset.f; keepUi(); renderSide(tab); } });
$("#marks-on").addEventListener("change", e => { ui.marks = e.target.checked; keepUi(); renderSide(tab); });
$("#side-parts").addEventListener("click", e => { const tr = e.target.closest("tr[data-ref]"); if (!tr) return;
  ui.highlight = ui.highlight === tr.dataset.ref ? null : tr.dataset.ref; renderSide(tab); });

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
    if (cu) { cu.pts = {}; cu.cur = 0; } else { ui.pts[side] = {}; ui.cur[side] = 0; ui.rot[side] = null; }
    return s;
  }, cu ? "Now click each point listed, on the closeup." : "Now click each fiducial, in the order listed.");
}
$("#ghost-on").addEventListener("change", e => { ui.ghostOn = e.target.checked; keepUi(); renderSide(tab); });

// ---- 5 map ---------------------------------------------------------------------------------
function renderMap() {
  const d = S.design, rows = [];
  let html = `<div class="kv">`;
  for (const side of ["front", "back"]) {
    const st = S.sides[side], n = d.parts.filter(p => p.side === SIDES[side]).length;
    html += `<b>${side === "front" ? "Front" : "Back"}</b><span>${n} polarised · ${st && st.aligned ? `${st.summary.checkable} checkable${st.summary.via_closeup ? ` (${st.summary.via_closeup} on closeups)` : ""}` : st && st.photo ? "photo not lined up yet" : "no photo yet"}</span>`;
    if (st && st.aligned) st.placed.filter(r => !r.checkable).forEach(r => rows.push({ side, ...r }));
  }
  html += `</div>`;
  $("#map-summary").innerHTML = html;
  const isRef = S.project && S.project.reference;
  $("#map-ref").textContent = isRef ? "THE REFERENCE FOR CHECKS ✓" : "USE AS THE REFERENCE FOR CHECKS";
  $("#map-ref").classList.toggle("on", !!isRef);
  $("#map-ref-note").textContent = isRef ? `Other ${S.project.board} boards are checked against this one on the Check a board tab. Press again to stop using it.`
    : "Once this board has been checked by hand, make it the reference: other boards of this type can then be checked against it on the Check a board tab.";
  $("#map-folder").textContent = `Everything for this board is kept with the project in the app's data folder under projects/${S.project ? S.project.uid : ""}/ (the ODB++ and BOM, design.json, parts_map.csv, and each side's reference.jpg, alignment.json, parts.json and map.jpg), and listed on the History tab.`;
  $("#unchecked tbody").innerHTML = rows.map(r => `<tr><td>${r.side}</td><td>${esc(r.ref)}</td><td>${KIND_NAMES[r.kind]}</td><td>${esc(r.value)}</td><td>${esc(r.reason)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="no">Nothing yet. Line up a photo of each side first.</td></tr>`;
}

$("#map-ref").addEventListener("click", async () => {
  if (!S || !S.project) return;
  try {
    const on = !S.project.reference;
    await post(`api/history/${encodeURIComponent(S.project.uid)}/reference`, { on });
    S.project.reference = on; recentAt = 0;
    say(on ? "This board is now the reference. Check other boards of this type on the Check a board tab." : "No longer the reference.");
    render();
  } catch (e) { say(e.message, true); }
});

// ---- history -------------------------------------------------------------------------------
const H = { q: "", sel: null, list: [], timer: null, kind: "projects" };
function setHistKind(k, load = true) {
  if (H.kind !== k) { H.kind = k; H.sel = null; $("#hist-detail").innerHTML = ""; }
  $$("#hist-kind button").forEach(b => b.classList.toggle("on", b.dataset.k === k));
  $("#hist-c4").textContent = k === "checks" ? "Result" : "Front";
  $("#hist-c5").textContent = k === "checks" ? "Flagged" : "Back";
  if (load) loadHistory();
}
$("#hist-kind").addEventListener("click", e => { const b = e.target.closest("button"); if (b) setHistKind(b.dataset.k); });
const kb = n => n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} kB`;
const fileUrl = (uid, path, extra = "") => `api/history/${encodeURIComponent(uid)}/file/${path.split("/").map(encodeURIComponent).join("/")}${extra}`;
async function loadHistory() {
  if (H.kind === "checks") return loadChecks();
  try {
    const h = await api(`api/history?limit=200&q=${encodeURIComponent(H.q)}`);
    H.list = h.projects;
    $("#hist-status").textContent = (h.source === "database" ? `${h.total} project(s) in the database.` : h.status) +
      (H.q ? ` ${h.projects.length} match.` : "");
    $("#hist-list tbody").innerHTML = h.projects.map(r => `<tr class="click ${r.uid === H.sel ? "sel" : ""}" data-uid="${esc(r.uid)}">
      <td>${esc(r.updated.slice(0, 16))}</td><td>${esc(r.board)}${r.uid === h.current ? ` <span class="ok">OPEN</span>` : ""}${r.reference ? ` <span class="dim">REFERENCE</span>` : ""}</td><td>${esc(r.serial)}</td>
      <td>${sideCell(r.front)}</td><td>${sideCell(r.back)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="no">${H.q ? "Nothing matches." : "No projects yet."}</td></tr>`;
    if (!H.sel && h.projects.length) showProject(h.projects[0].uid);
    else if (H.sel) showProject(H.sel);
  } catch (e) { say(e.message, true); }
}
const sideCell = s => !s.parts ? `<span class="dim">none</span>` : s.aligned ? `${s.checkable}/${s.parts}` : s.photo ? "photo" : `<span class="dim">–</span>`;
$("#hist-search").addEventListener("input", e => { H.q = e.target.value; clearTimeout(H.timer); H.timer = setTimeout(loadHistory, 300); });
$("#hist-list").addEventListener("click", e => { const tr = e.target.closest("[data-uid]"); if (tr) (H.kind === "checks" ? showCheck : showProject)(tr.dataset.uid); });

async function loadChecks() {
  try {
    const h = await api(`api/history/checks?limit=200&q=${encodeURIComponent(H.q)}`);
    $("#hist-status").textContent = (h.source === "database" ? `${h.total} check(s) in the database.` : h.status) + (H.q ? ` ${h.checks.length} match.` : "");
    const flagged = r => { const n = r.front.flagged + r.back.flagged, w = r.front.wrong.length + r.back.wrong.length; return w ? `<span class="st-wrong">${w} wrong</span>` : n ? `${n} looked at` : "none"; };
    $("#hist-list tbody").innerHTML = h.checks.map(r => `<tr class="click ${r.uid === H.sel ? "sel" : ""}" data-uid="${esc(r.uid)}">
      <td>${esc(r.updated.slice(0, 16))}</td><td>${esc(r.board)}</td><td>${esc(r.serial)}</td>
      <td><span class="badge ${esc(r.result)}">${esc(r.result)}</span></td><td>${flagged(r)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="no">${H.q ? "Nothing matches." : "No boards checked yet."}</td></tr>`;
    if (!H.sel && h.checks.length) showCheck(h.checks[0].uid);
    else if (H.sel) showCheck(H.sel);
  } catch (e) { say(e.message, true); }
}

const STATUS_TEXT = { ok: "right way round", reversed: "looks reversed", rotated: "looks turned", check: "look at it" };
const ckFile = (uid, side, name, extra = "") => `api/check/${encodeURIComponent(uid)}/file/${side}/${name}${extra}`;
const ckCrop = (uid, side, ref, which) => `api/check/${encodeURIComponent(uid)}/crop/${side}/${encodeURIComponent(ref)}?which=${which}`;
function flaggedHtml(c, side, editable) {
  const st = c.sides[side] || {}, rs = (st.results || []).filter(r => r.status !== "ok");
  return rs.map(r => `<div class="flag ${r.answer ? "answered" : ""}">
      <div><b>${esc(r.ref)}</b> ${KIND_NAMES[r.kind] || esc(r.kind)} ${esc(r.value)} · <span class="st-${r.status}">${STATUS_TEXT[r.status]}</span>
        <div class="dim">${esc(r.why || "")}</div></div>
      <div class="pics"><figure><img loading="lazy" src="${ckCrop(c.uid, side, r.ref, "reference")}" alt="">reference</figure>
        <figure><img loading="lazy" src="${ckCrop(c.uid, side, r.ref, "board")}" alt="">this board</figure></div>
      ${editable ? `<div class="row"><button data-ans="correct" data-ref="${esc(r.ref)}" class="${r.answer === "correct" ? "on" : ""}">RIGHT WAY ROUND</button>
        <button data-ans="wrong" data-ref="${esc(r.ref)}" class="${r.answer === "wrong" ? "on wrong" : ""}">WRONG</button></div>`
      : `<div>${r.answer === "wrong" ? `<span class="st-wrong">Answered: wrong</span>` : r.answer === "correct" ? "Answered: right way round" : `<span class="st-check">Not answered</span>`}</div>`}
    </div>`).join("");
}

async function showCheck(uid) {
  H.sel = uid;
  $$("#hist-list tr[data-uid]").forEach(tr => tr.classList.toggle("sel", tr.dataset.uid === uid));
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
      ${sides.map(s => { const st = c.sides[s]; const rs = st.results || []; return `<b>${s === "front" ? "Front" : "Back"}</b><span>${st.aligned ? `${rs.length} parts checked, ${rs.filter(r => r.status !== "ok").length} flagged` : st.photo ? "photo not lined up" : "not checked"}</span>`; }).join("")}
      <b>Notes</b><span>${esc(c.notes) || "–"}</span>
      <b>Folder</b><span class="mono">${esc(c.folder)}</span>
    </div>
    <h3>RESULT PHOTOS</h3>
    <div class="gallery">${sides.filter(s => c.sides[s].aligned).map(s => `<a href="${ckFile(uid, s, "result.jpg")}" target="_blank" rel="noopener"><img loading="lazy" src="${ckFile(uid, s, "result.jpg", `?thumb=360&v=${encodeURIComponent(c.updated)}`)}" alt=""><span>${s}</span></a>`).join("") || `<p class="dim">No side was lined up.</p>`}</div>
    ${sides.map(s => { const h = flaggedHtml(c, s, false); return h ? `<h3>FLAGGED ON THE ${s.toUpperCase()}</h3>${h}` : ""; }).join("")}
    <h3>LOG</h3>
    <div class="list log">${c.log.map(e => `<div><span class="dim">${esc(e.at)}</span><span class="grow">${esc(e.what)}</span></div>`).join("")}</div>`;
}
document.addEventListener("click", e => {
  const o = e.target.closest("[data-ckopen]");
  if (o) { post("api/check/open", { uid: o.dataset.ckopen }).then(st => { CK.st = st; goTab("check"); }).catch(err => say(err.message, true)); }
  const pr = e.target.closest("[data-showproj]");
  if (pr) { e.preventDefault(); setHistKind("projects", false); H.sel = pr.dataset.showproj; loadHistory(); }
});

async function showProject(uid) {
  H.sel = uid;
  $$("#hist-list tr[data-uid]").forEach(tr => tr.classList.toggle("sel", tr.dataset.uid === uid));
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
      <button id="hd-ref" class="${d.reference ? "on" : ""}">${d.reference ? "REFERENCE FOR CHECKS ✓" : "USE AS REFERENCE"}</button>
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
    ${d.unchecked.length ? `<div class="tablewrap short"><table><thead><tr><th>Side</th><th>Ref</th><th>Kind</th><th>Value</th><th>Why</th></tr></thead><tbody>
      ${d.unchecked.map(r => `<tr><td>${r.side}</td><td>${esc(r.ref)}</td><td>${KIND_NAMES[r.kind] || esc(r.kind)}</td><td>${esc(r.value)}</td><td>${esc(r.reason)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="dim">${d.front.aligned || d.back.aligned ? "Every placed part could be checked." : "No side was lined up."}</p>`}
    <h3>FILES</h3>
    <div class="tablewrap short"><table><thead><tr><th>File</th><th>What</th><th>Size</th><th>Saved</th></tr></thead><tbody>
      ${d.files.map(f => `<tr><td><a href="${fileUrl(uid, f.path, `?download=${encodeURIComponent(named(f).split("/").pop())}`)}">${esc(f.path)}</a>${named(f) !== f.path ? ` <span class="dim">${esc(named(f))}</span>` : ""}</td><td>${esc(f.kind)}${f.side ? " · " + f.side : ""}</td><td>${kb(f.bytes)}</td><td>${esc(f.modified.slice(0, 16))}</td></tr>`).join("")}</tbody></table></div>
    <h3>LOG</h3>
    <div class="list log">${d.log.map(e => `<div><span class="dim">${esc(e.at)}</span><span class="grow">${esc(e.what)}</span></div>`).join("") || `<div class="dim">Nothing logged.</div>`}</div>`;
  $("#hd-ref").addEventListener("click", async () => {
    try {
      await post(`api/history/${encodeURIComponent(uid)}/reference`, { on: !d.reference });
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
    const refs = st.references.filter(r => Object.keys(r.sides).length);
    $("#ck-ref").innerHTML = refs.map(r => `<option value="${esc(r.uid)}">${esc(r.board)} · reference ${esc(r.serial || r.uid)} (${Object.keys(r.sides).join(" and ")})</option>`).join("");
    $("#ck-noref").hidden = !!refs.length; $("#ck-go").disabled = !refs.length;
  } else {
    const sides = Object.keys(c.sides);
    if (!sides.includes(CK.side)) CK.side = sides[0];
    const s = c.sides[CK.side] || {};
    $("#ck-title").textContent = `${c.board}${c.serial ? "  ·  " + c.serial : ""}`;
    $("#ck-result").className = `badge ${c.result}`; $("#ck-result").textContent = c.result;
    const rs = Object.values(c.sides).flatMap(x => x.results || []), open = rs.filter(r => r.status !== "ok" && !r.answer).length;
    $("#ck-sub").textContent = c.result === "OPEN" ? (rs.length ? `${open} flagged part(s) to look at` : "Take a photo of the board") :
      c.result === "PASS" ? `All ${rs.length} parts checked are the right way round${sides.some(x => !c.sides[x].aligned) ? " (" + sides.filter(x => c.sides[x].aligned).join(" and ") + " only)" : ""}` :
      `Wrong: ${rs.filter(r => r.answer === "wrong").map(r => r.ref).join(", ")}`;
    $("#ck-sides").innerHTML = sides.map(x => { const y = c.sides[x]; const f = (y.results || []).filter(r => r.status !== "ok" && !r.answer).length;
      return `<button data-side="${x}" class="${x === CK.side ? "on" : ""}">${x.toUpperCase()}${y.aligned ? (f ? ` · ${f}` : " ✓") : ""}</button>`; }).join("");
    $("#ck-msg").hidden = !s.message; $("#ck-msg").textContent = s.message || "";
    $("#ck-points").hidden = !(s.photo && !s.aligned);
    if (s.photo && !s.aligned) loadCkAnchors();
    const fl = flaggedHtml(c, CK.side, true);
    $("#ck-flagged").innerHTML = s.aligned ? (fl ? `<h3>FLAGGED: LOOK AT EACH ONE</h3>${fl}` : `<p class="st-ok">Nothing flagged on the ${CK.side}.</p>`) : "";
    const ok = (s.results || []).filter(r => r.status === "ok");
    $("#ck-ok").innerHTML = s.aligned ? `<h3>RIGHT WAY ROUND (${ok.length})</h3><p class="dim">${esc(ok.map(r => r.ref).join(", ") || "none")}</p>` +
      ((s.skipped || []).length ? `<h3>NOT CHECKED HERE (${s.skipped.length})</h3><p class="dim">Look at these by eye: ${esc(s.skipped.map(r => r.ref).join(", "))}.</p>` : "") : "";
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
$("#ck-photo").addEventListener("load", drawCkMarks);
$("#ck-photo").addEventListener("click", async e => {
  const c = CK.st && CK.st.check; if (!c || c.sides[CK.side].aligned || !CK.anchors || !CK.anchors.prompts.length) return;
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
$("#ck-flagged").addEventListener("click", e => {
  const b = e.target.closest("[data-ans]"); if (!b) return;
  const cur = (CK.st.check.sides[CK.side].results || []).find(r => r.ref === b.dataset.ref);
  const ans = cur && cur.answer === b.dataset.ans ? null : b.dataset.ans;
  ckAct(() => post(`api/check/side/${CK.side}/answer`, { ref: b.dataset.ref, answer: ans }));
});
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
act(() => api("api/state")).then(() => { if (S && S.design) say(""); });
window.addEventListener("resize", () => (tab === "front" || tab === "back") && drawMarks(tab));
