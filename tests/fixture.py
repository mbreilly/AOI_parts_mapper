"""A made-up two-sided board for the tests: an ODB++ archive, a BOM with different column names
from the PTC's, and rendered photos of each side with a known camera position.

Rotations in this ODB++ follow the spec (clockwise), and one IC has a package box that isn't
centred on the part, so the rotation direction can be read back from pin 1."""

import io
import math
import tarfile
from pathlib import Path

import cv2
import numpy as np

W_IN, H_IN = 6.0, 4.0

PKGS = {   # name: bbox relative to the part centre, inches
    "QFN32": [-0.10, -0.10, 0.10, 0.10],
    "SOIC8_OFF": [-0.06, -0.10, 0.06, 0.30],    # not centred: the body extends further one way
    "CAPT3528": [-0.07, -0.04, 0.07, 0.04],
    "LED0603": [-0.03, -0.015, 0.03, 0.015],
    "SMA": [-0.10, -0.05, 0.10, 0.05],
    "R0603": [-0.03, -0.015, 0.03, 0.015],
    "FID1MM": [-0.02, -0.02, 0.02, 0.02],
}

# ref, side, x, y, rot (clockwise), package, BOM line
TOP = [
    ("FIDT1", "top", 0.25, 3.75, 0, "FID1MM", None), ("FIDT2", "top", 5.75, 3.75, 0, "FID1MM", None),
    ("FIDT3", "top", 0.25, 0.25, 0, "FID1MM", None), ("FIDT4", "top", 5.75, 0.25, 0, "FID1MM", None),
    ("U1", "top", 1.5, 2.6, 0, "QFN32", "mcu"), ("U2", "top", 3.2, 1.2, 90, "SOIC8_OFF", "opamp"),
    ("U3", "top", 4.6, 2.9, 270, "SOIC8_OFF", "opamp"),
    ("C1", "top", 0.9, 1.0, 0, "CAPT3528", "tant"), ("C2", "top", 2.0, 1.4, 90, "CAPT3528", "tant"),
    ("C3", "top", 2.6, 3.1, 180, "CAPT3528", "tant"), ("C4", "top", 4.0, 0.7, 270, "CAPT3528", "tant"),
    ("C5", "top", 5.0, 1.6, 0, "CAPT3528", "tant"), ("C6", "top", 5.2, 3.2, 90, "CAPT3528", "tant"),
    ("C7", "top", 1.2, 3.3, 0, "CAPT3528", "tant"), ("C8", "top", 3.8, 2.2, 180, "CAPT3528", "tant"),
    ("D1", "top", 4.8, 0.9, 0, "LED0603", "led"), ("D2", "top", 4.9, 0.9, 0, "LED0603", "led"),
    ("D3", "top", 5.0, 0.9, 0, "LED0603", "led"),
    ("D7", "top", 2.4, 2.2, 0, "SMA", "tvs"), ("D8", "top", 1.0, 2.0, 90, "SMA", "schottky"),
    ("R1", "top", 3.0, 2.0, 0, "R0603", "res"), ("R2", "top", 3.1, 2.0, 0, "R0603", "res"),
]
BOT = [
    ("FIDB1", "bot", 5.75, 3.75, 0, "FID1MM", None), ("FIDB2", "bot", 0.25, 3.75, 0, "FID1MM", None),
    ("FIDB3", "bot", 0.25, 0.25, 0, "FID1MM", None),
    ("UB1", "bot", 3.0, 2.0, 0, "QFN32", "mux"),
    ("DB1", "bot", 1.0, 0.8, 0, "LED0603", "led"), ("DB2", "bot", 1.1, 0.8, 0, "LED0603", "led"),
    ("DB3", "bot", 1.2, 0.8, 90, "LED0603", "led"), ("DB4", "bot", 4.5, 3.0, 0, "LED0603", "led"),
    ("RB1", "bot", 2.0, 2.0, 0, "R0603", "res"),
]
PARTS = TOP + BOT

LINES = {   # line: (value, description, refs filled in below)
    "mcu": ("STM32G0", "IC MCU 32BIT 64KB FLASH"),
    "opamp": ("OPA2333", "IC OPAMP ZERO DRIFT"),
    "tant": ("T491B106K", "CAP TANT 10UF 16V 3528"),
    "led": ("LTST-C191", "LED GREEN 0603"),
    "tvs": ("SMAJ12CA", "TVS DIODE 12V BIDIR SMA"),
    "schottky": ("SS14", "DIODE SCHOTTKY 40V 1A"),
    "mux": ("TS3A5017", "MULTIPLEXER 4:1 ANALOG"),
    "res": ("10k", "RES 10K 1% 0603"),
}


def rotate_cw(u, v, deg):
    t = math.radians(-deg)
    return u * math.cos(t) - v * math.sin(t), u * math.sin(t) + v * math.cos(t)


def pins_of(ref, x, y, rot, pkg):
    """Absolute pin positions: two-pin parts have pin 1 at -u, ICs at the -u,+v corner of the body."""
    x0, y0, x1, y1 = PKGS[pkg]
    if pkg.startswith("FID"):
        return {"1": (x, y)}
    if pkg in ("QFN32", "SOIC8_OFF"):
        local = {"1": (x0 + 0.02, y1 - 0.02), "2": (x0 + 0.02, y1 - 0.06), "5": (x1 - 0.02, y0 + 0.02)}
    else:
        local = {"1": (x0 + 0.01, 0.0), "2": (x1 - 0.01, 0.0)}
    out = {}
    for n, (u, v) in local.items():
        du, dv = rotate_cw(u, v, rot)
        out[n] = (x + du, y + dv)
    return out


def make_odb(folder: Path) -> Path:
    """Writes job_odb.tgz into folder and returns its path."""
    pkg_names = list(PKGS)
    files = {}
    files["job/steps/pcb/stephdr"] = "UNITS=INCH\nX_DATUM=0\nY_DATUM=0\n"
    files["job/steps/panel/stephdr"] = "UNITS=INCH\n"
    eda = ["HDR test", "UNITS=INCH"]
    for n in pkg_names:
        b = PKGS[n]
        eda.append(f"PKG {n} 0.05 {b[0]} {b[1]} {b[2]} {b[3]};")
    files["job/steps/pcb/eda/data"] = "\n".join(eda) + "\n"
    for side in ("top", "bot"):
        lines = ["UNITS=INCH", "#", "# Component records", "#"]
        for i, (ref, s, x, y, rot, pkg, _) in enumerate(p for p in PARTS if p[1] == side):
            lines.append(f"# CMP {i}")
            lines.append(f"CMP {pkg_names.index(pkg)} {x} {y} {rot} N {ref} PN_{ref} ;0=1")
            for j, (pn, (px, py)) in enumerate(pins_of(ref, x, y, rot, pkg).items()):
                lines.append(f"TOP {j} {px:.6f} {py:.6f} {rot} N 1 1 {pn}")
        files[f"job/steps/pcb/layers/comp_+_{side}/components"] = "\n".join(lines) + "\n"
    files["job/steps/pcb/profile"] = (f"UNITS=INCH\nS P 0\nOB 0 0 I\nOS {W_IN} 0\nOS {W_IN} {H_IN}\nOS 0 {H_IN}\nOS 0 0\nOE\nSE\n")
    out = Path(folder) / "job_odb.tgz"
    with tarfile.open(out, "w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name); info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out


def make_bom(folder: Path) -> Path:
    refs = {}
    for ref, *_, line in PARTS:
        if line:
            refs.setdefault(line, []).append(ref)
    rows = ["Project,Test board", "", "Item,Qty,Designator,Comment,Description,DNP"]
    for i, (line, rs) in enumerate(refs.items()):
        value, desc = LINES[line]
        rows.append(f'{i + 1},{len(rs)},"{", ".join(rs)}",{value},"{desc}",')
    rows.append(f'{len(refs) + 1},1,R9,10k,"RES 10K 0603",DNP')
    out = Path(folder) / "bom.csv"
    out.write_text("\n".join(rows) + "\n")
    return out


# ---- photos -----------------------------------------------------------------------------

def camera(side, ppi=300, size=(2000, 1500), tilt_deg=2.0, region=None, persp=1e-6):
    """A homography from board inches to photo pixels for a camera looking at one side.
    orient: front (1, -1), back turned top to bottom (1, 1). region [x0, y0, x1, y1] frames a closeup."""
    sx, sy = (1, -1) if side == "front" else (1, 1)
    W, H = size
    if region:
        x0, y0, x1, y1 = region
        ppi = min(W / (x1 - x0), H / (y1 - y0)) * 0.9
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    else:
        cx, cy = W_IN / 2, H_IN / 2
    t = math.radians(tilt_deg)
    A = np.float64([[ppi * sx * math.cos(t), -ppi * sy * math.sin(t), 0],
                    [ppi * sx * math.sin(t), ppi * sy * math.cos(t), 0], [0, 0, 1]])
    c = A @ [cx, cy, 1]
    A[0, 2], A[1, 2] = W / 2 - c[0], H / 2 - c[1]
    P = np.float64([[1, 0, 0], [0, 1, 0], [persp, 0.75 * persp, 1]])   # a little perspective, as on a camera stand
    return P @ A


def to_px(Hm, pts):
    pts = np.float64(pts).reshape(-1, 2)
    return cv2.perspectiveTransform(pts[None], Hm)[0]


def render(side, Hm, size=(2000, 1500), reversed_refs=()):
    """A photo of one side: green board, gold fiducials, yellow tantalums with a band at pin 1 (+),
    black ICs with a pin 1 dot, LEDs with a green cathode mark at pin 1."""
    W, H = size
    img = np.full((H, W, 3), (40, 40, 40), np.uint8)
    outline = to_px(Hm, [[0, 0], [W_IN, 0], [W_IN, H_IN], [0, H_IN]]).astype(np.int32)
    cv2.fillPoly(img, [outline], (40, 110, 30))
    rng = np.random.default_rng(1)
    noise = rng.integers(-6, 7, img.shape, dtype=np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    layer = "top" if side == "front" else "bot"
    for ref, s, x, y, rot, pkg, line in PARTS:
        if s != layer:
            continue
        x0, y0, x1, y1 = PKGS[pkg]
        rr = rot + (180 if ref in reversed_refs else 0)
        corners = [rotate_cw(u, v, rr) for u, v in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        poly = to_px(Hm, [(x + u, y + v) for u, v in corners]).astype(np.int32)
        pins = pins_of(ref, x, y, rr, pkg)
        if pkg.startswith("FID"):
            c = to_px(Hm, [(x, y)])[0]
            r = max(3, int(round(np.linalg.norm(to_px(Hm, [(x + 0.02, y)])[0] - c))))
            cv2.circle(img, tuple(c.astype(int)), r, (60, 190, 225), -1, cv2.LINE_AA)
        elif line == "tant":
            cv2.fillPoly(img, [poly], (30, 165, 230))
            p1 = to_px(Hm, [pins["1"]])[0]
            band = to_px(Hm, [(x + u * 0.75, y + v) for u, v in [rotate_cw(x0, y0, rr), rotate_cw(x0, y1, rr)]])
            q = np.vstack([poly[0], poly[3], band[1], band[0]]).astype(np.int32)
            cv2.fillPoly(img, [q], (20, 60, 110))
        elif pkg in ("QFN32", "SOIC8_OFF"):
            cv2.fillPoly(img, [poly], (25, 25, 25))
            cv2.circle(img, tuple(to_px(Hm, [pins["1"]])[0].astype(int)), 4, (220, 220, 220), -1)
        elif line == "led":
            cv2.fillPoly(img, [poly], (200, 230, 230))
            cv2.circle(img, tuple(to_px(Hm, [pins["1"]])[0].astype(int)), 2, (40, 160, 40), -1)
        else:
            cv2.fillPoly(img, [poly], (60, 60, 60))
    return img
