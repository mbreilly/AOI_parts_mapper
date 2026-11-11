"""Models trained on this machine, one for each job of the check, from what testers have taught it.
They give an extra opinion, with a confidence, next to the board check (check.py); they don't pass
or fail a part.

    missing       is the part there?
    polarity      is a polarised part (IC, tantalum, electrolytic, diode, LED) the right way round?
    orientation   is a square part turned a quarter or half turn?

What each one learns from, per board design:
    - the learned examples (learned/<design>/<side>/examples.json): GOOD (there, right way round),
      BAD reversed (there, reversed), BAD missing (not there), other BAD answers (there);
    - parts answered NS (not stuffed): not there;
    - the reference board(s) of the design, checked by hand: its parts in the check are the right way
      round, its fitted parts are there and its DNP parts are not.
Each part is cut from its lined-up photo in its own frame (turned upright, pin 1 the same way every
time), so a part turned round, or a quarter turn, is the same picture turned: that is how reversed
and turned examples are made from right ones.

Each picture is described by a small pretrained image network (MobileNetV2 0.5, ImageNet weights,
through onnxruntime) when it is installed (python -m aoimap.models setup), plus simple colour and
shape measures; without it by those measures alone. A logistic regression on top is each job's model.
Accuracy is measured on parts kept out of training (the examples of each part are held out together,
in 5 rounds).

    <data>/models/feature_extractor.onnx         the image network (setup)
    <data>/models/<design>/<job>.npz             each job's model
    <data>/models/<design>/metrics.json          counts, accuracy and when each was trained
    <data>/models/<design>/progress.json         a training run in progress

Training runs as its own process at the lowest priority on one thread (python -m aoimap.models train).
"""

import json
import math
import os
import sys
import time
import traceback
from argparse import ArgumentParser
from pathlib import Path

import cv2
import numpy as np

from . import check as ck
from . import projects as pj
from . import teach as tl

JOBS = ("missing", "polarity", "orientation")
POLARISED = ("ic", "tantalum", "electrolytic", "diode", "led")
CROP = 112                 # parts are cut out at this size (pixels) for the models
MIN_PX = 8                 # a part smaller than this on its photo isn't used
MIN = {"missing": 10, "polarity": 10, "orientation": 6}
MIN_PARTS = 5              # ...from at least this many different parts
USE_ACCURACY = 0.75        # a model gives its opinion only once its accuracy on held-out parts is this or better
PCA_DIMS = 48
FOLDS = 5
EXTRACTOR = "feature_extractor.onnx"
EXTRACTOR_URL = "https://huggingface.co/onnxmodelzoo/mobilenetv2_050_Opset18/resolve/main/mobilenetv2_050_Opset18.onnx"

TEXT = {
    "missing": {"title": "Missing part", "question": "Is the part there?", "pos": "there", "neg": "not there",
                "learns": "Parts answered GOOD or BAD (there), MISSING and NS (not there); also the reference board's "
                          "fitted parts (there) and its DNP parts whose pads look bare (not there): the BOM may be wrong, so "
                          "a DNP part counts only when its pads look empty.",
                "need": f"at least {MIN['missing']} examples of parts there and {MIN['missing']} not there, each from "
                        f"{MIN_PARTS} or more different parts"},
    "polarity": {"title": "Polarity", "question": "Is it the right way round?", "pos": "right way round", "neg": "reversed",
                 "learns": "Polarised parts (ICs, tantalums, electrolytics, diodes, LEDs) answered GOOD or BAD reversed, and "
                           "the reference board's polarised parts. Each one turned round is used as the opposite answer.",
                 "need": f"at least {MIN['polarity']} right-way-round examples from {MIN_PARTS} or more different parts"},
    "orientation": {"title": "Orientation", "question": "Is a square part turned?", "pos": "upright", "neg": "turned",
                    "learns": "Square parts (most ICs) answered GOOD or BAD reversed, and the reference board's, each also "
                              "turned 90°, 180° and 270°.",
                    "need": f"at least {MIN['orientation']} different square parts"},
}


def folder(data: Path, board: str) -> Path:
    return Path(data) / "models" / pj.slug(board)


# ---- cutting a part out of its photo -----------------------------------------------------------

def is_square(g) -> bool:
    x0, y0, x1, y1 = g["bbox"]
    return 0.8 < (x1 - x0) / max(y1 - y0, 1e-9) < 1.25


def cut(img, mapping, g, rot_sign, size: int = CROP, margin: float = 0.25):
    """The part with some board round it, in its own frame (its package's u to the right, v up),
    size x size pixels. None when it is too small on the photo or off it."""
    x0, y0, x1, y1 = g["bbox"]
    m = margin * max(x1 - x0, y1 - y0, 1e-6)
    u0, u1, v0, v1 = x0 - m, x1 + m, y0 - m, y1 + m
    t = math.radians(g["rot"]) * rot_sign
    c, s = math.cos(t), math.sin(t)
    mir = -1 if g.get("mirror") else 1
    board = [[g["x"] + c * mir * u - s * v, g["y"] + s * mir * u + c * v] for u, v in ((u0, v1), (u1, v1), (u0, v0), (u1, v0))]
    p = np.float32(mapping.apply(board))
    wpx, hpx = float(np.linalg.norm(p[1] - p[0])), float(np.linalg.norm(p[2] - p[0]))
    H, W = img.shape[:2]
    if min(wpx, hpx) < MIN_PX * (1 + 2 * margin) or p[:, 0].min() < -2 or p[:, 1].min() < -2 \
            or p[:, 0].max() > W + 2 or p[:, 1].max() > H + 2:
        return None
    n = int(min(512, max(size, wpx, hpx)))        # cut at about the photo's size, then shrink smoothly
    A = cv2.getAffineTransform(p[:3], np.float32([[0, 0], [n, 0], [0, n]]))
    out = cv2.warpAffine(img, A, (n, n), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out if n == size else cv2.resize(out, (size, size), interpolation=cv2.INTER_AREA)


def looks_empty(img, margin: float = 0.25) -> bool:
    """Bare pads: the middle of the part's box (between its pads) is the same colour as the board round
    it. Used before a DNP part on a reference counts as a "not there" example, as the BOM may be wrong."""
    n = img.shape[0]
    ring = int(n * margin / (1 + 2 * margin) * 0.8)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    mask = np.ones((n, n), bool)
    mask[ring:n - ring, ring:n - ring] = False
    board = np.median(lab[mask], 0)
    c0, c1 = int(n * 0.4), int(n * 0.6)
    mid = lab[c0:c1, c0:c1].reshape(-1, 3)
    near = np.linalg.norm(mid - board, axis=1) < 20
    return float(near.mean()) > 0.7


def turn(img, quarters: int):
    """img turned clockwise by quarters x 90 degrees."""
    q = quarters % 4
    return img if q == 0 else cv2.rotate(img, (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE)[q - 1])


# ---- describing a picture -----------------------------------------------------------------------

def _classic(img) -> np.ndarray:
    """Simple measures: a 16x16 grey thumbnail, a colour histogram and gradient directions in 4x4 cells."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    small = cv2.resize(g, (16, 16), interpolation=cv2.INTER_AREA).ravel()
    small = (small - small.mean()) / (small.std() + 1e-3)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256]).ravel()
    hist = hist / (hist.sum() + 1e-6)
    gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
    mag, ang = cv2.cartToPolar(gx, gy)
    b = np.minimum((ang / (2 * np.pi) * 8).astype(int), 7)
    n = img.shape[0] // 4
    hog = []
    for i in range(4):
        for j in range(4):
            sl = (slice(i * n, (i + 1) * n), slice(j * n, (j + 1) * n))
            hog.append(np.bincount(b[sl].ravel(), mag[sl].ravel(), 8))
    hog = np.concatenate(hog)
    hog = hog / (np.linalg.norm(hog) + 1e-6)
    return np.concatenate([small * 0.1, hist, hog]).astype(np.float32)


class Features:
    """Describes part pictures as vectors: the image network's features (when set up) and _classic."""
    MEAN = np.float32([0.485, 0.456, 0.406])
    STD = np.float32([0.229, 0.224, 0.225])

    def __init__(self, data: Path):
        self.sess, self.kind = None, "simple measures"
        f = Path(data) / "models" / EXTRACTOR
        if f.is_file():
            try:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = so.inter_op_num_threads = 1
                self.sess = ort.InferenceSession(str(f), so, providers=["CPUExecutionProvider"])
                self.input = self.sess.get_inputs()[0].name
                self.kind = "MobileNetV2 + simple measures"
            except Exception:   # noqa: BLE001 - not installed, or the file is broken: simple measures only
                traceback.print_exc()
                self.sess = None

    def __call__(self, imgs: list) -> np.ndarray:
        if not imgs:
            return np.zeros((0, 1), np.float32)
        out = np.stack([_classic(im) for im in imgs])
        if self.sess is None:
            return out
        x = np.stack([cv2.cvtColor(im, cv2.COLOR_BGR2RGB) for im in imgs]).astype(np.float32) / 255
        x = ((x - self.MEAN) / self.STD).transpose(0, 3, 1, 2)
        emb = np.concatenate([self.sess.run(None, {self.input: x[i:i + 16]})[0] for i in range(0, len(x), 16)])
        emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-6)
        return np.concatenate([emb, out], 1).astype(np.float32)


def setup(data: Path, url: str = EXTRACTOR_URL) -> Path:
    """Downloads the image network once and keeps only its feature part (needs onnx to cut it)."""
    import urllib.request
    import onnx
    from onnx import TensorProto, helper
    dest = Path(data) / "models" / EXTRACTOR
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    with urllib.request.urlopen(url, timeout=120) as r:
        tmp.write_bytes(r.read())
    m = onnx.load(str(tmp))
    g = m.graph
    gemm = next(n for n in g.node if n.op_type == "Gemm")       # the classifier: dropped
    feat = gemm.input[0]
    g.node.remove(gemm)
    for init in [i for i in g.initializer if i.name in gemm.input[1:]]:
        g.initializer.remove(init)
    while len(g.output):
        g.output.pop()
    g.output.append(helper.make_tensor_value_info(feat, TensorProto.FLOAT, ["N", None]))
    d = g.input[0].type.tensor_type.shape.dim
    d[0].dim_param, d[2].dim_param, d[3].dim_param = "N", "H", "W"
    while len(g.value_info):
        g.value_info.pop()
    onnx.checker.check_model(m)
    onnx.save(m, str(dest.with_suffix(".tmp")))
    dest.with_suffix(".tmp").replace(dest)
    tmp.unlink(missing_ok=True)
    return dest


# ---- the examples --------------------------------------------------------------------------------

def _progress(data, board, **kw):
    f = folder(data, board) / "progress.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    pj._write_json(f, {"board": board, "pid": os.getpid(), "time": time.time(), **kw})


def collect(data: Path, board: str, note=lambda text: None) -> list[dict]:
    """Every usable example of the design's parts: {"ref", "kind", "side", "src", "from", "present",
    "right", "turn", "square", "img"}. present / right are True, False or None (not known); turn is
    the quarter turns from upright when known; "from" says where the answer came from."""
    data = Path(data)
    found = {}                      # (src, ref) -> example; a tester's answer wins over the reference's
    hidden = set()                  # (src, ref) answered COVERED: its look there says nothing about the part

    def add(src, ref, origin, present=None, right=None, quarter=None):
        k = (src, ref)
        e = found.get(k)
        if e and e["from"] in ("taught", "ns") and origin not in ("taught", "ns"):
            return
        e = found.setdefault(k, {"src": src, "ref": ref, "from": origin, "present": None, "right": None, "turn": None})
        if origin in ("taught", "ns") and e["from"] not in ("taught", "ns"):
            e.update({"from": origin, "present": None, "right": None, "turn": None})
        for key, v in (("present", present), ("right", right), ("turn", quarter)):
            if v is not None:
                e[key] = v

    for side in ("front", "back"):
        exs = ck.load_examples(data, board, side)
        for e in exs["examples"]:
            src = e.get("source") or (f"check/{e['check']}/{side}" if e.get("check") else "")
            if not src or e["label"] == "covered":
                continue
            if e["label"] == "good":
                add(src, e["ref"], "taught", True, True, 0)
            elif e.get("reason") == "missing":
                add(src, e["ref"], "taught", False)
            elif e.get("reason") == "reversed":
                add(src, e["ref"], "taught", True, False, 2)
            else:
                add(src, e["ref"], "taught", True)
        ns = exs.get("not_stuffed") or {}
        srcs = tl.sources(data, board, side)
        for s in srcs:
            try:
                info = tl.source(data, s["id"])
            except ValueError:
                continue
            for ref in set(ns.get(ck.ns_key(info["serial"], info["uid"]), [])) | set(ns.get(ck.ALL_BOARDS, [])):
                add(s["id"], ref, "ns", False)
            hidden |= {(s["id"], ref) for ref in exs["covered"].get(ck.ns_key(info["serial"], info["uid"]), {})}
        # the reference boards, checked by hand
        for r in ck.references(data):
            if r["board"] != board or side not in r["sides"]:
                continue
            src = f"project/{r['uid']}/{side}"
            try:
                d = tl.design_for(data, r["uid"])
            except ValueError:
                continue
            layer = pj.LAYER[side]
            wrong = set(exs.get("reference_wrong", {}).get(r["uid"], []))
            for p in d["parts"]:
                if p["side"] == layer and p["kind"] in POLARISED and p["ref"] not in wrong:
                    add(src, p["ref"], "reference", True, True, 0)
            for c in d.get("components", []):
                if c["side"] != layer or c["ref"] in d.get("fiducials", {}):
                    continue
                if c.get("dnp"):
                    add(src, c["ref"], "dnp", False)
                elif c.get("in_bom"):
                    add(src, c["ref"], "reference", True)
    # cut each part out of its photo, one photo at a time
    out, by_src, skipped = [], {}, {}
    for k, e in found.items():
        if k not in hidden:
            by_src.setdefault(e["src"], []).append(e)
    for i, (src, rows) in enumerate(sorted(by_src.items())):
        note(f"Cutting parts out of photo {i + 1} of {len(by_src)}")
        try:
            s = tl.source(data, src)
            d = tl.design_for(data, s["design"])
            img = tl._image(s["img"])
        except (ValueError, OSError):
            continue
        kinds = {p["ref"]: p["kind"] for p in d["parts"]}
        for e in rows:
            g = tl.part_geom(d, e["ref"])
            if not g or g["side"] != pj.LAYER[s["side"]]:
                continue
            im = cut(img, s["mapping"], g, d["rot_sign"])
            if im is None:
                continue
            if e["from"] == "dnp" and not looks_empty(im):     # the BOM says DNP, but it doesn't look bare
                skipped["dnp"] = skipped.get("dnp", 0) + 1
                continue
            out.append({**e, "kind": kinds.get(e["ref"]), "side": s["side"], "square": is_square(g), "img": im})
    collect.skipped = skipped
    return out


# ---- a logistic regression -----------------------------------------------------------------------

def fit(X, y, center=True, bias=True, lam=1.0, dims=PCA_DIMS) -> dict:
    """y in {0, 1}. Standardised, cut to its main directions (PCA), then fitted by Newton's method.
    center=False, bias=False keeps the model odd (f(-x) = -f(x)), for the polarity job."""
    X = np.asarray(X, np.float64)
    mu = X.mean(0) if center else np.zeros(X.shape[1])
    sd = (X - mu).std(0) if center else np.sqrt((X ** 2).mean(0))
    sd = np.where(sd < 1e-8, 1.0, sd)
    Z = (X - mu) / sd
    _, _, vt = np.linalg.svd(Z, full_matrices=False)
    P = vt[:min(dims, len(vt))].T
    Z = Z @ P
    if bias:
        Z = np.c_[Z, np.ones(len(Z))]
    w = np.zeros(Z.shape[1])
    reg = lam * np.eye(Z.shape[1])
    if bias:
        reg[-1, -1] = 1e-6
    for _ in range(50):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        grad = Z.T @ (p - y) + reg @ w
        Hm = (Z * (p * (1 - p))[:, None]).T @ Z + reg
        step = np.linalg.solve(Hm, grad)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return {"mu": mu, "sd": sd, "P": P, "w": w[:-1] if bias else w, "b": float(w[-1]) if bias else 0.0}


def logit(m: dict, X) -> np.ndarray:
    Z = ((np.asarray(X, np.float64) - m["mu"]) / m["sd"]) @ m["P"]
    return Z @ m["w"] + m["b"]


def prob(m, X):
    return 1 / (1 + np.exp(-np.clip(logit(m, X), -30, 30)))


def _folds(groups, k=FOLDS):
    """Parts split into k rounds (every example of a part in the same one)."""
    names = sorted(set(groups))
    rng = np.random.default_rng(7)
    rng.shuffle(names)
    k = min(k, len(names))
    which = {n: i % k for i, n in enumerate(names)}
    return k, np.array([which[g] for g in groups])


# ---- each job --------------------------------------------------------------------------------------

def _train_missing(items, feats):
    rows = [e for e in items if e["present"] is not None]
    X = feats([e["img"] for e in rows]) if rows else np.zeros((0, 1))
    y = np.array([1.0 if e["present"] else 0.0 for e in rows])

    def fitter(i):
        return fit(X[i], y[i])

    def test(m, i):
        return prob(m, X[i]) >= 0.5, y[i] == 1
    pos = [e for e in rows if e["present"]]
    neg = [e for e in rows if not e["present"]]
    enough = min(len(pos), len(neg)) >= MIN["missing"] and min(len({e["ref"] for e in pos}), len({e["ref"] for e in neg})) >= MIN_PARTS
    short = []
    if len(pos) < MIN["missing"]:
        short.append(f"{MIN['missing'] - len(pos)} more examples of parts there")
    if len(neg) < MIN["missing"]:
        short.append(f"{MIN['missing'] - len(neg)} more MISSING or NS answers (or DNP parts on a reference)")
    return rows, X, y, fitter, test, enough, short, {"pos": len(pos), "neg": len(neg)}


def _train_polarity(items, feats):
    rows = [e for e in items if e["right"] is not None and e["kind"] in POLARISED]
    if rows:
        A = feats([e["img"] for e in rows]) - feats([turn(e["img"], 2) for e in rows])
    else:
        A = np.zeros((0, 1))
    y = np.array([1.0 if e["right"] else 0.0 for e in rows])

    def fitter(i):     # each example, and itself turned round with the other answer
        return fit(np.r_[A[i], -A[i]], np.r_[y[i], 1 - y[i]], center=False, bias=False)

    def test(m, i):
        return prob(m, A[i]) >= 0.5, y[i] == 1
    pos = [e for e in rows if e["right"]]
    enough = len(pos) >= MIN["polarity"] and len({e["ref"] for e in pos}) >= MIN_PARTS
    short = [] if enough else [f"{max(0, MIN['polarity'] - len(pos))} more right-way-round examples"
                               + (f" (from {MIN_PARTS} or more different parts)" if len({e['ref'] for e in pos}) < MIN_PARTS else "")]
    return rows, A, y, fitter, test, enough, short, {"pos": len(pos), "neg": len(rows) - len(pos)}


def _train_orientation(items, feats):
    rows = [e for e in items if e["square"] and e["turn"] is not None]
    # each example turned upright, then to each quarter turn: the model scores how upright a picture looks
    ups = [turn(e["img"], -e["turn"]) for e in rows]
    F = [feats([turn(u, q) for u in ups]) if rows else np.zeros((0, 1)) for q in range(4)]
    y = np.array([float(e["turn"]) for e in rows])

    def fitter(i):
        X = np.concatenate([F[q][i] for q in range(4)])
        return fit(X, np.r_[np.ones(len(i)), np.zeros(3 * len(i))])

    def test(m, i):      # each held-out picture as it is: which quarter turn makes it look upright?
        ok = []
        for q in range(4):        # the example turned q from upright: does the model say q?
            idx = [(q + j) % 4 for j in range(4)]
            scores = np.stack([logit(m, F[idx[j]][i]) for j in range(4)], 1)     # undo j quarter turns
            ok.append(scores.argmax(1) == q)
        return np.concatenate(ok), np.ones(4 * len(i), bool)
    parts = len({e["ref"] for e in rows})
    enough = parts >= MIN["orientation"]
    short = [] if enough else [f"{MIN['orientation'] - parts} more square parts answered GOOD (or on a reference)"]
    return rows, F[0], y, fitter, test, enough, short, {"pos": len(rows), "neg": 3 * len(rows)}


TRAIN = {"missing": _train_missing, "polarity": _train_polarity, "orientation": _train_orientation}


def _evaluate(rows, fitter, test):
    """Accuracy on parts kept out of training, and per kind of part."""
    groups = [e["ref"] for e in rows]
    k, fold = _folds(groups)
    if k < 2:
        return None, {}
    right, total, by_kind = 0, 0, {}
    for f in range(k):
        tr, te = np.where(fold != f)[0], np.where(fold == f)[0]
        if not len(te):
            continue
        m = fitter(tr)
        pred, truth = test(m, te)
        good = pred == truth
        reps = len(good) // len(te)
        for j, i in enumerate(te):
            kind = rows[i]["kind"] or "not in the check"
            bk = by_kind.setdefault(kind, [0, 0])
            ok = good[j::len(te)][:reps] if reps > 1 else [good[j]]
            bk[0] += int(np.sum(ok))
            bk[1] += len(ok)
        right += int(good.sum())
        total += len(good)
    return (right / total if total else None), {k: {"right": v[0], "total": v[1]} for k, v in by_kind.items()}


def train(data: Path, board: str, jobs=JOBS, note=None) -> dict:
    """Trains the jobs' models for a board design and writes their metrics. Returns the metrics."""
    data = Path(data)
    note = note or (lambda text, pct=None: None)
    d = folder(data, board)
    d.mkdir(parents=True, exist_ok=True)
    note("Collecting examples", 2)
    items = collect(data, board, lambda text: note(text, 10))
    feats = Features(data)
    mf = d / "metrics.json"
    metrics = pj._read_json(mf, {}) or {}
    metrics.setdefault("jobs", {})
    for n, job in enumerate(jobs):
        note(f"Training: {TEXT[job]['title']}", 30 + 60 * n // len(jobs))
        rows, X, y, fitter, test, enough, short, counts = TRAIN[job](items, feats)
        by_from, kinds = {}, {}
        for e in rows:
            by_from[e["from"]] = by_from.get(e["from"], 0) + 1
            kk = kinds.setdefault(e["kind"] or "not in the check", {"pos": 0, "neg": 0})
            label = e["present"] if job == "missing" else e["right"] if job == "polarity" else True
            kk["pos" if label else "neg"] += 1
        acc, by_kind = (None, {})
        if rows and len({e["ref"] for e in rows}) >= 2 and len(set(y.tolist())) >= (1 if job != "missing" else 2):
            acc, by_kind = _evaluate(rows, fitter, test)
        base = None
        if job == "missing" and len(y):
            base = float(max(y.mean(), 1 - y.mean()))
        elif job in ("polarity", "orientation") and len(y):
            base = 0.5 if job == "polarity" else 0.25
        m = None
        if rows and (job != "missing" or len(set(y.tolist())) == 2):
            m = fitter(np.arange(len(rows)))
            np.savez(d / f"{job}.npz", **{k: np.asarray(v) for k, v in m.items()})
        own = sum(v for k, v in by_from.items() if k in ("taught", "ns"))
        made = {"reference board": by_from.get("reference", 0), "empty pads on the reference (BOM DNP)": by_from.get("dnp", 0),
                "turned copies": len(rows) if job == "polarity" else 3 * len(rows) if job == "orientation" else 0}
        if job == "missing" and getattr(collect, "skipped", {}).get("dnp"):
            made["DNP parts left out (they don't look bare)"] = collect.skipped["dnp"]
        metrics["jobs"][job] = {
            "own": own, "auto": {k: v for k, v in made.items() if v},
            "rough": own < (len(rows) + made["turned copies"]) / 2,
            "job": job, "trained": pj.now(), "examples": len(rows), "parts": len({e["ref"] for e in rows}),
            "pos": counts["pos"], "neg": counts["neg"], "from": by_from, "kinds": kinds,
            "accuracy": acc, "baseline": base, "by_kind": {k: {**v, "pos": kinds.get(k, {}).get("pos", 0)} for k, v in by_kind.items()},
            "enough": bool(enough), "short": short, "model": m is not None, "features": feats.kind,
            "used": bool(enough and m is not None and (acc or 0) >= USE_ACCURACY),
        }
    metrics["board"] = board
    pj._write_json(mf, metrics)
    note("Done", 100)
    return metrics


# ---- using the models ----------------------------------------------------------------------------

_LOADED = {}


def _load(data: Path, board: str) -> dict:
    """{job: model} for the jobs trained with enough examples."""
    d = folder(data, board)
    mf = d / "metrics.json"
    key = mf.stat().st_mtime_ns if mf.is_file() else 0
    hit = _LOADED.get((str(data), board))
    if hit and hit[0] == key:
        return hit[1]
    out = {}
    met = pj._read_json(mf, {}) or {}
    for job, j in (met.get("jobs") or {}).items():
        f = d / f"{job}.npz"
        if j.get("enough") and j.get("model") and (j.get("accuracy") or 0) >= USE_ACCURACY and f.is_file():
            z = np.load(f)
            out[job] = ({k: (z[k] if z[k].shape else float(z[k])) for k in z.files}, j)
    if out:
        out["_features"] = Features(data)
    _LOADED[(str(data), board)] = (key, out)
    return out


def opinion(data: Path, board: str, img, mapping, g, rot_sign, kind) -> dict:
    """What the trained models make of one part on a lined-up photo: {"missing": {"p": chance it is
    there}, "polarity": {"p": chance it is the right way round}, "orientation": {"turn": degrees,
    "p": confidence}}, for the jobs with a model (empty when there's none, or the part can't be cut out)."""
    ms = _load(data, board)
    if not ms:
        return {}
    im = cut(img, mapping, g, rot_sign)
    if im is None:
        return {}
    feats, out = ms["_features"], {}
    pics = [im]
    pol = "polarity" in ms and kind in POLARISED
    ori = "orientation" in ms and is_square(g) and kind not in (None, "look")
    if pol or ori:
        pics.append(turn(im, 2))
    if ori:
        pics += [turn(im, 1), turn(im, 3)]
    F = feats(pics)
    acc = lambda job: ms[job][1].get("accuracy")
    if "missing" in ms:
        out["missing"] = {"p": round(float(prob(ms["missing"][0], F[:1])[0]), 3), "accuracy": acc("missing")}
    if pol:
        out["polarity"] = {"p": round(float(prob(ms["polarity"][0], F[:1] - F[1:2])[0]), 3), "accuracy": acc("polarity")}
    if ori:
        # F: turned 0, 180, 90, 270 clockwise; undoing a turn of t needs the picture turned back by t
        s = logit(ms["orientation"][0], F)
        back = {0: s[0], 90: s[3], 180: s[1], 270: s[2]}
        z = np.array(list(back.values()))
        p = np.exp(z - z.max())
        p /= p.sum()
        t = list(back)[int(p.argmax())]
        out["orientation"] = {"turn": t, "p": round(float(p.max()), 3), "accuracy": acc("orientation")}
    return out


def summary(op: dict) -> str:
    """One line for the page, e.g. "there 98% · right way round 91% · upright 88%"."""
    bits = []
    if "missing" in op:
        p = op["missing"]["p"]
        bits.append(f"there {p:.0%}" if p >= 0.5 else f"NOT THERE {1 - p:.0%}")
    if "polarity" in op:
        p = op["polarity"]["p"]
        bits.append(f"right way round {p:.0%}" if p >= 0.5 else f"REVERSED {1 - p:.0%}")
    if "orientation" in op:
        o = op["orientation"]
        bits.append(f"upright {o['p']:.0%}" if o["turn"] == 0 else f"TURNED {o['turn']}° {o['p']:.0%}")
    return " · ".join(bits)


# ---- the page's list, and training in the background ---------------------------------------------

def status(data: Path, board: str) -> dict:
    """The metrics of each job and any training run in progress, for the MODELS page."""
    d = folder(data, board)
    met = pj._read_json(d / "metrics.json", {}) or {}
    prog = pj._read_json(d / "progress.json", None)
    if prog and prog.get("state") == "running":
        alive = True
        try:
            os.kill(int(prog.get("pid", 0)), 0)
        except (OSError, ValueError):
            alive = False
        if not alive or time.time() - prog.get("time", 0) > 1800:
            prog = {**prog, "state": "failed", "error": prog.get("error") or "the training stopped"}
    rows = []
    for job in JOBS:
        j = (met.get("jobs") or {}).get(job)
        rows.append({"job": job, **TEXT[job], "min": MIN[job], **({"metrics": j} if j else {"metrics": None})})
    return {"board": board, "jobs": rows, "progress": prog, "use_accuracy": USE_ACCURACY, "features": Features(data).kind if (Path(data) / "models" / EXTRACTOR).is_file() else "simple measures"}


def start(data: Path, board: str, jobs=JOBS):
    """Starts training in its own process at the lowest priority, one thread. Returns at once."""
    import subprocess
    d = folder(data, board)
    prog = pj._read_json(d / "progress.json", None)
    if prog and prog.get("state") == "running" and status(data, board)["progress"]["state"] == "running":
        raise ValueError("That design's models are training already.")
    _progress(data, board, state="running", step="Starting", pct=0, jobs=list(jobs), started=pj.now())
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    cmd = [sys.executable, "-m", "aoimap.models", "train", "--data", str(data), "--board", board, "--jobs", ",".join(jobs)]
    try:
        cmd = ["nice", "-n", "19"] + cmd
        subprocess.Popen(cmd, env=env, cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=open(d / "train.log", "w"), start_new_session=True)
    except OSError as e:
        _progress(data, board, state="failed", error=str(e))
        raise ValueError(f"Training couldn't be started: {e}") from e


def main(argv=None):
    ap = ArgumentParser(prog="python -m aoimap.models")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="download the image network the models use")
    s.add_argument("--data", type=Path, default=Path.home() / "aoimap-data")
    t = sub.add_parser("train", help="train a board design's models")
    t.add_argument("--data", type=Path, default=Path.home() / "aoimap-data")
    t.add_argument("--board", required=True)
    t.add_argument("--jobs", default=",".join(JOBS))
    a = ap.parse_args(argv)
    if a.cmd == "setup":
        print("Saved", setup(a.data))
        return
    try:
        os.nice(19 - os.nice(0))
    except OSError:
        pass
    jobs = [j for j in a.jobs.split(",") if j in JOBS] or list(JOBS)
    started = pj.now()
    try:
        train(a.data, a.board, jobs, lambda text, pct=None: _progress(a.data, a.board, state="running", step=text, pct=pct,
                                                                      jobs=jobs, started=started))
        _progress(a.data, a.board, state="done", step="Done", pct=100, jobs=jobs, started=started, finished=pj.now())
    except Exception as e:   # noqa: BLE001 - shown on the page
        traceback.print_exc()
        _progress(a.data, a.board, state="failed", error=str(e), jobs=jobs, started=started)
        sys.exit(1)


if __name__ == "__main__":
    main()
