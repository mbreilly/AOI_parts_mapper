"""Lines a board photo up with the design from the fiducials the user clicks, places every polarised
part on it, decides which ones this photo can check, and draws the map.

Board coordinates are first turned into "view" coordinates, the board as it lies in the photo:
view = (sx * x, sy * y). The front is normally (1, -1) (photo y runs down, board y runs up) and the
back (1, 1) when the board is turned over top to bottom, or (-1, -1) when turned left to right.
A transform is then fitted from view to photo pixels: a similarity from 2 points, affine from 3,
homography from 4 or more, with the error left at each anchor spread smoothly between anchors to
follow lens bending.
"""

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .design import marking, outline_corners

SIDE_LAYER = {"front": "top", "back": "bot"}
DEFAULT_ORIENT = {"front": (1, -1), "back": (1, 1)}
CORNERS = ["top-left", "top-right", "bottom-right", "bottom-left"]

KIND_COLOURS = {   # BGR
    "ic": (0, 165, 255), "tantalum": (255, 0, 255), "electrolytic": (255, 255, 0),
    "diode": (60, 60, 255), "led": (80, 230, 80), "other": (230, 230, 230),
}
GREY = (150, 150, 150)


def decode(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("That image couldn't be read. Use a JPEG or PNG.")
    return img


def view(pts, orient):
    sx, sy = orient
    return [[sx * x, sy * y] for x, y in pts]


# ---- fiducials ---------------------------------------------------------------------------

def side_fiducials(design: dict, side: str) -> dict:
    layer = SIDE_LAYER[side]
    return {r: f for r, f in design["fiducials"].items() if f["side"] == layer}


def anchor_prompts(points: dict, orient) -> list[dict]:
    """For each anchor {ref: [x, y]} (board units), which part of the photo it should be in."""
    v = {r: view([p], orient)[0] for r, p in points.items()}
    # in view coordinates photo y runs down, so "top" is the smallest y
    xs, ys = [p[0] for p in v.values()], [p[1] for p in v.values()]
    out = []
    for r, (x, y) in v.items():
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        w, h = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
        horiz = "left" if x < cx - 0.15 * w else "right" if x > cx + 0.15 * w else "middle"
        vert = "top" if y < cy - 0.15 * h else "bottom" if y > cy + 0.15 * h else "middle"
        where = "centre" if horiz == vert == "middle" else f"{vert}-{horiz}".replace("middle-", "").replace("-middle", "")
        out.append({"ref": r, "where": where, "view": [x, y]})
    order = {"top-left": 0, "top": 1, "top-right": 2, "right": 3, "bottom-right": 4, "bottom": 5, "bottom-left": 6, "left": 7,
             "centre": 8}
    return sorted(out, key=lambda o: (order.get(o["where"], 9), o["ref"]))


def find_pads(img: np.ndarray, gold_only: bool = False) -> np.ndarray:
    """Small round metal pads that could be fiducials, as [x, y] centres: gold ones sized like a
    1 mm fiducial (the PTC's), and for snapping a click also tinned or silver ones."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s = img.shape[1] / 2576
    masks = [(cv2.inRange(hsv, (12, 70, 120), (38, 255, 255)), (60, 280), (8, 20))]
    if not gold_only:
        masks.append((cv2.inRange(hsv, (0, 0, 200), (180, 50, 255)), (40, 1200), (6, 45)))
    out = []
    for m, (amin, amax), (wmin, wmax) in masks:
        n, _, st, cen = cv2.connectedComponentsWithStats(m)
        for i in range(1, n):
            w, h, a = st[i][2], st[i][3], st[i][4]
            if amin * s ** 2 < a < amax * s ** 2 and wmin * s < w < wmax * s and 0.7 < w / h < 1.4 \
                    and a / (math.pi * (w / 2) ** 2) > 0.6:
                out.append(cen[i])
    return np.array(out, np.float32).reshape(-1, 2)


def snap(img: np.ndarray, xy, pads: np.ndarray | None = None) -> tuple[list, bool]:
    """The pad centre nearest a click (within width/80), or the click itself."""
    pads = find_pads(img) if pads is None else pads
    xy = np.float32(xy)
    if len(pads):
        d = np.linalg.norm(pads - xy, axis=1)
        if d.min() < img.shape[1] / 80:
            return [float(v) for v in pads[d.argmin()]], True
    return [float(xy[0]), float(xy[1])], False


def auto_fiducials(img: np.ndarray, fids: dict, orient) -> dict | None:
    """With 4 or more fiducials near the corners, the outermost pad in each corner of the photo."""
    if len(fids) < 4:
        return None
    pads = find_pads(img, gold_only=True)
    if len(pads) < 4:
        return None
    names = list(fids)
    v = np.float32(view([[fids[r]["x"], fids[r]["y"]] for r in names], orient))
    found = {}
    for a, b in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        r = names[int(np.argmin(a * v[:, 0] + b * v[:, 1]))]
        found[r] = [float(t) for t in pads[int(np.argmin(a * pads[:, 0] + b * pads[:, 1]))]]
    return found if len(found) == 4 else None


# ---- the transform -----------------------------------------------------------------------

@dataclass
class Mapping:
    kind: str
    M: np.ndarray                     # 3x3, view -> photo
    orient: tuple
    anchors_view: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), np.float32))
    residual: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), np.float32))
    errors: list = field(default_factory=list)

    def apply(self, board_pts) -> np.ndarray:
        pts = np.float32(view(board_pts, self.orient)).reshape(-1, 2)
        base = cv2.perspectiveTransform(pts[None], self.M)[0]
        if len(self.residual):
            w = 1 / (np.linalg.norm(pts[:, None] - self.anchors_view[None], axis=2) ** 2 + 1e-12)
            base = base + (w @ self.residual) / w.sum(1, keepdims=True)
        return base

    def px_per_unit(self, at) -> float:
        """Photo pixels per board unit near a board point."""
        x, y = at
        d = 0.01 * max(1.0, abs(x) + abs(y))
        a, b, c = self.apply([[x, y], [x + d, y], [x, y + d]])
        return float((np.linalg.norm(b - a) + np.linalg.norm(c - a)) / (2 * d))

    def to_dict(self):
        return {"kind": self.kind, "M": self.M.tolist(), "orient": list(self.orient),
                "anchors_view": self.anchors_view.tolist(), "residual": self.residual.tolist(), "errors": self.errors}

    @staticmethod
    def from_dict(d):
        return Mapping(d["kind"], np.float64(d["M"]), tuple(d["orient"]), np.float32(d["anchors_view"]).reshape(-1, 2),
                       np.float32(d["residual"]).reshape(-1, 2), d.get("errors", []))


def fit(board_pts, photo_pts, orient, extra_board=(), extra_photo=()) -> Mapping:
    """The view->photo transform through the user's points; extra anchors only refine it."""
    v = np.float32(view(board_pts, orient))
    p = np.float32(photo_pts)
    n = len(v)
    if n < 2:
        raise ValueError("At least 2 points are needed to line up the photo.")
    if n == 2:
        M, kind = np.vstack([_similarity(v, p), [0, 0, 1]]), "similarity"
    elif n == 3:
        M, kind = np.vstack([cv2.getAffineTransform(v, p), [0, 0, 1]]), "affine"
    else:
        M, _ = cv2.findHomography(v, p, 0)
        kind = "homography"
        if M is None:
            raise ValueError("Those points can't be lined up (are three of them in a line?).")
    m = Mapping(kind, M.astype(np.float64), tuple(orient))
    pred = m.apply(board_pts)
    m.errors = [float(e) for e in np.linalg.norm(pred - p, axis=1)]
    allb = list(board_pts) + list(extra_board)
    allp = np.float32(list(photo_pts) + list(extra_photo))
    if n >= 4 or len(extra_board):
        base = cv2.perspectiveTransform(np.float32(view(allb, orient))[None], m.M)[0]
        m.anchors_view, m.residual = np.float32(view(allb, orient)), allp - base
    return m


def _similarity(v, p):
    (a, b), (c, d) = v
    (e, f), (g, h) = p
    dv, dp = complex(c - a, d - b), complex(g - e, h - f)
    if abs(dv) < 1e-12:
        raise ValueError("The two points are at the same place on the board.")
    z = dp / dv
    t = complex(e, f) - z * complex(a, b)
    return np.float64([[z.real, -z.imag, t.real], [z.imag, z.real, t.imag]])


def check_points(board_pts, photo_pts, orient, img_w, mapping) -> list[str]:
    """Warnings about clicked points that don't look like the design."""
    warn = []
    v = np.float32(view(board_pts, orient)); p = np.float32(photo_pts)
    if len(v) >= 2:
        vw, vh = np.ptp(v[:, 0]), np.ptp(v[:, 1])
        pw, ph = np.ptp(p[:, 0]), np.ptp(p[:, 1])
        if pw < 0.3 * img_w and len(v) >= 3 and vw > 0:
            warn.append("The points cover less than a third of the photo. Points further apart line it up better.")
        if len(v) >= 3 and vh > 0 and ph > 0 and abs((pw / ph) / (vw / vh) - 1) > 0.15:
            warn.append("The points don't have the board's shape. Check each is the one asked for, and the board's "
                        "orientation for this side.")
        # each point in the right part of the photo
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                dx, dy = v[j] - v[i]
                ex, ey = p[j] - p[i]
                if (abs(dx) > 0.2 * vw and np.sign(dx) != np.sign(ex)) or (abs(dy) > 0.2 * vh and np.sign(dy) != np.sign(ey)):
                    warn.append("Two of the points are the wrong way round compared with the design. Check the order, "
                                "or how the board was turned over.")
                    break
            else:
                continue
            break
    big = [e for e in mapping.errors if e > max(4.0, img_w / 300)]
    if big and mapping.kind != "similarity" and len(board_pts) > 4:
        warn.append(f"{len(big)} point(s) are more than {max(4.0, img_w / 300):.0f} px from where the others put them.")
    return warn


# ---- extra anchors: tantalum capacitors -------------------------------------------------

def find_tantalums(img: np.ndarray) -> np.ndarray:
    """Yellow-orange tantalum bodies by colour, as centres."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (12, 110, 140), (32, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    s = img.shape[1] / 2576
    n, _, st, cen = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        w, h, a = st[i][2], st[i][3], st[i][4]
        long, short = max(w, h), min(w, h)
        if 250 * s ** 2 < a < 3000 * s ** 2 and 1.2 < long / short < 2.8 and short > 10 * s and a / (w * h) > 0.55:
            out.append(cen[i])
    return np.array(out, np.float32).reshape(-1, 2)


def refine_with_tantalums(img, design, side, mapping: Mapping, board_pts, photo_pts) -> tuple[Mapping, int, int]:
    """Adds the tantalum capacitors found near where the design puts them as extra anchors.
    Returns the new mapping, how many matched and how many the design has on this side."""
    tants = [p for p in design["parts"] if p["side"] == SIDE_LAYER[side] and p["kind"] == "tantalum" and not p.get("hidden")]
    if not tants:
        return mapping, 0, 0
    seen = find_tantalums(img)
    if not len(seen):
        return mapping, 0, len(tants)
    pred = mapping.apply([[p["x"], p["y"]] for p in tants])
    tol = 0.1 * 25.4 / design["mm_per_unit"] * mapping.px_per_unit([tants[0]["x"], tants[0]["y"]])   # 2.5 mm
    eb, ep = [], []
    for p, pr in zip(tants, pred):
        d = np.linalg.norm(seen - pr, axis=1)
        if d.min() < tol:
            eb.append([p["x"], p["y"]]); ep.append([float(v) for v in seen[d.argmin()]])
    if len(eb) < 2:
        return mapping, len(eb), len(tants)
    return fit(board_pts, photo_pts, mapping.orient, eb, ep), len(eb), len(tants)


# ---- placing the parts --------------------------------------------------------------------

def place(design: dict, side: str, img_shape, mapping: Mapping, min_px: float, rot_sign: int,
          only_refs: set | None = None, margin_px: float = 2) -> list[dict]:
    """Every polarised part on this side, with its outline in the photo and whether it can be checked."""
    H, W = img_shape[:2]
    layer = SIDE_LAYER[side]
    out = []
    for p in design["parts"]:
        if p["side"] != layer or (only_refs is not None and p["ref"] not in only_refs):
            continue
        poly = mapping.apply(outline_corners(p, rot_sign))
        centre = mapping.apply([[p["x"], p["y"]]])[0]
        e = [np.linalg.norm(poly[1] - poly[0]), np.linalg.norm(poly[2] - poly[1])]
        px = float(min(e))
        x0, y0 = poly.min(0); x1, y1 = poly.max(0)
        rec = {"ref": p["ref"], "kind": p["kind"], "value": p["value"], "poly": poly.round(1).tolist(),
               "centre": centre.round(1).tolist(),
               "box": [int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0))],
               "px": round(px, 1), "pin1_dir": p["pin1_dir"], "pin1_how": p["pin1_how"]}
        if p.get("pin1"):
            rec["pin1"] = mapping.apply([p["pin1"]])[0].round(1).tolist()
        mk = marking(p, design.get("markings", {}))
        if mk:
            rec["mark"] = {"type": mk["type"], "at": mapping.apply([mk["at"]])[0].round(1).tolist()}
            if mk["type"] == "band":
                rec["mark"]["towards"] = centre.round(1).tolist()
        reason, need = None, None
        if p.get("hidden"):
            reason = f"hidden ({p['hidden']})"
        elif x1 < 0 or y1 < 0 or x0 > W or y0 > H:
            reason = "not in this photo"
        elif x0 < -margin_px or y0 < -margin_px or x1 > W + margin_px or y1 > H + margin_px:
            reason = "cut off at the edge of the photo"
        elif px < min_px:
            need = min_px / max(px, 0.1)
            reason = f"too small: {px:.0f} px across, needs {min_px:.0f} px ({need:.1f}x closer)"
            if p["kind"] == "led":
                reason += "; a reversed LED won't light, so a light test also catches it"
        elif p["kind"] == "ic" and p["pin1_how"] != "named":
            rec["note"] = "pin 1 not named in the design" + (" (lowest pin used)" if p["pin1_how"] == "guess" else "")
        rec["checkable"] = reason is None
        rec["reason"] = reason or ""
        rec["zoom_needed"] = round(need, 2) if need else None
        out.append(rec)
    return out


def wrong_side_warning(img, design, side, n_tant_found, n_tant_design) -> str | None:
    if n_tant_design >= 6 and n_tant_found < n_tant_design / 2:
        return (f"Only {n_tant_found} of the {n_tant_design} tantalum capacitors on the {side} are where the design "
                f"puts them. Is this the {side}, turned over the way you chose?")
    if n_tant_design == 0 and len(find_tantalums(img)) >= 6 and \
            any(p["kind"] == "tantalum" for p in design["parts"] if p["side"] != SIDE_LAYER[side]):
        return f"This photo shows tantalum capacitors, which the design only has on the other side. Is it the {side}?"
    return None


# ---- closeups for parts too small in the full photo -----------------------------------

def plan_closeups(design: dict, side: str, placed: list[dict], img_shape, min_px: float, rot_sign: int,
                  max_anchor: int = 4) -> list[dict]:
    """Groups the parts that are too small into board regions, each small enough that a photo of
    just that region shows them large enough. Each region lists anchors (fiducials or part centres)
    for the user to click on the closeup."""
    small = {r["ref"]: r for r in placed if r["reason"].startswith("too small")}
    if not small:
        return []
    H, W = img_shape[:2]
    layer = SIDE_LAYER[side]
    parts = {p["ref"]: p for p in design["parts"] if p["side"] == layer}
    # region size in board units: a closeup framed on the region (with 25% spare) must make the
    # smallest part in it at least min_px across
    tiles = []
    todo = sorted(small, key=lambda r: (parts[r]["y"], parts[r]["x"]), reverse=True)
    while todo:
        seed = parts[todo[0]]
        smin = min(seed["size"]) or 0.01
        span = W * smin / min_px / 1.25            # board units across the closeup, at photo width W
        span_y = span * H / W
        cx, cy = seed["x"], seed["y"]
        members = [r for r in todo if abs(parts[r]["x"] - cx) <= span / 2 and abs(parts[r]["y"] - cy) <= span_y / 2]
        # re-centre on the members so the region frames them
        xs, ys = [parts[r]["x"] for r in members], [parts[r]["y"] for r in members]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        smin = min(min(parts[r]["size"]) or 0.01 for r in members)
        span = W * smin / min_px / 1.25
        span_y = span * H / W
        members = [r for r in todo if abs(parts[r]["x"] - cx) <= span / 2 and abs(parts[r]["y"] - cy) <= span_y / 2] or [todo[0]]
        region = [cx - span / 2, cy - span_y / 2, cx + span / 2, cy + span_y / 2]
        tiles.append({"index": len(tiles), "refs": sorted(members), "region": region, "centre": [cx, cy],
                      "anchors": _tile_anchors(design, side, region, members, max_anchor)})
        todo = [r for r in todo if r not in members]
    return tiles


def _tile_anchors(design, side, region, members, n):
    """Points to click on a closeup: fiducials in the region first, then the biggest parts in it,
    spread out (the region's own small parts last)."""
    x0, y0, x1, y1 = region
    inside = lambda x, y: x0 <= x <= x1 and y0 <= y <= y1
    layer = SIDE_LAYER[side]
    cands = [{"ref": r, "x": f["x"], "y": f["y"], "what": "fiducial", "size": 1e9}
             for r, f in design["fiducials"].items() if f["side"] == layer and inside(f["x"], f["y"])]
    cands += [{"ref": p["ref"], "x": p["x"], "y": p["y"], "what": "centre of", "size": max(p["size"]) * (0.5 if p["ref"] in members else 1)}
              for p in design["parts"] if p["side"] == layer and not p.get("hidden") and inside(p["x"], p["y"])]
    cands.sort(key=lambda c: -c["size"])
    chosen = []
    for c in cands:   # greedily keep points far from those already chosen
        if len(chosen) >= n:
            break
        if all(math.dist((c["x"], c["y"]), (o["x"], o["y"])) > 0.25 * (x1 - x0) for o in chosen):
            chosen.append(c)
    for c in cands:
        if len(chosen) >= min(n, 2):
            break
        if c not in chosen:
            chosen.append(c)
    return [{"ref": c["ref"], "what": c["what"], "x": c["x"], "y": c["y"]} for c in chosen]


# ---- rotation check -----------------------------------------------------------------------

def rotation_choice(img, design, side, mapping: Mapping) -> dict | None:
    """When the design's own pin data can't tell which way ODB++ rotation runs, two closeups of a
    part drawn each way for the user to pick from. None when no part on this side looks different
    either way."""
    from .design import rotation_evidence
    layer = SIDE_LAYER[side]
    cands = [p for p in design["parts"] if p["side"] == layer and not p.get("hidden")]
    ev = rotation_evidence(cands)
    best = None
    for p in cands:
        a = mapping.apply(outline_corners(p, 1)); b = mapping.apply(outline_corners(p, -1))
        diff = float(max(np.linalg.norm(a - b, axis=1)))
        if diff > 6 and (best is None or diff > best[1]):
            best = (p, diff, a, b)
    if best is None:
        return {"needed": False, "message": "Every part on this side has the same outline whichever way rotation runs, "
                                            "so no check is needed."}
    p, _, a, b = best
    crops = []
    for poly in (a, b):
        crops.append(_crop_with_poly(img, poly, a, b))
    return {"needed": True, "ref": p["ref"], "votes": ev["votes"], "crops": crops}


def _crop_with_poly(img, poly, a, b):
    allp = np.vstack([a, b])
    x0, y0 = allp.min(0); x1, y1 = allp.max(0)
    pad = max(20, 0.6 * max(x1 - x0, y1 - y0))
    H, W = img.shape[:2]
    X0, Y0 = int(max(0, x0 - pad)), int(max(0, y0 - pad))
    X1, Y1 = int(min(W, x1 + pad)), int(min(H, y1 + pad))
    c = img[Y0:Y1, X0:X1].copy()
    s = 320 / max(1, max(c.shape[:2]))
    c = cv2.resize(c, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
    q = ((poly - [X0, Y0]) * s).astype(np.int32)
    cv2.polylines(c, [q], True, (0, 0, 255), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


# ---- drawing --------------------------------------------------------------------------------

def overlay(img: np.ndarray, placed: list[dict], width: int = 1600, highlight: str | None = None,
            show_marks: bool = True) -> bytes:
    """A JPEG of the photo with every part outlined in its kind's colour, pin 1 as a dot, the expected
    polarity mark drawn, and parts that can't be checked in grey."""
    s = min(1.0, width / img.shape[1]) if width else 1.0
    vis = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s != 1 else img.copy()
    t = max(1, int(round(2 * vis.shape[1] / 1600)))
    for r in placed:
        col = KIND_COLOURS.get(r["kind"], KIND_COLOURS["other"]) if r["checkable"] else GREY
        q = (np.float32(r["poly"]) * s).astype(np.int32)
        cv2.polylines(vis, [q], True, col, t + (2 if r["ref"] == highlight else 0), cv2.LINE_AA)
        if r.get("pin1"):
            cv2.circle(vis, tuple(int(v * s) for v in r["pin1"]), max(2, t + 1), (0, 0, 255) if r["checkable"] else GREY, -1, cv2.LINE_AA)
        if show_marks and r.get("mark") and r["checkable"] and r["mark"]["type"] == "band":
            at, to = np.float32(r["mark"]["at"]) * s, np.float32(r["mark"]["towards"]) * s
            d = to - at
            n = np.linalg.norm(d)
            if n > 3:
                d /= n
                perp = np.float32([-d[1], d[0]])
                half = 0.5 * min(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[1]))
                p0, p1 = at + d * 0.15 * n - perp * half, at + d * 0.15 * n + perp * half
                cv2.line(vis, tuple(p0.astype(int)), tuple(p1.astype(int)), (255, 255, 255), t + 1, cv2.LINE_AA)
        if r["checkable"] and (r["px"] * s > 18 or r["ref"] == highlight):
            x, y = (int(v * s) for v in r["poly"][0])
            cv2.putText(vis, r["ref"], (x, max(10, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4 * max(1, t), col, 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


def ghost_svg(design: dict, side: str, orient, width: int = 1000, height: int = 750) -> str:
    """The board outline, fiducials and parts drawn as the board should lie in the photo, for lining
    the board up before the first photo exists."""
    o = design["outline"]
    corners = view([[o["xmin"], o["ymin"]], [o["xmax"], o["ymin"]], [o["xmax"], o["ymax"]], [o["xmin"], o["ymax"]]], orient)
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    bw, bh = max(xs) - min(xs), max(ys) - min(ys)
    k = 0.85 * min(width / bw, height / bh)
    ox, oy = (width - k * bw) / 2 - k * min(xs), (height - k * bh) / 2 - k * min(ys)
    P = lambda pts: " ".join(f"{ox + k * x:.1f},{oy + k * y:.1f}" for x, y in view(pts, orient))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
           f'<polygon points="{P([[o["xmin"], o["ymin"]], [o["xmax"], o["ymin"]], [o["xmax"], o["ymax"]], [o["xmin"], o["ymax"]]])}" '
           'fill="none" stroke="#7fd" stroke-width="3"/>']
    layer = SIDE_LAYER[side]
    for p in design["parts"]:
        if p["side"] == layer:
            out.append(f'<polygon points="{P(p["corners"])}" fill="none" stroke="#fd7" stroke-width="1.2"/>')
    for r, f in design["fiducials"].items():
        if f["side"] == layer:
            (x, y), = view([[f["x"], f["y"]]], orient)
            out.append(f'<circle cx="{ox + k * x:.1f}" cy="{oy + k * y:.1f}" r="7" fill="none" stroke="#f7a" stroke-width="2.5"/>'
                       f'<text x="{ox + k * x + 10:.1f}" y="{oy + k * y - 8:.1f}" fill="#f7a" font-size="16" font-family="sans-serif">{r}</text>')
    out.append("</svg>")
    return "".join(out)
