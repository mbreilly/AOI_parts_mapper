"""The board viewer on the Map tab: the open board's parts, pins and nets as compact JSON for the page
to draw on a canvas, each part's check status, and each side's lined-up photo turned into the
viewer's frame.

The page draws in "view" coordinates: board units with each side's orientation applied (x' = sx x,
y' = sy y, as the photo of that side was lined up, see photo.view), so the front reads as the design
shows it and the back as the board lies when turned over. A side's photo is warped into the same
frame here, so the page only has to place it on the board's rectangle.
"""

import hashlib
from pathlib import Path

import cv2
import numpy as np

from . import check as ck
from . import netlist as nl
from . import photo as ph
from . import teach as tl

PHOTO_MAX = 3200        # longest side of the warped photo, in pixels
GRID = 16               # the photo's transform is worked out every GRID pixels and filled in between


def nets(b) -> dict:
    """The nets and pins of the board's design, read once per ODB++ and kept per design."""
    step = b.odb.step if b.odb else b.profile.get("step")
    key = (str(b.odb_path), step, b.odb_path.stat().st_mtime_ns)
    if getattr(b, "_nets_key", None) != key:
        b._nets = nl.cached(b.odb_path, step, b.data / "boards" / b.name / "nets.json")
        b._nets_key = key
    return b._nets


def orient(b, side: str) -> list:
    """How a side is shown: its photo's line-up when it has one, else how the board lies."""
    m = (b.sides.get(side) or {}).get("mapping")
    return list(m.orient) if m else list(b.profile["orient"][side])


def _rect(b, side) -> list:
    """The board's rectangle in the side's view coordinates, with a margin: where the photo goes."""
    o, (sx, sy) = b.design["outline"], orient(b, side)
    xs = sorted([sx * o["xmin"], sx * o["xmax"]])
    ys = sorted([sy * o["ymin"], sy * o["ymax"]])
    m = 0.04 * max(xs[1] - xs[0], ys[1] - ys[0])
    return [xs[0] - m, ys[0] - m, xs[1] + m, ys[1] + m]


def _r(v, nd):
    return round(v, nd)


def geometry(b) -> dict:
    """Every part of the design (fiducials too) with its outline box, value and BOM line, the nets'
    names, the board outline and how each side is shown. Pins are fetched separately (pins())."""
    d, n = b.design, nets(b)
    nd = 5 if d["units"] == "inch" else 3
    comps = {c["ref"]: c for c in d.get("components", [])}
    line_of = {}
    if b.bom:
        for ln in b.bom["lines"] + b.bom.get("dnp_lines", []):
            for r in ln["refs"]:
                line_of[r] = ln
    parts, lines = [], {}
    for p in b.odb.parts:
        c = comps.get(p.ref, {})
        ln = line_of.get(p.ref)
        if ln and ln["key"] not in lines:
            lines[ln["key"]] = {"value": ln.get("value", ""), "description": ln.get("description", ""),
                                "qty": len(ln["refs"]), "dnp": bool(ln.get("dnp"))}
        parts.append({"r": p.ref, "s": p.side, "x": _r(p.x, nd), "y": _r(p.y, nd), "a": p.rot * d["rot_sign"],
                      "m": p.mirror, "b": [_r(v, nd) for v in p.bbox], "k": p.package,
                      "v": c.get("value", ln["value"] if ln else ""), "f": p.ref in d["fiducials"],
                      "l": ln["key"] if ln else None})
    outline = n["outline"] or [[d["outline"]["xmin"], d["outline"]["ymin"]], [d["outline"]["xmax"], d["outline"]["ymin"]],
                               [d["outline"]["xmax"], d["outline"]["ymax"]], [d["outline"]["xmin"], d["outline"]["ymax"]]]
    sides = {}
    for side in ("front", "back"):
        st = b.sides.get(side) or {}
        sides[side] = {"orient": orient(b, side), "photo": bool(st.get("mapping")), "stamp": b._stamp(side),
                       "rect": _rect(b, side)}
    return {"uid": b.project["uid"] if b.project else None, "board": b.name, "units": d["units"],
            "nets": n["nets"], "outline": outline, "parts": parts, "lines": lines, "sides": sides,
            "notes": n["notes"], "pins_without_net": n["pins_without_net"]}


def pins(b) -> dict:
    """{ref: [[name, x, y, net, w, h, round]]}: each pin's position (board units), net (-1: none) and
    pad size in the part's own frame (0 when the package doesn't say)."""
    n = nets(b)
    nd = 5 if b.design["units"] == "inch" else 3
    out = {}
    for side in ("top", "bot"):
        for c in n["parts"][side]:
            shapes = n["packages"][c["package"]]["pins"] if c["package"] >= 0 else []
            row = []
            for name, x, y, net, i in c["pins"]:
                w, h, rnd = shapes[i][1:4] if 0 <= i < len(shapes) else (0, 0, False)
                row.append([name, _r(x, nd), _r(y, nd), net, _r(w, nd), _r(h, nd), 1 if rnd else 0])
            out[c["ref"]] = row
    return {"pins": out}


def status(b) -> dict:
    """{ref: {"st", "why", "kind", "extra"}} for each part: "bad" / "good" (answered on this board's
    photo), "ns" (not stuffed), "checked" (in the check and its photo shows it), "skipped" (in the
    check but the photo can't show it), "nophoto" (in the check, its side has no lined-up photo) or
    "off" (not in the check)."""
    d = b.design
    kinds = {p["ref"]: p for p in d["parts"]}
    out = {}
    for rows in b.line_status().values():
        for r in rows:
            out[r["ref"]] = {"st": r["status"], "why": r.get("why", ""), "good": r.get("good", 0), "bad": r.get("bad", 0)}
    for c in d.get("components", []):
        out.setdefault(c["ref"], {"st": "off", "why": "not in the BOM" if not c["in_bom"] else "not in the check",
                                  "good": 0, "bad": 0})
    for side in ("front", "back"):
        st = b.sides.get(side) or {}
        for r in st.get("placed", []):      # parts added by hand are in the check but on no BOM line
            if r["ref"] in out and out[r["ref"]]["st"] == "off":
                out[r["ref"]].update(st="checked" if r["checkable"] else "skipped", why=r.get("reason") or "")
        if b.project:
            for ref, ans in tl.answers(b.data, b.name, f"project/{b.project['uid']}/{side}").items():
                if ref in out:
                    out[ref]["answer"] = ans
                    out[ref]["st"] = ans
            for e in ck.load_examples(b.data, b.name, side)["examples"]:
                if e.get("source") == f"project/{b.project['uid']}/{side}" and e["ref"] in out and e["label"] == "bad":
                    out[e["ref"]]["reason"] = e.get("reason", "")
    for ref, o in out.items():
        p = kinds.get(ref)
        o["kind"] = p["kind"] if p else None
        o["extra"] = bool(p and p.get("extra"))
    return {"parts": out}


def photo(b, side: str) -> bytes:
    """The side's lined-up photo warped into the viewer's frame (see the module notes), as a JPEG.
    Kept in <data>/cache/viewer/ until the photo or its line-up changes."""
    st = b.sides.get(side) or {}
    m = st.get("mapping")
    if st.get("img") is None or not m:
        raise ValueError(f"The {side} has no lined-up photo yet.")
    stamp = b._stamp(side)
    rect = _rect(b, side)
    key = hashlib.sha1(f"{stamp}:{rect}:{PHOTO_MAX}".encode()).hexdigest()[:12]
    cache = b.data / "cache" / "viewer" / f"{b.project['uid']}-{side}.jpg"
    keyf = cache.with_suffix(".key")
    if cache.is_file() and keyf.is_file() and keyf.read_text() == key:
        return cache.read_bytes()
    x0, y0, x1, y1 = rect
    sx, sy = m.orient
    ppu = m.px_per_unit([sx * (x0 + x1) / 2, sy * (y0 + y1) / 2])
    scale = min(ppu, PHOTO_MAX / max(x1 - x0, y1 - y0))
    W, H = max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))
    gx, gy = np.arange(0, W + GRID, GRID), np.arange(0, H + GRID, GRID)
    U, V = np.meshgrid(gx, gy)
    board = np.stack([sx * (x0 + U / scale), sy * (y0 + V / scale)], -1).reshape(-1, 2)
    P = m.apply(board).reshape(len(gy), len(gx), 2).astype(np.float32)
    # dst pixel (u, v) takes the grid's value at (u / GRID, v / GRID)
    A = np.float32([[1 / GRID, 0, 0], [0, 1 / GRID, 0]])
    mx = cv2.warpAffine(P[..., 0], A, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    my = cv2.warpAffine(P[..., 1], A, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    out = cv2.remap(st["img"], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(20, 20, 22))
    data = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    keyf.write_text(key)
    return data
