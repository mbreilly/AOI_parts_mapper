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
  tab = b.dataset.tab; ui.closeup = null; stopCamera(); say("");
  render();
  if (tab === "front" || tab === "back") loadAnchors(tab);
});

function render() {
  $$("#tabs button").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === tab);
    b.disabled = b.dataset.tab !== "files" && !(S && S.design);
  });
  $("#tab-files").hidden = tab !== "files";
  $("#tab-parts").hidden = tab !== "parts";
  $("#tab-side").hidden = !(tab === "front" || tab === "back");
  $("#tab-map").hidden = tab !== "map";
  if (S && S.design) $("#sub").textContent = `${S.design.name}  ·  ${S.design.units}  ·  ${countAll()} polarised parts`;
  if (tab === "files") renderFiles();
  if (tab === "parts") renderParts();
  if (tab === "front" || tab === "back") renderSide(tab);
  if (tab === "map") renderMap();
}
const countAll = () => S.design.parts.length;

// ---- 1 files -------------------------------------------------------------------------------
function setupDrop(id, kind) {
  const lab = $(`#drop-${kind}`), input = $(`#file-${kind}`);
  const send = f => act(() => upload(`/api/upload?kind=${kind}&name=${encodeURIComponent(f.name)}`, f), (S && S.messages && S.messages.join(" ")) || "");
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
  $("#boards").innerHTML = S.boards.length ? S.boards.map(b => `<div><span class="grow">${esc(b.name)}</span><span class="dim">${b.parts} parts${b.sides.length ? " · photos: " + b.sides.join(", ") : ""}</span><button data-open="${esc(b.name)}">OPEN</button></div>`).join("")
    : `<div class="dim">None yet. A board is saved as soon as its ODB++ and BOM are in.</div>`;
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
$("#boards").addEventListener("click", e => { const b = e.target.closest("[data-open]"); if (b) act(() => post("/api/open", { name: b.dataset.open }), `Opened ${b.dataset.open}.`); });
$("#step").addEventListener("change", e => act(() => post("/api/option", { key: "step", value: e.target.value })));
$("#columns").addEventListener("change", e => {
  const cols = {}; $$("#columns select").forEach(s => cols[s.dataset.col] = s.value === "" ? null : +s.value);
  act(() => post("/api/columns", cols), "BOM columns updated.");
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
  if (!minpxKnob) minpxKnob = knob($("#knob-minpx"), S.profile.min_px, (v, end) => { if (end) act(() => post("/api/option", { key: "min_px", value: v })); });
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
  act(() => post("/api/lines", { [key]: ch }));
});
$("#markings").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return;
  act(() => post("/api/option", { key: "marking", value: { kind: b.closest("[data-mark]").dataset.mark, end: b.dataset.end } })); });
$("#rot-sign").addEventListener("click", e => { const b = e.target.closest("button"); if (b) act(() => post("/api/option", { key: "rot_sign", value: b.dataset.v })); });
$("#use-tant").addEventListener("change", e => act(() => post("/api/option", { key: "use_tantalums", value: e.target.checked })));

// ---- 3/4 a side: photo, fiducials, closeups ------------------------------------------------
let ghostKnob = null, zoomKnob = null;
async function loadAnchors(side) {
  try { ui.anchors[side] = await api(`/api/side/${side}/anchors`); } catch (e) { say(e.message, true); }
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
  $("#video").hidden = !live;
  const img = $("#photo"), ghost = $("#ghost");
  img.hidden = live || !hasPhoto;
  $("#placeholder").hidden = live || hasPhoto;
  const t = Date.now();
  if (hasPhoto && !live) {
    const aligned = cu ? cst.aligned : st.aligned;
    const base = cu ? `/api/side/${side}/closeup/${cu.index}` : `/api/side/${side}`;
    const src = aligned ? `${base}/map.jpg?w=1800&marks=${ui.marks ? 1 : 0}${ui.highlight && !cu ? "&highlight=" + encodeURIComponent(ui.highlight) : ""}` : `${base}/photo.jpg`;
    const key = src + "|" + (S._v || "") + "|" + (S._r || "");
    if (img.dataset.key !== key) { img.dataset.key = key; img.src = src + (src.includes("?") ? "&" : "?") + "t=" + t; }
  }
  // ghost over the live camera: the last photo of this side, or the design drawn as it should lie
  ghost.hidden = !(live && ui.ghostOn);
  if (live && ui.ghostOn) {
    const gsrc = cu ? "" : (st.photo ? `/api/side/${side}/photo.jpg` : `/api/side/${side}/ghost.svg?o=${o.join(",")}`);
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
  for (const el of [$("#photo"), $("#video")]) el.style.width = (100 * z) + "%";
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
    const url = cu ? `/api/side/${side}/closeup/${cu.index}/snap` : `/api/side/${side}/snap`;
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
    const r = await post(`/api/side/${side}/auto`, {});
    if (!r.points) return say("The fiducials couldn't be found automatically. Click each one.", true);
    ui.pts[side] = r.points; ui.cur[side] = nextPrompt(side);
    say("Found the fiducials. Check each mark sits on its pad, then press Line up.");
    renderSide(side);
  } catch (e) { say(e.message, true); }
});
$("#do-align").addEventListener("click", () => {
  const side = tab, cu = ui.closeup;
  if (cu) return act(() => post(`/api/side/${side}/closeup/${cu.index}/align`, { points: cu.pts }), "Closeup lined up.");
  act(async () => { const s = await post(`/api/side/${side}/align`, { points: ui.pts[side] }); S = s; await loadRotation(side); return s; },
    "Lined up. Coloured outlines can be checked; grey ones can't (see the list).");
});
async function loadRotation(side) {
  try { ui.rot[side] = await api(`/api/side/${side}/rotation`); } catch (e) { ui.rot[side] = null; }
  if (tab === side) renderSide(side);
}
$("#rot-crops").addEventListener("click", e => { const b = e.target.closest("[data-sign]"); if (b) act(() => post("/api/option", { key: "rot_sign", value: +b.dataset.sign }), "Rotation direction set."); });
$("#orient").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  const side = tab, v = b.dataset.o.split(",").map(Number);
  act(async () => { const s = await post(`/api/side/${side}/orient`, { orient: v }); S = s; await loadAnchors(side); return S; });
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
async function startCamera() {
  try {
    const want = S.profile.camera;
    ui.stream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: want ? { ideal: want } : undefined, width: { ideal: 4096 }, height: { ideal: 3072 } }, audio: false });
    $("#video").srcObject = ui.stream;
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput");
    const now = ui.stream.getVideoTracks()[0].getSettings().deviceId;
    $("#cam-list").innerHTML = devs.map((d, i) => `<option value="${d.deviceId}" ${d.deviceId === now ? "selected" : ""}>${esc(d.label || "Camera " + (i + 1))}</option>`).join("");
    say("Line the board up with the ghost, then take the photo.");
  } catch (e) {
    ui.stream = null;
    say(location.hostname === "localhost" || location.hostname === "127.0.0.1" ? `The camera couldn't start: ${e.message}` : "The camera only works when this page is opened at http://localhost.", true);
  }
  renderSide(tab);
}
function stopCamera() { if (ui.stream) { ui.stream.getTracks().forEach(t => t.stop()); ui.stream = null; } }
$("#cam-start").addEventListener("click", () => { if (ui.stream) { stopCamera(); renderSide(tab); } else startCamera(); });
$("#cam-list").addEventListener("change", e => { stopCamera(); post("/api/option", { key: "camera", value: e.target.value }).then(s => { S = s; startCamera(); }); });
$("#cam-take").addEventListener("click", () => {
  const v = $("#video"), c = document.createElement("canvas");
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext("2d").drawImage(v, 0, 0);
  c.toBlob(b => { stopCamera(); sendPhoto(b); }, "image/jpeg", 0.95);
});
$("#photo-file").addEventListener("change", e => { const f = e.target.files[0]; e.target.value = ""; if (f) sendPhoto(f); });
function sendPhoto(blob) {
  const side = tab, cu = ui.closeup;
  const url = cu ? `/api/side/${side}/closeup/${cu.index}/photo` : `/api/side/${side}/photo`;
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
  $("#map-folder").textContent = `Everything for this board is also saved in the app's data folder under boards/${S.name}/ (design.json, parts_map.csv, and each side's reference.jpg, alignment.json, parts.json and map.jpg).`;
  $("#unchecked tbody").innerHTML = rows.map(r => `<tr><td>${r.side}</td><td>${esc(r.ref)}</td><td>${KIND_NAMES[r.kind]}</td><td>${esc(r.value)}</td><td>${esc(r.reason)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="no">Nothing yet. Line up a photo of each side first.</td></tr>`;
}

// ---- start --------------------------------------------------------------------------------
act(() => api("/api/state")).then(() => { if (S && S.design) say(""); });
window.addEventListener("resize", () => (tab === "front" || tab === "back") && drawMarks(tab));
