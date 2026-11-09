"""Looking at any one part on any photo of a board, and teaching the check what it should look like.

A photo is named by a source id:

    project/<uid>/<side>        a mapped board's photo (reference.jpg, lined up with the design)
    check/<uid>/<side>          a checked board's photo, lined up with its reference (aligned.jpg)

Every part in the ODB++ can be looked at, not only the polarised ones the BOM names: the part's area
is cut from the photo, turned and scaled into the reference photo's frame (so an example from any
photo lines up with the parts later checks compare), compared with the reference as the board check
does, and shown with what the app makes of it. The tester's answer, GOOD or BAD with a reason, is
kept as a learned example of the part (see check.py), and a part the check skipped (too small, or
not in the check at all) is checked on later boards once it has been taught.
"""

import json
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np

from . import bom as bomlib
from . import check as ck
from . import design as designlib
from . import odb as odblib
from . import photo as ph
from . import projects as pj

_IMAGES = OrderedDict()       # (path, mtime) -> image; the last few full photos read
_DESIGNS = {}                 # uid -> (mtime, design with components)
VERDICTS = {"ok": "correct", "reversed": "wrong", "rotated": "wrong", "fault": "wrong", "check": "unsure"}


def _image(path: Path):
    key = (str(path), path.stat().st_mtime if path.is_file() else 0)
    if key not in _IMAGES:
        img = cv2.imread(str(path))
        if img is None:
            raise ValueError("The photo is missing.")
        _IMAGES[key] = img
        while len(_IMAGES) > 3:
            _IMAGES.popitem(last=False)
    _IMAGES.move_to_end(key)
    return _IMAGES[key]


def design_for(data: Path, uid: str) -> dict:
    """A project's design.json with "components" (every ODB++ part, with where it is), worked out
    from the project's ODB++ and BOM for projects saved before design.json kept them."""
    f = pj.folder(data, uid) / "design.json"
    mt = f.stat().st_mtime if f.is_file() else 0
    if uid in _DESIGNS and _DESIGNS[uid][0] == mt:
        return _DESIGNS[uid][1]
    d = pj._read_json(f)
    if not d:
        raise ValueError("That project has no design yet.")
    if "components" not in d:
        src = pj.folder(data, uid) / "source"
        odbs, boms = sorted(src.glob("odb.*")), sorted(src.glob("bom.*"))
        if not (odbs and boms):
            raise ValueError("That project has no saved ODB++ and BOM.")
        prof = pj._read_json(pj.folder(data, uid) / "profile.json", {}) or {}
        odb, _ = odblib.load(odbs[0], prof.get("step"))
        d["components"] = designlib.components(odb, bomlib.read_bom(boms[0], prof.get("columns")), d)
    _DESIGNS[uid] = (mt, d)
    return d


def part_geom(design: dict, ref: str) -> dict | None:
    return next((p for p in design["parts"] if p["ref"] == ref), None) or \
        next((p for p in design.get("components", []) if p["ref"] == ref), None)


def _mapping(data, uid, side):
    a = pj._read_json(pj.folder(data, uid) / side / "alignment.json", {}) or {}
    if not a.get("mapping"):
        raise ValueError(f"The {side} photo of {uid} isn't lined up.")
    return ph.Mapping.from_dict(a["mapping"])


def newest_reference(data: Path, board: str, side: str) -> str | None:
    return next((r["uid"] for r in ck.references(data) if r["board"] == board and side in r["sides"]), None)


def source(data: Path, src: str) -> dict:
    """What a source id names: the photo, its board, the mapping from the board to it, the project
    whose design it uses and the reference whose frame examples are kept in."""
    bits = (src or "").split("/")
    if len(bits) != 3 or bits[0] not in ("project", "check") or bits[2] not in pj.SIDES:
        raise ValueError("No such photo.")
    kind, uid, side = bits
    if kind == "project":
        p = pj.load(data, uid)
        img = pj.folder(data, uid) / side / "reference.jpg"
        out = {"board": p["board"], "design": uid, "mapping": _mapping(data, uid, side), "serial": p.get("serial", ""),
               "check": "", "reference": newest_reference(data, p["board"], side), "is_reference": False}
        out["is_reference"] = out["reference"] == uid
        label = ("Reference " if p.get("reference") else "Mapped board ") + (p.get("serial") or uid)
    else:
        c = ck.load(data, uid)
        if not c["sides"].get(side, {}).get("aligned"):
            raise ValueError(f"The {side} of that check wasn't lined up.")
        img = ck.folder(data, uid) / side / "aligned.jpg"
        out = {"board": c["board"], "design": c["reference"], "mapping": _mapping(data, c["reference"], side),
               "serial": c.get("serial", ""), "check": uid, "reference": c["reference"], "is_reference": False}
        label = f"Check {c.get('serial') or uid} ({c.get('result', '')})"
    if not img.is_file():
        raise ValueError("The photo is missing.")
    out.update(id=src, kind=kind, uid=uid, side=side, img=img, label=f"{label} · {side}")
    return out


def sources(data: Path, board: str, side: str) -> list[dict]:
    """Every lined-up photo of this side of a board design, references first, then newest first."""
    out = []
    for uid in pj.uids(data):
        p = pj._read_json(pj.folder(data, uid) / "project.json", {}) or {}
        d = pj.folder(data, uid) / side
        if p.get("board") == board and (d / "reference.jpg").is_file() and (d / "alignment.json").is_file():
            out.append({"id": f"project/{uid}/{side}", "date": p.get("updated", ""), "reference": bool(p.get("reference")),
                        "label": ("Reference " if p.get("reference") else "Mapped board ") + (p.get("serial") or uid)})
    for uid in ck.uids(data):
        c = pj._read_json(ck.folder(data, uid) / "check.json", {}) or {}
        if c.get("board") == board and c.get("sides", {}).get(side, {}).get("aligned") and \
                (ck.folder(data, uid) / side / "aligned.jpg").is_file():
            out.append({"id": f"check/{uid}/{side}", "date": c.get("updated", ""), "reference": False,
                        "label": f"Check {c.get('serial') or uid} ({c.get('result', '')})"})
    out.sort(key=lambda r: r["date"], reverse=True)
    out.sort(key=lambda r: not r["reference"])
    return out


def _geom(data, s, ref):
    d = design_for(data, s["design"])
    g = part_geom(d, ref)
    if not g:
        raise ValueError(f"{ref} isn't in this board's design.")
    if g["side"] != pj.LAYER[s["side"]]:
        raise ValueError(f"{ref} is on the {'back' if s['side'] == 'front' else 'front'}.")
    return d, g


def _frame(data, s, d, g):
    """The part's box in the frame examples are kept in (the reference photo's, or this photo's
    when there's no reference), the transform from that frame to this photo, and the reference."""
    corners = designlib.outline_corners(g, d["rot_sign"])
    pts = corners + [[g["x"], g["y"]]]
    ref_uid = s["reference"]
    m_ref = _mapping(data, ref_uid, s["side"]) if ref_uid else s["mapping"]
    in_ref, in_src = m_ref.apply(pts), s["mapping"].apply(pts)
    A = np.c_[in_ref, np.ones(len(pts))]
    T = np.linalg.lstsq(A, in_src, rcond=None)[0].T          # 2x3: frame -> this photo
    x0, y0 = in_ref[:4].min(0)
    x1, y1 = in_ref[:4].max(0)
    return [float(x0), float(y0), float(x1 - x0), float(y1 - y0)], T, ref_uid


def _warp(img, T, rect):
    """The rect (x, y, w, h) of the frame, cut from a photo through T (frame -> photo)."""
    x, y, w, h = (int(round(v)) for v in rect)
    M = T.copy()
    M[:, 2] += M[:, :2] @ [x, y]
    return cv2.warpAffine(img, M, (max(1, w), max(1, h)), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


def _jpeg(img, size=220):
    k = size / max(img.shape[:2])
    img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


def crop(data: Path, src: str, ref: str, which: str = "board") -> bytes:
    """The part with some board round it: on this photo, or on the reference, in the same frame."""
    s = source(data, src)
    d, g = _geom(data, s, ref)
    box, T, ref_uid = _frame(data, s, d, g)
    x, y, w, h = box
    pad = max(10.0, 0.6 * max(w, h))
    rect = [x - pad, y - pad, w + 2 * pad, h + 2 * pad]
    if which == "reference":
        if not ref_uid:
            raise ValueError("No reference yet.")
        rimg = _image(pj.folder(data, ref_uid) / s["side"] / "reference.jpg")
        return _jpeg(_warp(rimg, np.float64([[1, 0, 0], [0, 1, 0]]), rect))
    return _jpeg(_warp(_image(s["img"]), T, rect))


def _placed(data, uid, side, ref):
    rows = pj._read_json(pj.folder(data, uid) / side / "parts_full.json", []) or []
    return next((r for r in rows if r["ref"] == ref), None)


def inspect(data: Path, src: str, ref: str) -> dict:
    """What the app makes of one part on one photo: correct, wrong, unsure or skipped, and why."""
    s = source(data, src)
    d, g = _geom(data, s, ref)
    board, side = s["board"], s["side"]
    comp = next((c for c in d.get("components", []) if c["ref"] == ref), {})
    in_check = next((p for p in d["parts"] if p["ref"] == ref), None)
    ex = [e for e in ck.load_examples(data, board, side)["examples"] if e["ref"] == ref]
    extra = (ck.board_profile(data, board).get("extra") or {}).get(ref) or {}
    out = {"ref": ref, "side": side, "board": board, "src": src, "label": s["label"], "reference": s["reference"],
           "kind": in_check["kind"] if in_check else None, "value": comp.get("value") or (in_check or {}).get("value", ""),
           "description": comp.get("description") or (in_check or {}).get("description", ""),
           "package": comp.get("package") or g.get("package", ""), "in_bom": comp.get("in_bom", True),
           "in_check": bool(in_check), "extra": bool(extra), "wait": bool(extra.get("wait")),
           "pending": bool(extra.get("wait")) and not ex,
           "good": sum(e["label"] == "good" for e in ex), "bad": sum(e["label"] == "bad" for e in ex),
           "examples": [{k: e.get(k, "") for k in ("id", "label", "reason", "note", "serial", "date")} for e in ex[-6:]],
           "answer": None, "verdict": "skipped", "why": ""}
    placed = _placed(data, s["reference"], side, ref) if s["reference"] else None
    small = placed and ("via" in placed or (placed.get("reason") or "").startswith("too small"))
    # what the check said, for a part on a checked board
    if s["kind"] == "check":
        c = ck.load(data, s["uid"])
        r = next((r for r in c["sides"][side].get("results", []) if r["ref"] == ref), None)
        if r:
            out.update(verdict=VERDICTS.get(r["status"], "unsure"), why=r.get("why") or "the check found it like the reference",
                       answer=r.get("answer"),
                       note=r.get("note", ""), reason=r.get("reason", ""), missed=bool(r.get("missed")), status=r["status"])
            if r.get("missed"):
                out.update(verdict="wrong", why="flagged by a tester though the check passed it")
            return out
        sk = next((k for k in c["sides"][side].get("skipped", []) if k["ref"] == ref), None)
        if sk:
            out.update(verdict="skipped", why=sk["why"] + ("; teach it and later boards check it" if not ex else ""))
            return out
    if not s["reference"]:
        out.update(verdict="skipped", why="there's no reference for this board yet to compare it with")
        return out
    skip = None
    if not in_check:
        skip = "not in the check: the BOM doesn't mark it polarised. Teach it and later boards check it"
    elif placed and not (placed["checkable"] and "via" not in placed) and not ((ex or out["pending"]) and small):
        skip = (placed.get("reason") or "needs a closeup") + ("; teach it and later boards check it" if small else "")
        if not small:          # hidden, or not (all) in the reference photo: nothing to compare
            out.update(why=skip)
            return out
    # compare it with the reference here, as the board check does
    box, T, ref_uid = _frame(data, s, d, g)
    rimg = _image(pj.folder(data, ref_uid) / side / "reference.jpg")
    x, y, w, h = box
    if x < 0 or y < 0 or x + w > rimg.shape[1] or y + h > rimg.shape[0]:
        out.update(why="the reference photo doesn't show it all")
        return out
    margin = max(8, int(0.004 * rimg.shape[1]))
    e = max(3, int(0.3 * min(w, h))) + margin + 4
    R = [max(0, int(x) - e), max(0, int(y) - e)]
    R += [int(x + w) + e - R[0], int(y + h) + e - R[1]]
    ref_local = _warp(rimg, np.float64([[1, 0, 0], [0, 1, 0]]), R)
    here = _warp(_image(s["img"]), T, R)
    part = {"ref": ref, "kind": out["kind"] or "look", "value": out["value"], "box": [x - R[0], y - R[1], w, h]}
    wrong = ref in ck.load_examples(data, board, side)["reference_wrong"].get(ref_uid, [])
    res = ck.check_part(here, ref_local, part, margin, ck.example_images(data, board, side).get(ref), wrong)
    seen = {"ok": "looks right", "reversed": "looks reversed", "rotated": "looks turned", "fault": "looks like a known fault",
            "check": "can't be told"}[res["status"]] + (f": {res['why']}" if res["why"] and res["status"] != "ok" else "")
    if s["is_reference"]:
        out.update(verdict="reference", why="this is the reference photo: other boards are compared with it"
                   + (f"; with what it has learned, it {seen}" if ex else ""))
    elif skip:
        out.update(verdict="skipped", why=f"{skip} (compared with the reference here, it {seen})")
    else:
        out.update(verdict=VERDICTS[res["status"]], why=res["why"] or seen, status=res["status"])
    if out["pending"]:
        out["why"] = "new part, teach me. " + out["why"]
    return out


def add(data: Path, src: str, ref: str, label: str, reason: str = "", note: str = "") -> str:
    """Keeps the part on this photo as a good or bad example, in the reference's frame."""
    if label not in ("good", "bad"):
        raise ValueError("Answer good or bad.")
    s = source(data, src)
    d, g = _geom(data, s, ref)
    box, T, _ = _frame(data, s, d, g)
    rect = ck.box_round(box)
    patch = _warp(_image(s["img"]), T, rect)
    ex_id = ck.add_example(data, s["board"], s["side"], patch, [0, 0, patch.shape[1], patch.shape[0]], ref, label, note,
                           s["serial"], s["check"], reason, src)
    if not ex_id:
        raise ValueError(f"{ref} couldn't be cut from that photo.")
    return ex_id


def pick(data: Path, src: str, fx: float, fy: float) -> dict:
    """The part under a click on a photo (fx, fy: 0 to 1 across the photo as shown; a check's photo
    is shown lined up with its reference)."""
    s = source(data, src)
    d = design_for(data, s["design"])
    img = _image(s["img"])
    xy = np.float32([fx * img.shape[1], fy * img.shape[0]])
    layer = pj.LAYER[s["side"]]
    geoms = {p["ref"]: p for p in d.get("components", []) if p["side"] == layer}
    geoms.update({p["ref"]: p for p in d["parts"] if p["side"] == layer})
    if not geoms:
        return {"ref": None, "near": []}
    refs = list(geoms)
    corners = [designlib.outline_corners(geoms[r], d["rot_sign"]) for r in refs]
    polys = s["mapping"].apply([c for cs in corners for c in cs]).reshape(-1, 4, 2)
    inside, near = [], []
    for r, q in zip(refs, polys):
        dist = cv2.pointPolygonTest(q.astype(np.float32), (float(xy[0]), float(xy[1])), True)
        area = cv2.contourArea(q.astype(np.float32))
        if dist >= -2:
            inside.append((area, r))
        near.append((-dist, r))
    inside.sort()
    near.sort()
    best = inside[0][1] if inside else (near[0][1] if near and near[0][0] < 0.02 * img.shape[1] else None)
    return {"ref": best, "near": list(dict.fromkeys(r for _, r in inside + near))[:6]}


def part_list(data: Path, uid: str) -> list[dict]:
    """Every part of a project's design, for the part picker."""
    d = design_for(data, uid)
    in_check = {p["ref"]: p for p in d["parts"]}
    return [{k: c.get(k) for k in ("ref", "side", "package", "value", "description", "in_bom", "dnp")}
            | {"kind": in_check[c["ref"]]["kind"] if c["ref"] in in_check else None}
            for c in d.get("components", [])]
