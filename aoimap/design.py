"""Joins the ODB++ and the BOM into the board's parts map: every polarised part with its position,
rotated outline, pin 1 and the marking a person should see on it."""

import math
import re

from . import bom as bomlib
from .odb import OdbDesign, pin1_of

MM_PER = {"inch": 25.4, "mm": 1.0}

# Which end of a two-pin polarised part carries the marking (band or + sign), by kind.
# "pin1": the marked end is pin 1 (usual for tantalum +, diode and LED cathode); "other": the other pin.
DEFAULT_MARKINGS = {"tantalum": "pin1", "electrolytic": "pin1", "diode": "pin1", "led": "pin1"}


def line_key(line: dict) -> str:
    """What a BOM line's choices are remembered by: its part number, or its description."""
    return (line.get("value") or line.get("description") or ",".join(line["refs"][:3])).strip().upper()


def outline_corners(p: dict, rot_sign: int = 1) -> list[list[float]]:
    """The four corners of the part's package box on the board, in board units."""
    x0, y0, x1, y1 = p["bbox"]
    t = math.radians(p["rot"]) * rot_sign
    c, s = math.cos(t), math.sin(t)
    m = -1 if p["mirror"] else 1
    return [[p["x"] + c * m * u - s * v, p["y"] + s * m * u + c * v] for u, v in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def _inside(pt, poly, tol):
    """Point in a convex quad, with a tolerance in board units."""
    n = len(poly)
    sign = 0
    for i in range(n):
        ax, ay = poly[i]; bx, by = poly[(i + 1) % n]
        cross = (bx - ax) * (pt[1] - ay) - (by - ay) * (pt[0] - ax)
        length = math.hypot(bx - ax, by - ay) or 1
        d = cross / length
        if abs(d) <= tol:
            continue
        s = 1 if d > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def rotation_evidence(parts: list[dict]) -> dict:
    """Which rotation direction puts each part's pin 1 inside its outline. Parts whose outline is the
    same either way (a box centred on the part, at a multiple of 90 degrees) say nothing."""
    votes = {1: 0, -1: 0}
    telling = []
    for p in parts:
        if not p.get("pin1"):
            continue
        a, b = outline_corners(p, 1), outline_corners(p, -1)
        if max(math.dist(u, v) for u, v in zip(sorted(a), sorted(b))) < 1e-6:
            continue
        tol = 0.02 * max(abs(v) for v in p["bbox"]) + 1e-6
        ia, ib = _inside(p["pin1"], a, tol), _inside(p["pin1"], b, tol)
        if ia != ib:
            votes[1 if ia else -1] += 1
            telling.append(p["ref"])
    return {"votes": votes, "telling": telling}


def pin1_direction(p: dict) -> str:
    """Compass direction from the part centre to pin 1, on the board as seen from the top (N is +y)."""
    if not p.get("pin1"):
        return ""
    dx, dy = p["pin1"][0] - p["x"], p["pin1"][1] - p["y"]
    if math.hypot(dx, dy) < 1e-6:
        return "centre"
    a = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((a + 22.5) // 45) % 8]


def build(odb: OdbDesign, bom: dict, profile: dict) -> dict:
    """The parts map. profile holds the user's choices: kinds and hidden by BOM line key."""
    kinds = profile.get("kinds", {})
    hidden = profile.get("hidden", {})
    odb_by_ref = {p.ref: p for p in odb.parts}
    parts = []
    for line in bom["lines"]:
        key = line_key(line)
        kind = kinds[key] if key in kinds else line["kind"]
        line["key"], line["chosen_kind"], line["hidden"] = key, kind, hidden.get(key)
        line["on_board"] = [r for r in line["refs"] if r in odb_by_ref]
        if not kind:
            continue
        for ref in line["refs"]:
            c = odb_by_ref.get(ref)
            if not c:
                continue
            pin1, how = pin1_of(c.pins)
            other = None
            if len(c.pins) == 2 and pin1:
                other = next(v for v in c.pins.values() if v is not pin1)
            p = {"ref": ref, "side": c.side, "x": c.x, "y": c.y, "rot": c.rot, "mirror": c.mirror,
                 "package": c.package, "bbox": c.bbox, "pin1": pin1, "pin1_how": how, "other_pin": other,
                 "pins": len(c.pins), "kind": kind, "value": line["value"], "description": line["description"],
                 "hidden": hidden.get(key), "key": key}
            parts.append(p)
    rot_sign = profile.get("rot_sign")
    ev = rotation_evidence(parts)
    if rot_sign is None:
        rot_sign = -1 if ev["votes"][-1] > ev["votes"][1] else 1
    for p in parts:
        p["corners"] = outline_corners(p, rot_sign)
        p["pin1_dir"] = pin1_direction(p)
        w, h = abs(p["bbox"][2] - p["bbox"][0]), abs(p["bbox"][3] - p["bbox"][1])
        p["size"] = [w, h]
    bom_refs = set(bom["parts"])
    odb_refs = {p.ref for p in odb.parts}
    fid_refs = set(odb.fiducials)
    parts.sort(key=lambda p: (p["side"], re.sub(r"\d+", lambda m: m.group().zfill(6), p["ref"])))
    return {
        "name": odb.name, "units": odb.units, "units_known": odb.units_known, "mm_per_unit": MM_PER[odb.units],
        "outline": odb.outline, "fiducials": odb.fiducials, "parts": parts, "rot_sign": rot_sign,
        "rotation_evidence": ev,
        "bom_only": sorted(bom_refs - odb_refs), "odb_only": sorted(odb_refs - bom_refs - fid_refs - set(bom["dnp"])),
        "dnp_on_board": sorted(set(bom["dnp"]) & odb_refs), "notes": list(odb.notes),
        "counts": counts(parts),
    }


def counts(parts):
    out = {}
    for p in parts:
        side = out.setdefault(p["side"], {})
        side[p["kind"]] = side.get(p["kind"], 0) + 1
    return out


def marking(p: dict, markings: dict) -> dict | None:
    """Where the polarity mark should be, in board units: for two-pin parts the end that's banded,
    for ICs the pin 1 corner. None when the design doesn't say."""
    if not p.get("pin1"):
        return None
    if p["kind"] in ("ic", "other") or not p.get("other_pin"):
        return {"type": "dot", "at": p["pin1"]}
    end = p["pin1"] if markings.get(p["kind"], DEFAULT_MARKINGS.get(p["kind"], "pin1")) == "pin1" else p["other_pin"]
    return {"type": "band", "at": end, "towards": [p["x"], p["y"]]}
