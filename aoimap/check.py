"""Checking another board of a type already mapped: are its polarised parts the right way round?

A project whose photos were lined up, from a board checked by hand, can be marked as the reference
for its board type. A photo of another board of that type is then lined up with the reference
photo (feature matching, so it can be a little rotated, shifted or closer; or from fiducials the
user clicks when that fails), and each part the reference could check is compared with the same
part on the reference as placed and turned round. A part that matches the turned version better
is flagged as reversed; one that matches neither is flagged to be looked at. The user answers each
flagged part. This is a quick look before work starts on a board, not a production test.

The method is the PTC QC station's orientation check (ptctestsuite/utils/inspection.py), and so is
the learning: every answer is kept as a good or bad example of that part (see "learned examples").

    <data>/checks/<uid>/
        check.json              board, reference project, serial, notes, every part's result and answer, log
        front/ back/            photo.jpg (as taken), aligned.jpg (lined up with the reference), result.jpg
    <data>/learned/<board>/<side>/
        examples.json           the examples, and the parts each reference shows wrong
        <id>.jpg                each example: the part's area of a lined-up photo
"""

import json
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import photo as ph
from . import projects as pj

ALIGN_WIDTH = 2000          # photos are lined up at this width (faster, still accurate)
FEATURES = 6000
MIN_MATCHES = 60            # fewer matching features than this: the photo can't be lined up
REVERSE_MARGIN = 0.10       # a quarter turn must match this much better to flag "rotated"
MIN_MATCH = 0.60            # below this a part doesn't look like the reference: "look at it"
MARK_STRENGTH = 0.15        # a reference part with less difference end to end than this shows no mark
MARK_MIN = 0.30             # the polarity mark must agree (or disagree) at least this much to decide
LEARNED_MARGIN = 0.03       # a learned example must match this much better than the other kind to decide
MAX_EXAMPLES = 20           # learned examples of each kind (good, bad) kept for each part; the oldest go first
# These come from the PTC station's photos; tune them on real boards.

# why a part is wrong, as the tester picks it
REASONS = {"reversed": "Reversed", "missing": "Missing", "wrong_part": "Wrong part", "damaged": "Damaged",
           "shifted": "Misplaced / shifted", "other": "Other"}

# called with (board, side) whenever a side's learned examples change (the database copy follows)
LEARNED_LISTENERS = []

STATUS_COLOURS = {"ok": (80, 190, 80), "reversed": (60, 60, 230), "rotated": (60, 60, 230), "check": (40, 170, 240),
                  "fault": (60, 60, 230),   # fault: looks like a learned bad example (a known fault)
                  "ns": (150, 150, 150)}    # ns: not stuffed, left off this board on purpose (not a fault)
FINE = ("ok", "ns")         # statuses that need no answer
ALL_BOARDS = "*"            # the not-stuffed key for every board of a design


def root(data: Path) -> Path:
    return Path(data) / "checks"


def folder(data: Path, uid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", uid or "") or uid.startswith("."):
        raise ValueError("No such check.")
    return root(data) / uid


def load(data: Path, uid: str) -> dict:
    c = pj._read_json(folder(data, uid) / "check.json")
    if not c or c.get("uid") != uid:
        raise ValueError("No such check.")
    return c


def save(data: Path, c: dict):
    pj._write_json(folder(data, c["uid"]) / "check.json", c)


def uids(data: Path) -> list[str]:
    r = root(data)
    return sorted(d.name for d in r.iterdir() if (d / "check.json").is_file()) if r.is_dir() else []


def file_path(data: Path, uid: str, rel: str) -> Path:
    d = folder(data, uid).resolve()
    f = (d / rel).resolve()
    if d not in f.parents or not f.is_file():
        raise ValueError("No such file.")
    return f


# ---- references ---------------------------------------------------------------------------

def reference_sides(data: Path, uid: str) -> dict:
    """{side: number of parts the reference photo can check} for a project's lined-up sides."""
    d = pj.folder(data, uid)
    out = {}
    for side in pj.SIDES:
        placed = pj._read_json(d / side / "parts_full.json", []) if (d / side / "alignment.json").is_file() else []
        n = sum(1 for r in placed if r.get("checkable") and "via" not in r)
        if n and (d / side / "reference.jpg").is_file():
            out[side] = n
    return out


def references(data: Path) -> list[dict]:
    """Projects marked as references, newest first."""
    out = []
    for uid in pj.uids(data):
        p = pj._read_json(pj.folder(data, uid) / "project.json", {}) or {}
        if p.get("reference"):
            sides = reference_sides(data, uid)
            out.append({"uid": uid, "board": p["board"], "serial": p.get("serial", ""), "created": p.get("created", ""),
                        "marked": p.get("reference_at", ""), "sides": sides})
    return sorted(out, key=lambda r: r["marked"] or r["created"], reverse=True)


# ---- learned examples ----------------------------------------------------------------------
# Every answer the tester gives teaches the check: a flagged part answered Right way round is kept as
# a good example of that part, and a part answered Wrong (flagged by the check, or one it missed) as a
# bad example with the tester's note. Later boards of the same design are compared with these as well
# as the reference, so a false alarm confirmed once isn't raised again and a fault seen once is
# flagged as a known fault. When the tester flags a part the check passed, that reference shows the
# part wrong, so its look there no longer counts as right (the reference turned round does instead).
# Clearing an answer forgets its example; the History tab lists them all to look over and delete.

def learned_dir(data: Path, board: str, side: str) -> Path:
    if side not in pj.SIDES:
        raise ValueError("No such side.")
    return Path(data) / "learned" / pj.slug(board) / side


def load_examples(data: Path, board: str, side: str) -> dict:
    """{"examples": [{id, ref, label: good|bad, note, serial, check, date, file}],
    "reference_wrong": {reference uid: [ref, ...]}, "not_stuffed": {serial or ALL_BOARDS: [ref, ...]}}"""
    d = pj._read_json(learned_dir(data, board, side) / "examples.json", {}) or {}
    return {"examples": d.get("examples", []), "reference_wrong": d.get("reference_wrong", {}),
            "not_stuffed": d.get("not_stuffed", {})}


def _save_examples(data: Path, board: str, side: str, d: dict):
    learned_dir(data, board, side).mkdir(parents=True, exist_ok=True)
    pj._write_json(learned_dir(data, board, side) / "examples.json", {"board": board, **d})
    for fn in LEARNED_LISTENERS:
        fn(board, side)


def example_path(data: Path, board: str, side: str, ex_id: str) -> Path:
    ex = next((e for e in load_examples(data, board, side)["examples"] if e["id"] == ex_id), None)
    f = learned_dir(data, board, side) / ex["file"] if ex else None
    if not f or not f.is_file():
        raise ValueError("No such example.")
    return f


def add_example(data: Path, board: str, side: str, aligned, box, ref: str, label: str, note: str = "",
                serial: str = "", check: str = "", reason: str = "", source: str = "") -> str | None:
    """Keeps an area of a lined-up photo as a good or bad example of a part. Its id, or None.
    reason: why a bad one is wrong (a REASONS key); source: the photo it came from."""
    x, y, w, h = (int(v) for v in box)
    crop = aligned[max(0, y):y + h, max(0, x):x + w]
    if crop.size == 0:
        return None
    d = load_examples(data, board, side)
    ex_id = f"{pj.slug(ref)}-{label}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    learned_dir(data, board, side).mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(learned_dir(data, board, side) / f"{ex_id}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
    d["examples"].append({"id": ex_id, "ref": ref, "label": label, "note": (note or "").strip()[:500], "serial": serial,
                          "check": check, "date": pj.now()[:16], "file": f"{ex_id}.jpg",
                          "reason": reason if label == "bad" and reason in REASONS else "", "source": source})
    same = [e for e in d["examples"] if e["ref"] == ref and e["label"] == label]
    for old in same[:-MAX_EXAMPLES]:
        _drop(data, board, side, d, old["id"])
    _save_examples(data, board, side, d)
    return ex_id


def _drop(data, board, side, d, ex_id) -> bool:
    ex = next((e for e in d["examples"] if e["id"] == ex_id), None)
    if ex:
        d["examples"].remove(ex)
        (learned_dir(data, board, side) / ex["file"]).unlink(missing_ok=True)
    return bool(ex)


def delete_example(data: Path, board: str, side: str, ex_id: str):
    d = load_examples(data, board, side)
    if _drop(data, board, side, d, ex_id):
        _save_examples(data, board, side, d)


def set_note(data: Path, board: str, side: str, ex_id: str, note: str, reason: str | None = None):
    d = load_examples(data, board, side)
    ex = next((e for e in d["examples"] if e["id"] == ex_id), None)
    if ex:
        ex["note"] = note.strip()[:500]
        if reason is not None:
            ex["reason"] = reason if ex["label"] == "bad" and reason in REASONS else ""
        _save_examples(data, board, side, d)


def set_reference_wrong(data: Path, board: str, side: str, reference: str, ref: str, wrong: bool = True):
    d = load_examples(data, board, side)
    refs = set(d["reference_wrong"].get(reference, []))
    refs = refs | {ref} if wrong else refs - {ref}
    if refs:
        d["reference_wrong"][reference] = sorted(refs)
    else:
        d["reference_wrong"].pop(reference, None)
    _save_examples(data, board, side, d)


def ns_key(serial: str, uid: str) -> str:
    """Which board a not-stuffed answer is about: its serial, or the photo's check or project when it has none."""
    return (serial or "").strip() or f"#{uid}"


def set_not_stuffed(data: Path, board: str, side: str, key: str, ref: str, on: bool = True):
    """Marks a part not stuffed (NS: left off on purpose, not a fault) on one board (key from ns_key),
    or on every board of the design (key ALL_BOARDS); on=False undoes it."""
    d = load_examples(data, board, side)
    refs = set(d["not_stuffed"].get(key, []))
    refs = refs | {ref} if on else refs - {ref}
    if refs:
        d["not_stuffed"][key] = sorted(refs)
    else:
        d["not_stuffed"].pop(key, None)
    _save_examples(data, board, side, d)


def not_stuffed(data: Path, board: str, side: str, key: str) -> dict:
    """{ref: "board" | "all"}: the parts not stuffed on this board, and on every board of the design."""
    ns = load_examples(data, board, side)["not_stuffed"]
    return {r: "board" for r in ns.get(key, [])} | {r: "all" for r in ns.get(ALL_BOARDS, [])}


def ns_why(scope: str) -> str:
    return "not stuffed on any board of this design" if scope == "all" else "not stuffed on this board"


def taught_refs(data: Path, board: str, side: str) -> set:
    """Parts with at least one learned example on this side."""
    return {e["ref"] for e in load_examples(data, board, side)["examples"]}


def board_profile(data: Path, board: str) -> dict:
    return pj._read_json(Path(data) / "boards" / pj.slug(board) / "profile.json", {}) or {}


def pending_refs(data: Path, board: str, side: str) -> set:
    """Parts added by hand to wait for a new photo, not yet taught on this side. Which side a part is
    on isn't known here, so a ref on the other side is simply never found among this side's parts."""
    extra = board_profile(data, board).get("extra") or {}
    return {r for r, e in extra.items() if e.get("wait")} - taught_refs(data, board, side)


def reason_text(ex: dict) -> str:
    return REASONS.get(ex.get("reason") or "", "")


def box_round(box):
    """A part's box with some board round it, as check_part compares it."""
    x, y, w, h = (int(round(v)) for v in box)
    e = max(3, int(0.3 * min(w, h)))
    return [max(0, x - e), max(0, y - e), w + 2 * e, h + 2 * e]


def example_images(data: Path, board: str, side: str) -> dict:
    """{ref: {"good": [(image, example)], "bad": [(image, example)]}} for check_part."""
    out = {}
    for e in load_examples(data, board, side)["examples"]:
        im = cv2.imread(str(learned_dir(data, board, side) / e["file"]))
        if im is not None:
            out.setdefault(e["ref"], {"good": [], "bad": []})[e["label"]].append((im.astype(np.float32), e))
    return out


def _learned_match(area, ex, size, either_way: bool = False):
    """How well a learned example matches a part: the best fit near where it should be, or -1 when
    the example's polarity mark points the other way (so a reversed part's example doesn't stand for
    the part the right way round, and the other way about). either_way: a non-polarised part, which
    matches its example turned round as well."""
    w, h = size
    t = ex if (ex.shape[1], ex.shape[0]) == (w, h) else cv2.resize(ex, (w, h), interpolation=cv2.INTER_AREA)
    if area.shape[0] < h or area.shape[1] < w or float(t.std()) < 1e-3:
        return -1.0
    if either_way:
        return max(float(cv2.minMaxLoc(cv2.matchTemplate(area, v, cv2.TM_CCOEFF_NORMED))[1])
                   for v in (t, cv2.rotate(t, cv2.ROTATE_180)))
    _, s, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(area, t, cv2.TM_CCOEFF_NORMED))
    anti = (t - cv2.rotate(t, cv2.ROTATE_180)) / 2
    if float(anti.std() / (t.std() + 1e-6)) > MARK_STRENGTH:
        patch = area[by:by + h, bx:bx + w]
        if _ncc((patch - cv2.rotate(patch, cv2.ROTATE_180)) / 2, anti) <= 0:
            return -1.0
    return float(s)


# ---- lining up and comparing ----------------------------------------------------------------

def _gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def align(photo: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray | None, str]:
    """The homography from the photo to the reference photo, or None and why not."""
    s = min(1.0, ALIGN_WIDTH / ref.shape[1])
    g_ref = cv2.resize(_gray(ref), None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    sp = g_ref.shape[1] / photo.shape[1]
    g_ph = cv2.resize(_gray(photo), None, fx=sp, fy=sp, interpolation=cv2.INTER_AREA)
    orb = cv2.ORB_create(FEATURES)
    k1, d1 = orb.detectAndCompute(g_ref, None)
    k2, d2 = orb.detectAndCompute(g_ph, None)
    if d1 is None or d2 is None or len(k1) < 50 or len(k2) < 50:
        return None, "The photo has too little detail to line up. Check it's in focus and well lit."
    matches = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d2, d1), key=lambda m: m.distance)[:2000]
    if len(matches) < 40:
        return None, "The photo doesn't look like the reference. Check it's the right side of the board."
    src = np.float32([k2[m.queryIdx].pt for m in matches]) / sp
    dst = np.float32([k1[m.trainIdx].pt for m in matches]) / s
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0 / s)
    n_in = int(inl.sum()) if inl is not None else 0
    if H is None or n_in < MIN_MATCHES or n_in / len(matches) < 0.15:
        return None, (f"The photo couldn't be lined up with the reference automatically ({n_in} matching features). "
                      "Click the fiducials instead, or retake it lined up with the ghost.")
    det = np.linalg.det(H[:2, :2]) * (photo.shape[1] / ref.shape[1]) ** 2
    if not 0.4 < det < 2.5:
        return None, "The photo lined up with the reference only by stretching it a lot. Click the fiducials instead."
    return H, f"Lined up with the reference using {n_in} matching features."


def _similarity(src, dst):
    return np.vstack([ph._similarity(np.float32(src), np.float32(dst)), [0, 0, 1]])


def homography_from_points(src, dst) -> np.ndarray:
    src, dst = np.float32(src), np.float32(dst)
    if len(src) == 2:
        return _similarity(src, dst)
    if len(src) == 3:
        return np.vstack([cv2.getAffineTransform(src, dst), [0, 0, 1]])
    H, _ = cv2.findHomography(src, dst, 0)
    if H is None:
        raise ValueError("Those points can't be lined up (are three of them in a line?).")
    return H


def _ncc(a, b) -> float:
    a, b = a - a.mean(), b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def check_part(aligned, ref, part: dict, margin: int, learned: dict | None = None, reference_wrong: bool = False) -> dict:
    """Compares a part on the lined-up photo with the reference.

    The part is first found near where the reference has it, as placed or turned round, whichever
    fits best. Then what changes when it's turned round (a band, a + or a pin 1 dot: the difference
    between the part and itself turned round) is compared with the reference's: it correlates
    positively when the part is the same way round, negatively when it's reversed, and near zero
    when the mark can't be seen.

    Then the learned examples of the part, {"good": [(image, example)], "bad": [...]}, are compared
    too: a part that looks like a known fault is flagged "fault"; a flagged part that looks like one
    the tester confirmed right way round passes. reference_wrong: a tester said the reference shows
    this part wrong, so the reference turned round is its right look."""
    x, y, w, h = (int(round(v)) for v in part["box"])
    # with some of the board round the part, so the template can't slide off it and still match
    e = max(3, int(0.3 * min(w, h)))
    x, y = max(0, x - e), max(0, y - e)
    w, h = min(ref.shape[1] - x, w + 2 * e), min(ref.shape[0] - y, h + 2 * e)
    # in colour: a dark band can be the same grey as the solder mask around it
    body = ref[y:y + h, x:x + w].astype(np.float32)
    H, W = aligned.shape[:2]
    ax, ay = max(0, x - margin), max(0, y - margin)
    area = aligned[ay:min(H, y + h + margin), ax:min(W, x + w + margin)].astype(np.float32)
    out = {"ref": part["ref"], "kind": part["kind"], "value": part.get("value", ""), "box": [int(round(v)) for v in part["box"]],
           "status": "check", "score": 0.0, "turn": 0, "turned": 0.0, "mark": 0.0, "strength": 0.0, "why": "not found on the photo", "answer": None}
    if body.size == 0 or area.shape[0] < h or area.shape[1] < w:
        return out
    turned = cv2.rotate(body, cv2.ROTATE_180)
    anti = (body - turned) / 2
    if float(body.std()) < 1e-3:
        return out
    # a part turned round sits on the same pads, so it's found with the turned template
    best = max((cv2.minMaxLoc(cv2.matchTemplate(area, t, cv2.TM_CCOEFF_NORMED))[1::2] for t in (body, turned)), key=lambda v: v[0])
    bx, by = best[1]
    out["at"] = [ax + bx, ay + by, w, h]    # where the part (with some board round it) is on the lined-up photo
    patch = area[by:by + h, bx:bx + w]
    pt = cv2.rotate(patch, cv2.ROTATE_180)
    same, rev = _ncc(patch, body), _ncc(patch, turned)
    # how strongly the reference part shows which way round it is, against its overall contrast
    strength = float(anti.std() / (body.std() + 1e-6))
    mark = _ncc((patch - pt) / 2, anti) if strength > MARK_STRENGTH else 0.0
    out.update(score=round(same, 3), turned=round(rev, 3), turn=180, mark=round(mark, 3), strength=round(strength, 3))
    best_turn, best_quarter = 0, -1.0
    if part["kind"] == "ic" and 0.8 < w / max(h, 1) < 1.25:   # square ICs can also go in a quarter turn out
        for t, code in ((90, cv2.ROTATE_90_CLOCKWISE), (270, cv2.ROTATE_90_COUNTERCLOCKWISE)):
            q = cv2.rotate(body, code)
            if q.shape == patch.shape:
                v = _ncc(patch, q)
                if v > best_quarter:
                    best_turn, best_quarter = t, v
    if part["kind"] == "look":    # no polarity mark: only whether it looks like the reference part
        out.update(mark=0.0, status="ok" if max(same, rev) >= MIN_MATCH else "check",
                   why="" if max(same, rev) >= MIN_MATCH else "doesn't look like the reference part: missing, a different part, or out of place")
    elif best_quarter > max(same, rev) + REVERSE_MARGIN:
        out.update(status="rotated", turn=best_turn, turned=round(best_quarter, 3), why=f"looks turned {best_turn}°")
    elif max(same, rev) < MIN_MATCH:
        out.update(status="check", why="doesn't look like the reference part: missing, a different part, or out of place")
    elif mark <= -MARK_MIN:
        out.update(status="reversed", why="its mark is at the other end from the reference")
    elif mark >= MARK_MIN:
        out.update(status="ok", why="")
    elif strength <= MARK_STRENGTH:
        out.update(status="check", why="no mark shows on the reference photo, so look at it by eye")
    else:
        out.update(status="check", why="its mark can't be made out in this photo")
    if reference_wrong and part["kind"] != "look" and out["status"] in ("ok", "reversed"):
        out.update(status="ok", why="the right way round: the reference shows this part wrong") if out["status"] == "reversed" else \
            out.update(status="reversed", why="looks like the reference, which shows this part the wrong way round")
    learned = learned or {}
    look = part["kind"] == "look"
    good = [(_learned_match(area, im, (w, h), look), e) for im, e in learned.get("good", [])]
    bad = [(_learned_match(area, im, (w, h), look), e) for im, e in learned.get("bad", [])]
    # the closest right look: confirmed-good examples and the reference (turned round if it shows the part wrong)
    best_good = max([v for v, _ in good] + [max(same, rev) if look else rev if reference_wrong else same])
    best_bad, bad_ex = max(bad, key=lambda v: v[0]) if bad else (-1.0, None)
    if best_bad >= MIN_MATCH and best_bad > best_good + LEARNED_MARGIN:
        seen = " ".join(v for v in (bad_ex.get("serial"), bad_ex.get("date", "")[:10]) if v)
        said = ": ".join(v for v in (reason_text(bad_ex), bad_ex.get("note", "")) if v)
        out.update(status="fault", learned="bad", why=f"looks like a fault seen before ({seen})" + (f": {said}" if said else ""))
    elif out["status"] != "ok" and good:
        g, ex = max(good, key=lambda v: v[0])
        if g >= MIN_MATCH and g > best_bad + LEARNED_MARGIN:
            seen = " ".join(v for v in (ex.get("serial"), ex.get("date", "")[:10]) if v)
            out.update(status="ok", learned="good", why=f"looks like a part confirmed right way round ({seen})")
    return out


def overlay(img, results, width: int = 1600) -> bytes:
    """The lined-up photo with each part outlined: green fine, red reversed, orange to look at.
    A flagged part answered "correct" turns green, "wrong" (or a missed part) red."""
    s = min(1.0, width / img.shape[1])
    vis = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    t = max(2, int(3 * vis.shape[1] / 1600))
    for r in results:
        x, y, w, h = (int(v * s) for v in r["box"])
        st = {"correct": "ok", "wrong": "reversed"}.get(r.get("answer"), r["status"])
        if st == "ns":      # not stuffed: a grey dashed box, labelled NS
            ph.dashed_poly(vis, [(x - 3, y - 3), (x + w + 3, y - 3), (x + w + 3, y + h + 3), (x - 3, y + h + 3)], STATUS_COLOURS[st], t)
            cv2.putText(vis, f"{r['ref']} NS", (x, max(14, y - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * vis.shape[1] / 1600 + 0.2,
                        STATUS_COLOURS[st], 2)
            continue
        cv2.rectangle(vis, (x - 3, y - 3), (x + w + 3, y + h + 3), STATUS_COLOURS[st], t)
        if r["status"] != "ok" or r.get("answer"):
            cv2.putText(vis, r["ref"], (x, max(14, y - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * vis.shape[1] / 1600 + 0.2,
                        STATUS_COLOURS[st], 2)
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


def crop(img, box, size: int = 220) -> bytes:
    x, y, w, h = box
    pad = max(10, int(0.6 * max(w, h)))
    H, W = img.shape[:2]
    c = img[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
    k = size / max(c.shape[:2])
    c = cv2.resize(c, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


def result_of(c: dict) -> str:
    """FAIL if any part was answered wrong; PASS once at least one side is checked and every
    flagged part is answered; otherwise OPEN."""
    rs = [r for s in c["sides"].values() for r in s.get("results", [])]
    if any(r.get("answer") == "wrong" for r in rs):
        return "FAIL"
    done = [s for s in c["sides"].values() if s.get("aligned")]
    if done and all(r["status"] in FINE or r.get("answer") for r in rs):
        return "PASS"
    return "OPEN"


def row(c: dict) -> dict:
    """One line about a check for the history list (the same shape the database gives)."""
    out = {k: c.get(k, "") for k in ("uid", "board", "reference", "serial", "notes", "station", "created", "updated", "result")}
    for side in pj.SIDES:
        s = c["sides"].get(side, {})
        rs = s.get("results", [])
        out[side] = {"aligned": bool(s.get("aligned")), "checked": len(rs),
                     "flagged": sum(1 for r in rs if r["status"] not in FINE),
                     "wrong": [r["ref"] for r in rs if r.get("answer") == "wrong"]}
    return out


# ---- the check being done ---------------------------------------------------------------------

class Checks:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.cur = None             # check.json of the check open on the page
        self.on_change = None       # called with a check's uid whenever it changes
        self._img = {}              # (uid, side, name) -> image, for crops

    def _touch(self, what: str | None = None):
        c = self.cur
        c["updated"] = pj.now()
        if what:
            c["log"].append({"at": c["updated"], "what": what})
        c["result"] = result_of(c)
        save(self.data, c)
        if self.on_change:
            self.on_change(c["uid"])

    def start(self, ref_uid: str, serial: str = "", notes: str = ""):
        p = pj.load(self.data, ref_uid)
        sides = reference_sides(self.data, ref_uid)
        if not p.get("reference") or not sides:
            raise ValueError("That project isn't a reference with a lined-up photo.")
        when = datetime.now()
        base = f"{when:%Y%m%d-%H%M%S}-{pj.slug(p['board'])}"[:72]
        uid, n = base, 2
        while folder(self.data, uid).exists():
            uid, n = f"{base}-{n}", n + 1
        folder(self.data, uid).mkdir(parents=True)
        stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        self.cur = {"uid": uid, "board": p["board"], "reference": ref_uid, "serial": serial.strip()[:128],
                    "notes": notes[:5000], "station": pj.STATION, "created": stamp, "updated": stamp,
                    "result": "OPEN", "sides": {s: {} for s in sides}, "log": []}
        self._img.clear()
        self._touch(f"Started a check against reference {ref_uid}" + (f", serial {self.cur['serial']}" if self.cur["serial"] else ""))

    def open(self, uid: str):
        self.cur = load(self.data, uid)
        self._img.clear()

    def close(self):
        self.cur = None
        self._img.clear()

    def _need(self, side):
        if not self.cur:
            raise ValueError("Start a check first.")
        if side not in self.cur["sides"]:
            raise ValueError(f"The reference has no lined-up {side} photo.")
        return self.cur["sides"][side]

    def _ref(self, side):
        d = pj.folder(self.data, self.cur["reference"])
        key = ("ref", self.cur["reference"], side)
        if key not in self._img:
            self._img = {k: v for k, v in self._img.items() if k[0] != "ref"}
            self._img[key] = cv2.imread(str(d / side / "reference.jpg"))
        return self._img[key]

    def _ref_parts(self, side, taught=frozenset()):
        """The reference's parts this side checks, and those it skips. A part a tester has taught
        (or one added to wait for a photo) is checked even when it's small in the photo."""
        d = pj.folder(self.data, self.cur["reference"])
        placed = pj._read_json(d / side / "parts_full.json", [])
        small = lambda r: "via" in r or (r.get("reason") or "").startswith("too small")
        ok = lambda r: (r.get("checkable") and "via" not in r) or (r["ref"] in taught and small(r))
        use = [r for r in placed if ok(r)]
        skipped = [{"ref": r["ref"], "kind": r.get("kind", ""), "why": "needs a closeup; look at it by eye" if "via" in r
                    else r.get("reason") or "can't be checked from the photo"}
                   for r in placed if not ok(r)]
        return use, skipped

    def photo(self, side, data: bytes):
        st = self._need(side)
        img = ph.decode(data)
        d = folder(self.data, self.cur["uid"]) / side
        d.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(d / "photo.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        for f in ("aligned.jpg", "result.jpg"):
            (d / f).unlink(missing_ok=True)
        self._img.pop((self.cur["uid"], side, "photo"), None)
        self._img[(self.cur["uid"], side, "photo")] = img
        st.clear()
        st.update(photo=True, width=img.shape[1], height=img.shape[0])
        H, msg = align(img, self._ref(side))
        if H is not None and self._run(side, H, msg):
            return
        # then the corner fiducials, found by colour
        try:
            a = self.anchors(side)
            d = pj.folder(self.data, self.cur["reference"])
            design = pj._read_json(d / "design.json", {})
            orient = (pj._read_json(d / side / "alignment.json", {}) or {}).get("orient") or ph.DEFAULT_ORIENT[side]
            found = ph.auto_fiducials(img, ph.side_fiducials(design, side), orient)
        except ValueError:
            found = None
        if found and self._run(side, homography_from_points([found[r] for r in found], [a["on_ref"][r] for r in found]),
                               f"Lined up from the {len(found)} corner fiducials, found automatically."):
            return
        st.update(aligned=False, results=[], message=msg if H is None else
                  "The photo lined up, but the parts don't match the reference. Is it the right side, and the same board type?",
                  need_points=True)
        self._touch(f"{side.capitalize()} photo taken; not lined up automatically")

    def anchors(self, side) -> dict:
        """The fiducials to click when a photo can't be lined up automatically, with where each is
        on the reference photo."""
        self._need(side)
        d = pj.folder(self.data, self.cur["reference"])
        design = pj._read_json(d / "design.json", {})
        a = pj._read_json(d / side / "alignment.json", {})
        if not design or not a.get("mapping"):
            raise ValueError("The reference has no lined-up photo of this side.")
        fids = ph.side_fiducials(design, side)
        if len(fids) < 2:
            raise ValueError("This side has fewer than 2 fiducials, so the photo has to line up automatically. "
                             "Retake it lined up with the ghost.")
        m = ph.Mapping.from_dict(a["mapping"])
        pts = {r: [f["x"], f["y"]] for r, f in fids.items()}
        on_ref = {r: [float(v) for v in m.apply([xy])[0]] for r, xy in pts.items()}
        return {"prompts": ph.anchor_prompts(pts, a.get("orient") or m.orient), "on_ref": on_ref}

    def snap(self, side, xy):
        img = self._photo(side)
        return ph.snap(img, xy)

    def align_points(self, side, points: dict):
        st = self._need(side)
        on_ref = self.anchors(side)["on_ref"]
        use = {r: xy for r, xy in points.items() if r in on_ref and xy}
        if len(use) < 2:
            raise ValueError("Click at least 2 fiducials.")
        H = homography_from_points([use[r] for r in use], [on_ref[r] for r in use])
        if not self._run(side, H, f"Lined up from {len(use)} fiducials you clicked.", force=True):
            raise ValueError("The parts don't match the reference with those points. Check the order, and that it's the right side.")
        st["points"] = use

    def _photo(self, side):
        key = (self.cur["uid"], side, "photo")
        if key not in self._img:
            img = cv2.imread(str(folder(self.data, self.cur["uid"]) / side / "photo.jpg"))
            if img is None:
                raise ValueError("Take the photo first.")
            self._img[key] = img
        return self._img[key]

    def _run(self, side, H, msg, force=False) -> bool:
        """Compares every part through H (photo -> reference). False, and nothing kept, if most
        parts don't look like the reference either way round: then the lining up is wrong."""
        st = self.cur["sides"][side]
        ref = self._ref(side)
        aligned = cv2.warpPerspective(self._photo(side), np.asarray(H, np.float64), (ref.shape[1], ref.shape[0]))
        board = self.cur["board"]
        pending = pending_refs(self.data, board, side)
        parts, skipped = self._ref_parts(side, taught_refs(self.data, board, side) | pending)
        margin = max(8, int(0.004 * ref.shape[1]))
        learned = example_images(self.data, board, side)
        wrong = set(load_examples(self.data, board, side)["reference_wrong"].get(self.cur["reference"], []))
        results = [check_part(aligned, ref, p, margin, learned.get(p["ref"]), p["ref"] in wrong) for p in parts]
        self._models(side, aligned, results)
        ns = self._ns(side)
        usual = [r for r in results if r["ref"] not in pending and r["ref"] not in ns]
        if usual and np.median([max(r["score"], r["turned"]) for r in usual]) < 0.45 and not (force and len(usual) < 3):
            return False
        for r in results:
            if r["ref"] in ns:           # not stuffed on purpose: not a fault, nothing to ask
                r.update(seen={"status": r["status"], "why": r["why"]}, status="ns", why=ns_why(ns[r["ref"]]))
            elif r["ref"] in pending:    # added by hand to wait for a photo: always asked until taught
                r.update(status="check", new=True, why="new part, teach me: is it right?")
        d = folder(self.data, self.cur["uid"]) / side
        cv2.imwrite(str(d / "aligned.jpg"), aligned, [cv2.IMWRITE_JPEG_QUALITY, 92])
        self._img[(self.cur["uid"], side, "aligned")] = aligned
        st.update(aligned=True, need_points=False, message=msg, H=np.asarray(H).tolist(), results=results, skipped=skipped)
        (d / "result.jpg").write_bytes(overlay(aligned, results))
        flagged = [r["ref"] for r in results if r["status"] not in FINE]
        self._touch(f"{side.capitalize()} checked: {len(results)} parts, "
                    + (f"{len(flagged)} flagged ({', '.join(flagged[:12])}{' ...' if len(flagged) > 12 else ''})" if flagged else "none flagged"))
        return True

    def _models(self, side, aligned, results):
        """The trained models' opinion of each part (models.py), kept beside the check's own answer."""
        try:
            from . import models as md
            from . import teach as tl
            d = tl.design_for(self.data, self.cur["reference"])
            m = tl._mapping(self.data, self.cur["reference"], side)
            for r in results:
                g = tl.part_geom(d, r["ref"])
                op = md.opinion(self.data, self.cur["board"], aligned, m, g, d["rot_sign"], r["kind"]) if g else {}
                if op:
                    r.update(model=op, model_text=md.summary(op))
        except Exception:   # noqa: BLE001 - an extra opinion: the check stands without it
            import traceback
            traceback.print_exc()

    def _ns(self, side) -> dict:
        return not_stuffed(self.data, self.cur["board"], side, ns_key(self.cur.get("serial", ""), self.cur["uid"]))

    def set_ns(self, side, ref, on: bool = True, everywhere: bool = False):
        """NS: the part is not stuffed on this board (everywhere: on every board of the design from now
        on), so it isn't a fault and isn't asked about; on=False undoes it (both). An answer already
        given for it is cleared, with its example."""
        st = self._need(side)
        c, board = self.cur, self.cur["board"]
        key = ns_key(c.get("serial", ""), c["uid"])
        if on:
            set_not_stuffed(self.data, board, side, ALL_BOARDS if everywhere else key, ref)
        else:
            for k in (key, ALL_BOARDS):
                set_not_stuffed(self.data, board, side, k, ref, False)
        r = next((r for r in st.get("results", []) if r["ref"] == ref), None)
        if r:
            if on and r.get("answer"):
                self.answer(side, ref, None)
            ns = self._ns(side)
            if ref in ns:
                r.setdefault("seen", {"status": r["status"], "why": r["why"]})
                r.update(status="ns", why=ns_why(ns[ref]))
            elif "seen" in r:
                r.update(r.pop("seen"))
            (folder(self.data, c["uid"]) / side / "result.jpg").write_bytes(overlay(self._aligned(c["uid"], side), st["results"]))
        self._touch(f"{side.capitalize()} {ref}: " + (("NOT STUFFED on every board of this design" if everywhere
                                                      else "NOT STUFFED on this board") if on else "NS undone, checked again"))

    def answer(self, side, ref, answer, note=None, missed=False, reason=None):
        """The tester's answer for a part, which also teaches the check (see learned examples). A
        part the check passed can be answered wrong: it missed it. A changed or cleared answer
        forgets the example the old one made."""
        st = self._need(side)
        if answer not in ("correct", "wrong", None, ""):
            raise ValueError("Answer correct or wrong.")
        r = next((r for r in st.get("results", []) if r["ref"] == ref), None)
        if not r:
            raise ValueError(f"{ref} isn't checked on this side.")
        answer = answer or None
        c, board = self.cur, self.cur["board"]
        if answer and r["status"] == "ns":
            raise ValueError(f"{ref} is marked not stuffed: undo NS first.")
        if r["status"] != "ok" or answer == "correct":
            missed = False
        elif answer is None:
            missed = r.get("missed", False)
        note = (r.get("note", "") if note is None else note).strip()[:500]
        reason = (r.get("reason", "") if reason is None else reason) if answer == "wrong" else ""
        reason = reason if reason in REASONS else ""
        if answer == r.get("answer") and r.get("example"):   # the same answer again: only the note and reason can change
            if note != r.get("note", "") or reason != r.get("reason", ""):
                r["note"], r["reason"] = note, reason
                set_note(self.data, board, side, r["example"], note, reason)
                self._touch(f"{side.capitalize()} {ref}: note set to “{note}”" + (f" ({REASONS[reason]})" if reason else "")
                            if note or reason else f"{side.capitalize()} {ref}: note cleared")
            return
        if r.get("example"):
            delete_example(self.data, board, side, r.pop("example"))
        if r.pop("missed", False):
            set_reference_wrong(self.data, board, side, c["reference"], ref, False)
        r["answer"], r["note"], r["reason"] = answer, note if answer else "", reason
        img = self._aligned(c["uid"], side)
        if answer:
            box = r.get("at") or box_round(r["box"])
            r["example"] = add_example(self.data, board, side, img, box, ref, "good" if answer == "correct" else "bad",
                                       r["note"], c.get("serial", ""), c["uid"], reason, f"check/{c['uid']}/{side}")
            if missed:
                r["missed"] = True
                set_reference_wrong(self.data, board, side, c["reference"], ref)
        (folder(self.data, c["uid"]) / side / "result.jpg").write_bytes(overlay(img, st["results"]))
        what = {"correct": "looked at, it's the right way round", "wrong": "WRONG way round"}.get(answer) or "answer cleared"
        if missed and answer:
            what = "WRONG, though the check passed it (the reference's look of it is no longer trusted)"
        said = "; ".join(v for v in (REASONS.get(reason, ""), r["note"]) if v)
        self._touch(f"{side.capitalize()} {ref}: {what}" + (f" ({said})" if answer and said else ""))

    def learned(self, board: str, side: str) -> dict:
        """What the check has learned for a board design's side, for the page."""
        d = load_examples(self.data, board, side)
        return {"board": board, "side": side, "folder": str(learned_dir(self.data, board, side).resolve()),
                "examples": sorted(d["examples"], key=lambda e: e["date"], reverse=True),
                "reference_wrong": d["reference_wrong"]}

    def learned_boards(self) -> list[dict]:
        """Board designs with a reference or anything learned, with how many examples each side has."""
        names = {r["board"] for r in references(self.data)} | ({self.cur["board"]} if self.cur else set())
        lr = Path(self.data) / "learned"
        for f in lr.glob("*/*/examples.json") if lr.is_dir() else ():
            names.add((pj._read_json(f, {}) or {}).get("board") or f.parent.parent.name)
        out = []
        for n in sorted(names):
            row = {"board": n}
            for s in pj.SIDES:
                d = load_examples(self.data, n, s)
                row[s] = {"good": sum(e["label"] == "good" for e in d["examples"]),
                          "bad": sum(e["label"] == "bad" for e in d["examples"]),
                          "reference_wrong": sum(len(v) for v in d["reference_wrong"].values())}
            out.append(row)
        return out

    def forget(self, board: str, side: str, ex_id: str):
        """Deletes a learned example, and the answer that made it stops pointing at it."""
        delete_example(self.data, board, side, ex_id)
        if self.cur and self.cur["board"] == board:
            for r in self.cur["sides"].get(side, {}).get("results", []):
                if r.get("example") == ex_id:
                    r.pop("example")
            save(self.data, self.cur)

    def trust_reference(self, board: str, side: str, reference: str, ref: str):
        set_reference_wrong(self.data, board, side, reference, ref, False)

    def set_info(self, uid, serial=None, notes=None):
        same = self.cur and self.cur["uid"] == uid
        keep = self.cur
        self.cur = keep if same else load(self.data, uid)
        try:
            said = []
            if serial is not None and serial.strip() != self.cur.get("serial", ""):
                self.cur["serial"] = serial.strip()[:128]
                said.append(f"Serial set to {self.cur['serial']}" if self.cur["serial"] else "Serial cleared")
            if notes is not None and notes != self.cur.get("notes", ""):
                self.cur["notes"] = notes[:5000]
                said.append("Notes changed")
            if said:
                self._touch("; ".join(said))
            return self.cur
        finally:
            if not same:
                self.cur = keep

    def _aligned(self, uid, side):
        key = (uid, side, "aligned")
        if key not in self._img:
            img = cv2.imread(str(folder(self.data, uid) / side / "aligned.jpg"))
            if img is None:
                raise ValueError("That side wasn't lined up.")
            if len(self._img) > 6:
                self._img = {k: v for k, v in self._img.items() if self.cur and k[0] == self.cur["uid"]}
            self._img[key] = img
        return self._img[key]

    def crop(self, uid, side, ref, which) -> bytes:
        c = self.cur if self.cur and self.cur["uid"] == uid else load(self.data, uid)
        r = next((r for r in c["sides"].get(side, {}).get("results", []) if r["ref"] == ref), None)
        if not r:
            raise ValueError("No such part.")
        if which == "reference":
            img = cv2.imread(str(pj.folder(self.data, c["reference"]) / side / "reference.jpg"))
        else:
            img = self._aligned(uid, side)
        if img is None:
            raise ValueError("The image is missing.")
        return crop(img, r["box"])

    def view(self, c: dict) -> dict:
        """A check as the page shows it."""
        ch = {k: c.get(k) for k in ("uid", "board", "reference", "serial", "notes", "station", "created", "updated", "result")}
        ch["sides"] = {s: {k: v for k, v in st.items() if k != "H"} for s, st in c["sides"].items()}
        ch["log"] = list(reversed(c["log"]))
        try:
            ref = pj.load(self.data, c["reference"])
            ch["reference_serial"] = ref.get("serial", "")
        except ValueError:
            ch["reference_serial"] = None    # the reference project was removed
        ch["folder"] = str(folder(self.data, c["uid"]).resolve())
        return ch

    def detail(self, uid: str) -> dict:
        return self.view(self.cur if self.cur and self.cur["uid"] == uid else load(self.data, uid))

    def state(self) -> dict:
        return {"references": references(self.data), "check": self.view(self.cur) if self.cur else None}
