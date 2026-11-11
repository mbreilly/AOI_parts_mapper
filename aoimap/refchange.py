"""Changing the reference a board design is checked against, with the tester approving every difference.

The reference is what "right" looks like for every later check, so it never changes silently. When
another board is made the reference (History > Reference for checks, or the Map tab's button), or the
reference board's own photo is replaced or lined up again, the new reference is compared with the one
in use part by part: every part in the check, and every part with an answer or a learned example, on
each side. The comparison is the board check's (the new photo lined up with the old reference, with
what has been learned), plus the trained models' opinion of both photos.

Parts that look the same switch over without a question. Each difference is a row to decide:

    accept      ACCEPT NEW: the new picture is the standard
    keep_old    KEEP OLD: the old crop is kept as a good example, so it still counts as right
    wrong       MARK NEW AS WRONG: the new reference shows the part wrong (a bad example of it, and the
                new reference's look of it isn't trusted)
    ns          NS: not stuffed on every board of the design

A part answered COVERED on the new reference (under a heatsink...) can't be the standard there: its row
starts decided KEEP OLD, so the old reference's look of it is kept as a good example and later boards go
by that. Covered on the old one only, KEEP OLD keeps nothing (that look was never the standard).

The old reference stays the one in use until every row has a decision and the change is finished
(decisions are saved as they are made, so it can be finished later). A finished change can be undone:
the old reference is put back and what the decisions added is taken away again.

When the reference board's own photo is replaced, the reference as it was is first copied to a
project of its own (so it stays in use, and in the history), and the change compares the two.

    <data>/reference_changes/<id>.json      the change: old and new reference, the rows, the decisions
"""

import json
import re
import secrets
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import check as ck
from . import projects as pj

CHOICES = {"accept": "ACCEPT NEW", "keep_old": "KEEP OLD", "wrong": "MARK NEW AS WRONG", "ns": "NS ON EVERY BOARD"}
# why a part needs a decision, in plain words
KINDS = {"different": "looks different", "turned": "turned or reversed", "missing_new": "missing on the new one",
         "missing_old": "missing on the old one", "not_seen": "can't be seen on the new one",
         "answer": "an answer given before doesn't fit the new picture", "ns": "not stuffed on one of them",
         "covered_new": "covered on the new reference", "covered_old": "covered on the old reference"}
LISTENERS = []          # called with a change id whenever one changes (the database copy follows)
_BUILDING = {}          # change id -> progress while its rows are worked out


def root(data: Path) -> Path:
    return Path(data) / "reference_changes"


def path(data: Path, cid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cid or "") or cid.startswith("."):
        raise ValueError("No such reference change.")
    return root(data) / f"{cid}.json"


def load(data: Path, cid: str) -> dict:
    c = pj._read_json(path(data, cid))
    if not c or c.get("id") != cid:
        raise ValueError("No such reference change.")
    return c


def save(data: Path, c: dict):
    c["updated"] = pj.now()
    pj._write_json(path(data, c["id"]), c)
    for fn in LISTENERS:
        fn(c["id"])


def ids(data: Path) -> list[str]:
    r = root(data)
    return sorted(f.stem for f in r.glob("*.json")) if r.is_dir() else []


def changes(data: Path, board: str | None = None) -> list[dict]:
    """Every change (of one board design), newest first, without its rows."""
    out = []
    for cid in ids(data):
        c = pj._read_json(path(data, cid), {}) or {}
        if c.get("id") == cid and (board is None or c.get("board") == board):
            out.append(brief(c))
    return sorted(out, key=lambda c: c["created"], reverse=True)


def brief(c: dict) -> dict:
    rows = c.get("rows", [])
    left = sum(1 for r in rows if r["key"] not in c.get("decisions", {}))
    return {k: c.get(k) for k in ("id", "board", "old", "new", "status", "created", "finished", "undone", "trigger",
                                  "old_label", "new_label")} | {
        "differences": len(rows), "left": left, "same_count": sum(len(v) for v in c.get("same", {}).values()),
        "building": c["id"] in _BUILDING, "progress": _BUILDING.get(c["id"])}


def open_for(data: Path, board: str) -> dict | None:
    """The change of this design still waiting for decisions, if any."""
    return next((c for c in changes(data, board) if c["status"] == "open"), None)


def _label(data, uid) -> str:
    try:
        p = pj.load(data, uid)
    except ValueError:
        return uid
    return p.get("serial") or uid


def _aligned_sides(data, uid) -> set:
    d = pj.folder(data, uid)
    return {s for s in pj.SIDES if (d / s / "reference.jpg").is_file() and
            (pj._read_json(d / s / "alignment.json", {}) or {}).get("mapping")}


# ---- starting a change, and the comparison ---------------------------------------------------

def start(data: Path, old: str, new: str, trigger: str = "switch", refresh=None) -> dict:
    """Starts a change from reference old to project new (a change of this design still open is
    replaced). refresh(uid) brings a project's placed parts up to the design's choices first. The rows
    are worked out in the background; brief() shows how far it has got."""
    po, pn = pj.load(data, old), pj.load(data, new)
    if po["board"] != pn["board"]:
        raise ValueError("Those boards are of different designs.")
    if old == new:
        raise ValueError("That board is the reference already.")
    have_old, have_new = _aligned_sides(data, old), _aligned_sides(data, new)
    if not have_new:
        raise ValueError("Line up a photo of at least one side first.")
    missing = sorted(have_old - have_new)
    if missing:
        raise ValueError(f"Line up the {' and '.join(missing)} photo of this board first: the reference in use has one, "
                         "and checks of that side need it.")
    for c in changes(data, po["board"]):
        if c["status"] == "open":
            cancel(data, c["id"], "replaced by a newer change")
    cid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    c = {"id": cid, "board": po["board"], "old": old, "new": new, "old_label": _label(data, old),
         "new_label": _label(data, new), "trigger": trigger, "status": "open", "created": pj.now(), "finished": "",
         "undone": "", "sides": sorted(have_new & (have_old or have_new)), "rows": [], "same": {}, "decisions": {},
         "applied": {}}
    if refresh:
        for uid in (old, new):
            refresh(uid)
    save(data, c)
    _BUILDING[cid] = {"done": 0, "total": 0}
    threading.Thread(target=_build_bg, args=(data, cid), name="aoimap-refchange", daemon=True).start()
    return c


def _build_bg(data, cid):
    try:
        build(data, cid)
    except Exception:   # noqa: BLE001 - shown on the page as a change with nothing to decide
        traceback.print_exc()
        c = load(data, cid)
        c["error"] = "The comparison failed; see the app's log."
        save(data, c)
    finally:
        _BUILDING.pop(cid, None)


def _refs(data, c, side) -> list[str]:
    """Every part to compare on a side: in the check on either board, answered, learned or NS."""
    out = set()
    for uid in (c["old"], c["new"]):
        out |= {r["ref"] for r in pj._read_json(pj.folder(data, uid) / side / "parts_full.json", []) or []}
    d = ck.load_examples(data, c["board"], side)
    out |= {e["ref"] for e in d["examples"]}
    out |= set(d["reference_wrong"].get(c["old"], []))
    for key in (ck.ns_key(pj.load(data, c["old"]).get("serial", ""), c["old"]), ck.ALL_BOARDS):
        out |= set(d["not_stuffed"].get(key, []))
    for uid in (c["old"], c["new"]):
        out |= set(ck.reference_covered(data, c["board"], side, uid))
    return sorted(out, key=lambda r: (re.sub(r"\d+", "", r), int((re.findall(r"\d+", r) or ["0"])[0])))


def _models(data, board, uid, side, ref):
    from . import models as md
    from . import teach as tl
    try:
        s = tl.source(data, f"project/{uid}/{side}")
        d, g = tl._geom(data, s, ref)
        return md.opinion(data, board, tl._image(s["img"]), s["mapping"], g, d["rot_sign"],
                          (tl.part_geom(d, ref) or {}).get("kind"))
    except Exception:   # noqa: BLE001 - an extra opinion
        return {}


def compare(data: Path, c: dict, side: str, ref: str) -> dict | None:
    """One part, new reference against old: None when it looks the same (no question), or the row."""
    from . import models as md
    from . import teach as tl
    src_new = f"project/{c['new']}/{side}"
    try:
        r = tl._inspect(data, src_new, ref, c["old"])
    except ValueError:
        return None         # not on this side, or not in the design
    board = c["board"]
    placed_new = next((x for x in pj._read_json(pj.folder(data, c["new"]) / side / "parts_full.json", []) or []
                       if x["ref"] == ref), None)
    placed_old = next((x for x in pj._read_json(pj.folder(data, c["old"]) / side / "parts_full.json", []) or []
                       if x["ref"] == ref), None)
    lx = ck.load_examples(data, board, side)
    old_key = ck.ns_key(pj.load(data, c["old"]).get("serial", ""), c["old"])
    new_key = ck.ns_key(pj.load(data, c["new"]).get("serial", ""), c["new"])
    ns_old, ns_new = ref in lx["not_stuffed"].get(old_key, []), ref in lx["not_stuffed"].get(new_key, [])
    if ref in lx["not_stuffed"].get(ck.ALL_BOARDS, []):
        return None          # not stuffed on any board: nothing to compare
    cov_old = ck.reference_covered(data, board, side, c["old"]).get(ref)
    cov_new = ck.reference_covered(data, board, side, c["new"]).get(ref)
    if cov_old and cov_new:
        return None          # covered on both: neither shows it, nothing to compare
    kinds, why = [], []
    if cov_new:
        kinds.append("covered_new")
        why.append(f"covered on the new reference ({cov_new}): its look there can't be the standard. "
                   "KEEP OLD keeps the old reference's look of it as a good example")
    elif cov_old:
        kinds.append("covered_old")
        why.append(f"covered on the old reference ({cov_old}); the new one shows it, so its look can be the standard")
    if ns_old != ns_new:
        kinds.append("ns")
        why.append("answered NS on the " + ("old" if ns_old else "new") + " reference only")
    if ref in lx["reference_wrong"].get(c["old"], []):
        kinds.append("answer")
        why.append("the old reference was answered BAD or MISSING for this part")
    on_old = [e for e in lx["examples"] if e["ref"] == ref and e.get("source") == f"project/{c['old']}/{side}"]
    seen_old = bool(placed_old and placed_old.get("checkable"))
    seen_new = bool(placed_new and placed_new.get("checkable"))
    if seen_old and placed_new and not seen_new and "via" not in (placed_old or {}):
        kinds.append("not_seen")
        why.append("can't be seen on the new one: " + (placed_new.get("reason") or "not in the photo"))
    v, st = r.get("verdict"), r.get("status")
    if v == "wrong":
        if st == "fault":
            kinds.append("answer")
            why.append(r["why"])
        else:
            kinds.append("turned")
            why.append(r["why"])
    elif v == "unsure" and not ns_new and not ns_old and ("doesn't look like" in r["why"] or "shows this part wrong" in r["why"]):
        # (a part that looks the same but shows no polarity mark either way isn't a difference)
        kinds.append("different")
        why.append(r["why"])
    m_old, m_new = _models(data, board, c["old"], side, ref), _models(data, board, c["new"], side, ref)
    p_old, p_new = (m_old.get("missing") or {}).get("p"), (m_new.get("missing") or {}).get("p")
    if p_old is not None and p_new is not None and abs(p_old - p_new) >= 0.5:
        k = "missing_new" if p_new < 0.5 else "missing_old"
        kinds.append(k)
        why.append(f"the models think it's {'not there' if p_new < 0.5 else 'there'} on the new one "
                   f"and {'not there' if p_old < 0.5 else 'there'} on the old one")
    if on_old and v in ("wrong", "unsure") and "answer" not in kinds:
        last = on_old[-1]
        kinds.append("answer")
        why.append(f"answered {'GOOD' if last['label'] == 'good' else 'BAD'} on the old reference, and the new one "
                   + ("looks different" if v == "unsure" else "looks wrong"))
    if not kinds:
        return None
    kinds = list(dict.fromkeys(kinds))
    both_there = (p_old is None or p_old >= 0.5) and (p_new is None or p_new >= 0.5)
    pol = lambda m: (m.get("polarity") or {}).get("p")
    same_way = pol(m_old) is None or pol(m_new) is None or (pol(m_old) >= 0.5) == (pol(m_new) >= 0.5)
    return {"key": f"{side}/{ref}", "side": side, "ref": ref, "kind": r.get("kind"), "value": r.get("value", ""),
            "kinds": kinds, "why": "; ".join(dict.fromkeys(why)), "verdict": v,
            "model_old": md.summary(m_old) if m_old else "", "model_new": md.summary(m_new) if m_new else "",
            # "matches": only a look difference, and the models see it there and the same way round on both
            "likely_same": set(kinds) <= {"different"} and both_there and same_way and bool(m_old and m_new)}


def build(data: Path, cid: str):
    c = load(data, cid)
    todo = [(s, ref) for s in c["sides"] for ref in _refs(data, c, s)]
    prog = _BUILDING.setdefault(cid, {"done": 0, "total": 0})
    prog["total"] = len(todo)
    rows, same = [], {s: [] for s in c["sides"]}
    for i, (side, ref) in enumerate(todo):
        row = compare(data, c, side, ref)
        if row:
            rows.append(row)
        else:
            same[side].append(ref)
        prog["done"] = i + 1
        time.sleep(0.01)            # give the page's own requests a turn
    c = load(data, cid)
    c["rows"], c["same"] = rows, same
    for r in rows:          # covered on the new reference: KEEP OLD unless the tester says otherwise
        if "covered_new" in r["kinds"] and r["key"] not in c["decisions"]:
            c["decisions"][r["key"]] = {"choice": "keep_old", "at": pj.now(), "default": True}
    save(data, c)


def view(data: Path, cid: str) -> dict:
    c = load(data, cid)
    return {**c, **brief(c), "choices": CHOICES, "kinds_text": KINDS}


# ---- deciding -----------------------------------------------------------------------------------

def decide(data: Path, cid: str, keys: list[str], choice: str | None) -> dict:
    """Records a decision for rows (choice None takes it back). Saved straight away."""
    c = load(data, cid)
    if c["status"] != "open":
        raise ValueError("That change is finished; undo it to change its decisions.")
    if choice is not None and choice not in CHOICES:
        raise ValueError("Unknown choice.")
    known = {r["key"] for r in c["rows"]}
    for k in keys:
        if k not in known:
            raise ValueError(f"{k} isn't in this change.")
        if choice:
            c["decisions"][k] = {"choice": choice, "at": pj.now()}
        else:
            c["decisions"].pop(k, None)
    save(data, c)
    return view(data, cid)


def approve_matching(data: Path, cid: str) -> dict:
    """ACCEPT NEW for every row not decided yet whose only difference is its look and that the models
    see there and the same way round on both."""
    c = load(data, cid)
    keys = [r["key"] for r in c["rows"] if r.get("likely_same") and r["key"] not in c["decisions"]]
    return decide(data, cid, keys, "accept") if keys else view(data, cid)


def _log(data, uid, what):
    try:
        p = pj.load(data, uid)
    except ValueError:
        return
    pj.log(data, p, what)


def _set_reference(data, uid, on: bool):
    p = pj.load(data, uid)
    p["reference"] = on
    if on:
        p["reference_at"] = pj.now()
    pj.save(data, p)


def finish(data: Path, cid: str, on_project=None) -> dict:
    """Makes the new board the reference once every difference has a decision, and applies them:
    KEEP OLD keeps the old crop as a good example, MARK NEW AS WRONG a bad example of the new one (and
    the new reference isn't trusted for it), NS marks it not stuffed on every board. on_project(uid) is
    called for each project changed."""
    from . import teach as tl
    c = load(data, cid)
    if c["status"] != "open":
        raise ValueError("That change is already finished.")
    if c["id"] in _BUILDING:
        raise ValueError("Still comparing the parts; wait a moment.")
    left = [r["ref"] for r in c["rows"] if r["key"] not in c["decisions"]]
    if left:
        raise ValueError(f"{len(left)} part(s) still need a decision: {', '.join(left[:10])}{' ...' if len(left) > 10 else ''}.")
    board = c["board"]
    _set_reference(data, c["new"], True)
    _set_reference(data, c["old"], False)
    applied = {"examples": [], "reference_wrong": [], "ns": []}
    note = f"reference change {c['id']}"
    for r in c["rows"]:
        side, ref, ch = r["side"], r["ref"], c["decisions"][r["key"]]["choice"]
        try:
            if ch == "keep_old" and "covered_old" in r["kinds"]:
                pass            # the old look was of it covered: nothing to keep
            elif ch == "keep_old":
                ex = tl.add(data, f"project/{c['old']}/{side}", ref, "good", note=f"kept from the old reference ({note})")
                applied["examples"].append([side, ex])
            elif ch == "wrong":
                ex = tl.add(data, f"project/{c['new']}/{side}", ref, "bad", "other", f"the new reference shows it wrong ({note})")
                applied["examples"].append([side, ex])
                if ref not in ck.load_examples(data, board, side)["reference_wrong"].get(c["new"], []):
                    ck.set_reference_wrong(data, board, side, c["new"], ref, True)
                    applied["reference_wrong"].append([side, ref])
            elif ch == "ns":
                if ref not in ck.load_examples(data, board, side)["not_stuffed"].get(ck.ALL_BOARDS, []):
                    ck.set_not_stuffed(data, board, side, ck.ALL_BOARDS, ref)
                    applied["ns"].append([side, ref])
        except ValueError as e:      # the part couldn't be cut from a photo: the decision is kept, nothing added
            r["note"] = str(e)
    count = {k: sum(1 for d in c["decisions"].values() if d["choice"] == k) for k in CHOICES}
    same = sum(len(v) for v in c["same"].values())
    said = (f"Reference for checks changed from {c['old_label']} ({c['old']}) to {c['new_label']} ({c['new']}): "
            f"{same} part(s) the same, switched over; {len(c['rows'])} difference(s) decided: "
            + ", ".join(f"{n} {CHOICES[k].lower()}" for k, n in count.items() if n))
    c.update(status="done", finished=pj.now(), applied=applied, summary=said)
    save(data, c)
    for uid in (c["old"], c["new"]):
        _log(data, uid, said)
        if on_project:
            on_project(uid)
    return view(data, cid)


def cancel(data: Path, cid: str, why: str = "") -> dict:
    c = load(data, cid)
    if c["status"] != "open":
        raise ValueError("Only a change still being reviewed can be cancelled.")
    c.update(status="cancelled", finished=pj.now(), summary=f"Cancelled{': ' + why if why else ''}; the old reference stays.")
    save(data, c)
    _log(data, c["new"], f"Reference change {cid} cancelled{': ' + why if why else ''}; {c['old_label']} stays the reference")
    return view(data, cid)


def undo(data: Path, cid: str, on_project=None) -> dict:
    """Puts the old reference back, and takes away what the decisions added."""
    c = load(data, cid)
    if c["status"] != "done":
        raise ValueError("Only a finished change can be undone.")
    later = [x for x in changes(data, c["board"]) if x["status"] == "done" and x["finished"] > c["finished"]]
    if later:
        raise ValueError("A later reference change was made since; undo that one first.")
    pj.load(data, c["old"])          # the old reference must still be there
    board = c["board"]
    for side, ex in c["applied"].get("examples", []):
        ck.delete_example(data, board, side, ex)
    for side, ref in c["applied"].get("reference_wrong", []):
        ck.set_reference_wrong(data, board, side, c["new"], ref, False)
    for side, ref in c["applied"].get("ns", []):
        ck.set_not_stuffed(data, board, side, ck.ALL_BOARDS, ref, False)
    _set_reference(data, c["new"], False)
    _set_reference(data, c["old"], True)
    c.update(status="undone", undone=pj.now())
    save(data, c)
    said = f"Reference change {cid} undone: {c['old_label']} ({c['old']}) is the reference for checks again"
    for uid in (c["old"], c["new"]):
        _log(data, uid, said)
        if on_project:
            on_project(uid)
    return view(data, cid)


# ---- the reference board's own photo replaced ----------------------------------------------------

def keep_old_reference(data: Path, uid: str, side: str) -> dict:
    """Before the reference board's photo of a side is replaced or lined up again: the reference as it
    is now is copied to a project of its own, which stays the reference for checks until the change is
    reviewed; the board itself stops being the reference. Returns the copy's project.json."""
    p = pj.load(data, uid)
    snap = pj.new(data, p["board"])
    src, dst = pj.folder(data, uid), pj.folder(data, snap["uid"])
    for item in src.iterdir():
        if item.name in ("project.json", "thumbs"):
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, dst / item.name)
    for d in dst.glob("*/thumbs"):
        shutil.rmtree(d, ignore_errors=True)
    snap.update({k: p.get(k, "") for k in ("serial", "odb_file", "bom_file")})
    snap.update(reference=True, reference_at=p.get("reference_at") or pj.now(), copy_of=uid,
                notes=f"The reference {p.get('serial') or uid} as it was before its {side} photo was changed on {pj.now()[:16]}. "
                      "It stays the reference for checks until the change is reviewed.")
    snap["log"] = [{"at": pj.now(), "what": f"Copied from {uid} before its {side} photo was changed, to stay the reference "
                                            "until the change is reviewed"}]
    pj.save(data, snap)
    for s in pj.SIDES:          # what's been said about the reference's look follows the copy
        d = ck.load_examples(data, p["board"], s)
        for r in d["reference_wrong"].get(uid, []):
            ck.set_reference_wrong(data, p["board"], s, snap["uid"], r, True)
        if not (p.get("serial") or "").strip():     # COVERED goes by serial; with none, by the project
            for r, why in d["covered"].get(ck.ns_key("", uid), {}).items():
                ck.set_covered(data, p["board"], s, ck.ns_key("", snap["uid"]), r, why)
    p["reference"] = False
    p["replaced_reference"] = snap["uid"]
    pj.save(data, p)
    pj.log(data, p, f"{side.capitalize()} photo changed while this board was the reference: the reference as it was is kept as "
                    f"{snap['uid']} and stays in use until the change is reviewed (Map tab or History)")
    return snap
