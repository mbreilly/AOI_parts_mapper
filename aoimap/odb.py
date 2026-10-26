"""Reads the parts of a board from its ODB++: positions, rotations, sides, package outlines, pins,
fiducials, the board outline and the units.

ODB++ arrives as a .tgz/.tar/.zip or an unpacked folder. Archives are unpacked into a new temporary
folder and only read as text; nothing in them is run.
"""

import gzip
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path


class OdbError(Exception):
    """A problem with the ODB++ the user can act on."""


@dataclass
class OdbPart:
    ref: str
    side: str            # "top" or "bot", from the layer file the part is in
    x: float
    y: float
    rot: float           # degrees, as written in the file
    mirror: bool
    package: str
    bbox: list           # [xmin, ymin, xmax, ymax] relative to the part centre
    part_name: str = ""
    pins: dict = field(default_factory=dict)   # pin name -> [x, y], absolute board coordinates


@dataclass
class OdbDesign:
    name: str
    step: str
    units: str           # "inch" or "mm"
    units_known: bool    # False when the files didn't say and inches were assumed
    outline: dict
    parts: list
    fiducials: dict      # ref -> {"side", "x", "y"}
    notes: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        return d


# ---- opening the archive ---------------------------------------------------------------

def unpack(src: Path, into: Path) -> Path:
    """Unpacks an ODB++ archive into the folder `into` (which must be new and empty). Returns the
    folder to search. A folder is used as it is."""
    src = Path(src)
    if src.is_dir():
        return src
    name = src.name.lower()
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                p = Path(info.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise OdbError(f"The zip has an unsafe path ({info.filename}).")
            zf.extractall(into)
        # an ODB++ .tgz inside a zip (a common way to send it)
        inner = [p for p in into.rglob("*") if p.is_file() and re.search(r"\.(tgz|tar\.gz|tar)$", p.name, re.I)]
        if inner and not any(into.rglob("stephdr")):
            sub = into / "_inner"
            sub.mkdir()
            return unpack(inner[0], sub)
        return into
    if tarfile.is_tarfile(src) or name.endswith((".tgz", ".tar.gz", ".tar")):
        try:
            with tarfile.open(src) as tf:
                tf.extractall(into, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise OdbError(f"The ODB++ archive couldn't be opened: {e}") from e
        return into
    raise OdbError(f"{src.name} isn't an ODB++ archive or folder. Use a .tgz, .tar, .zip or the unpacked folder.")


def _read_text(path: Path) -> str:
    """A record file, plain or gzipped (some tools write components.gz). Unix-compressed .Z files
    can't be read without extra tools."""
    for p in (path, path.with_name(path.name + ".gz"), path.with_name(path.name + ".Z")):
        if p.is_file():
            if p.suffix == ".Z":
                raise OdbError(f"{p.name} is Unix-compressed (.Z). Export the ODB++ uncompressed, or unpack it with "
                               "'uncompress' first.")
            data = p.read_bytes()
            if p.suffix == ".gz" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("latin-1")
    return ""


def _records(path: Path):
    for line in _read_text(path).splitlines():
        t = line.split(";")[0].split()
        if t:
            yield t


# ---- steps and units -------------------------------------------------------------------

def find_steps(root: Path) -> list[dict]:
    """Every step in the ODB++, with whether it has component layers. Panels come last."""
    steps = []
    for hdr in sorted(Path(root).rglob("stephdr")):
        d = hdr.parent
        layers = d / "layers"
        has = {s: bool(_read_text(layers / f"comp_+_{s}" / "components")) for s in ("top", "bot")}
        steps.append({"name": d.name, "path": str(d), "has_components": has["top"] or has["bot"],
                      "sides": [s for s, v in has.items() if v], "panel": "pan" in d.name.lower()})
    steps.sort(key=lambda s: (not s["has_components"], s["panel"], s["name"]))
    return steps


def _units(step: Path) -> tuple[str, bool]:
    for f in (step / "stephdr", step / "layers" / "comp_+_top" / "components", step / "eda" / "data",
              step / "layers" / "comp_+_bot" / "components", step / "profile"):
        txt = _read_text(f)
        m = re.search(r"^\s*(?:UNITS\s*=\s*|U\s+)(MM|INCH)\b", txt, re.I | re.M)
        if m:
            return ("mm" if m.group(1).upper() == "MM" else "inch"), True
    return "inch", False


# ---- reading a step --------------------------------------------------------------------

PIN1_NAMES = ("1", "A1", "A", "+", "P1", "PIN1")


def pin1_of(pins: dict) -> tuple[list | None, str]:
    """Pin 1 of a part and how sure we are: 'named' (a pin called 1, A1, A, + ...), 'guess' (the lowest
    pin number) or 'none'."""
    for n in PIN1_NAMES:
        if n in pins:
            return pins[n], "named"
    nums = sorted((int(m.group()), k) for k in pins if (m := re.search(r"\d+", k)))
    if nums:
        return pins[nums[0][1]], "guess"
    return None, "none"


def read_step(step: Path, name: str | None = None) -> OdbDesign:
    step = Path(step)
    units, known = _units(step)
    notes = [] if known else ["The ODB++ doesn't say its units, so inches are assumed. Check the board size."]
    pkgs = []
    for t in _records(step / "eda" / "data"):
        if t[0] == "PKG" and len(t) >= 7:
            try:
                pkgs.append({"name": t[1], "bbox": [float(v) for v in t[3:7]]})
            except ValueError:
                pkgs.append({"name": t[1], "bbox": [0, 0, 0, 0]})
    if not pkgs:
        notes.append("No packages found in eda/data, so part outlines are unknown; a small box is used.")
    parts, fiducials = [], {}
    for side in ("top", "bot"):
        c = None
        for t in _records(step / "layers" / f"comp_+_{side}" / "components"):
            if t[0] == "CMP" and len(t) >= 7:
                try:
                    pk = pkgs[int(t[1])] if pkgs and 0 <= int(t[1]) < len(pkgs) else {"name": "?", "bbox": [-.02, -.02, .02, .02]}
                    c = OdbPart(ref=t[6], side=side, x=float(t[2]), y=float(t[3]), rot=float(t[4]),
                                mirror=t[5].upper() == "M", package=pk["name"], bbox=list(pk["bbox"]),
                                part_name=t[7] if len(t) > 7 else "")
                except ValueError:
                    c = None
                    continue
                parts.append(c)
                if c.ref.upper().startswith("FID") or "FID" in c.package.upper():
                    fiducials[c.ref] = {"side": side, "x": c.x, "y": c.y}
            elif t[0] == "TOP" and c is not None and len(t) >= 4:
                try:
                    c.pins[t[-1]] = [float(t[2]), float(t[3])]
                except ValueError:
                    pass
    if not parts:
        raise OdbError("No components found. The ODB++ needs layers/comp_+_top/components or comp_+_bot/components.")
    xs, ys = [], []
    for t in _records(step / "profile"):
        if t[0] in ("OB", "OS") and len(t) >= 3:
            try:
                xs.append(float(t[1])); ys.append(float(t[2]))
            except ValueError:
                pass
    if xs:
        outline = {"xmin": min(xs), "xmax": max(xs), "ymin": min(ys), "ymax": max(ys), "from": "profile"}
    else:
        px, py = [p.x for p in parts], [p.y for p in parts]
        pad = 0.1 if units == "inch" else 2.5
        outline = {"xmin": min(px) - pad, "xmax": max(px) + pad, "ymin": min(py) - pad, "ymax": max(py) + pad,
                   "from": "parts"}
        notes.append("No board profile found, so the outline is drawn around the parts.")
    return OdbDesign(name=name or step.name, step=step.name, units=units, units_known=known, outline=outline,
                     parts=parts, fiducials=fiducials, notes=notes)


def load(src: Path, step_name: str | None = None) -> tuple[OdbDesign, list[dict]]:
    """Opens an ODB++ archive or folder and reads one step (the named one, or the first board step).
    Returns the design and the list of steps found."""
    tmp = Path(tempfile.mkdtemp(prefix="aoimap-odb-"))
    try:
        root = unpack(Path(src), tmp)
        steps = find_steps(root)
        if not steps:
            raise OdbError("No ODB++ step found (no stephdr file).")
        pick = next((s for s in steps if s["name"] == step_name), None) if step_name else steps[0]
        if pick is None:
            raise OdbError(f"There's no step called {step_name}.")
        job = re.sub(r"(_odb)?(\.tgz|\.tar\.gz|\.tar|\.zip)$", "", Path(src).name, flags=re.I)
        return read_step(Path(pick["path"]), name=job or pick["name"]), steps
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
