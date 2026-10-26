"""AOI projects: one folder for each board mapped, so earlier boards are never overwritten.

    <data>/projects/<uid>/
        project.json            board name, serial, notes, original file names, a log of what was done
        profile.json            the choices used for this project (a copy of the board profile)
        source/                 the ODB++ and BOM as loaded
        design.json, parts_map.csv
        front/ back/            reference.jpg, alignment.json, parts.json, parts_full.json, map.jpg, closeups/
        thumbs/                 small copies of the images for the history page (made when asked for)

The folder is complete on its own. The database (db.py) keeps the same information so the history
page can list and search projects, and it is rebuilt from these folders if it falls behind.
"""

import json
import re
import shutil
import socket
from datetime import datetime
from pathlib import Path

import cv2

STATION = socket.gethostname()
SIDES = ("front", "back")
LAYER = {"front": "top", "back": "bot"}


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "board"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def root(data: Path) -> Path:
    return Path(data) / "projects"


def folder(data: Path, uid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", uid or "") or uid.startswith("."):
        raise ValueError("No such project.")
    return root(data) / uid


def new(data: Path, board: str, when: datetime | None = None) -> dict:
    when = when or datetime.now()
    base = f"{when:%Y%m%d-%H%M%S}-{slug(board)}"[:72]
    uid, n = base, 2
    while (root(data) / uid).exists():
        uid, n = f"{base}-{n}", n + 1
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    p = {"uid": uid, "board": board, "serial": "", "notes": "", "station": STATION, "created": stamp,
         "updated": stamp, "odb_file": "", "bom_file": "", "log": []}
    (root(data) / uid).mkdir(parents=True)
    save(data, p)
    return p


def load(data: Path, uid: str) -> dict:
    p = _read_json(folder(data, uid) / "project.json")
    if not p or p.get("uid") != uid:
        raise ValueError("No such project.")
    return p


def save(data: Path, p: dict):
    _write_json(folder(data, p["uid"]) / "project.json", p)


def log(data: Path, p: dict, what: str):
    p["updated"] = now()
    p["log"].append({"at": p["updated"], "what": what})
    save(data, p)


def uids(data: Path) -> list[str]:
    r = root(data)
    return sorted(d.name for d in r.iterdir() if (d / "project.json").is_file()) if r.is_dir() else []


# ---- what a project folder holds ------------------------------------------------------------

def classify(rel: str) -> tuple[str, str]:
    """(kind, side) for a file in a project folder, by its path."""
    parts = rel.split("/")
    side = parts[0] if parts[0] in SIDES else ""
    name = parts[-1]
    if parts[0] == "source":
        return ("odb" if name.startswith("odb.") else "bom" if name.startswith("bom.") else "source"), ""
    if side and len(parts) == 2:
        return {"reference.jpg": "photo", "map.jpg": "map"}.get(name, "data"), side
    if side and parts[1] == "closeups":
        return ("closeup" if name.endswith(".jpg") else "data"), side
    return {"design.json": "design", "parts_map.csv": "csv", "profile.json": "profile",
            "project.json": "project"}.get(name, "data"), ""


def files(data: Path, uid: str) -> list[dict]:
    d = folder(data, uid)
    out = []
    for f in sorted(d.rglob("*")):
        rel = f.relative_to(d).as_posix()
        if not f.is_file() or rel.startswith("thumbs/") or f.suffix == ".tmp":
            continue
        kind, side = classify(rel)
        st = f.stat()
        out.append({"path": rel, "kind": kind, "side": side, "bytes": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return out


def file_path(data: Path, uid: str, rel: str) -> Path:
    d = folder(data, uid).resolve()
    f = (d / rel).resolve()
    if d not in f.parents or not f.is_file() or rel.startswith("thumbs/"):
        raise ValueError("No such file.")
    return f


def thumb(data: Path, uid: str, rel: str, width: int = 480) -> bytes:
    """A small JPEG of an image in the project, made once and kept in thumbs/."""
    src = file_path(data, uid, rel)
    width = max(80, min(1600, int(width)))
    t = folder(data, uid) / "thumbs" / f"{width}" / rel
    if not t.is_file() or t.stat().st_mtime < src.stat().st_mtime:
        img = cv2.imread(str(src), cv2.IMREAD_REDUCED_COLOR_2)
        if img is None:
            raise ValueError("That file isn't an image.")
        h, w = img.shape[:2]
        if w > width:
            img = cv2.resize(img, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        t.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        t.write_bytes(buf.tobytes())
    return t.read_bytes()


def summary(data: Path, uid: str) -> dict:
    """The counts the history list shows, and every placed part, read from the project folder."""
    d = folder(data, uid)
    des = _read_json(d / "design.json", {}) or {}
    parts = des.get("parts", [])
    s = {"units": des.get("units", ""), "parts": len(parts), "closeups": 0, "rows": []}
    for side in SIDES:
        n = sum(1 for p in parts if p.get("side") == LAYER[side])
        placed = _read_json(d / side / "parts_full.json", []) if (d / side / "alignment.json").is_file() else []
        cl = d / side / "closeups"
        s[side] = {"parts": n, "photo": (d / side / "reference.jpg").is_file(), "aligned": bool(placed),
                   "checkable": sum(1 for r in placed if r.get("checkable")) if placed else None}
        s["closeups"] += len(list(cl.glob("*.jpg"))) if cl.is_dir() else 0
        for r in placed:
            s["rows"].append({"side": side, "ref": r["ref"], "kind": r.get("kind", ""), "value": r.get("value", ""),
                              "checkable": bool(r.get("checkable")), "via": r.get("via"),
                              "reason": r.get("reason") or "", "px": r.get("px")})
    return s


def row(data: Path, p: dict, s: dict | None = None) -> dict:
    """One line about a project for the history list (the same shape the database gives)."""
    s = s or summary(data, p["uid"])
    out = {k: p.get(k, "") for k in ("uid", "board", "serial", "notes", "station", "created", "updated", "odb_file", "bom_file")}
    out.update(units=s["units"], parts=s["parts"], closeups=s["closeups"])
    for side in SIDES:
        out[side] = {k: s[side][k] for k in ("parts", "photo", "aligned", "checkable")}
    return out


def copy_source(data: Path, src_uid: str, dst: dict):
    s = folder(data, src_uid)
    d = folder(data, dst["uid"])
    shutil.copytree(s / "source", d / "source")
    if (s / "profile.json").is_file():
        shutil.copy(s / "profile.json", d / "profile.json")
