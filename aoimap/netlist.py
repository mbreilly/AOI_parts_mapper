"""Nets and pins from the ODB++, for the board viewer on the Map tab: every pin of every part with its
net, the pad size of each pin, and the board outline as drawn in the profile.

Where it comes from (ODB++ 7/8):
    steps/<step>/eda/data                NET records (in order: a net's number is its place in this
                                         list), PKG records with each PIN and the pad shape after it,
                                         and SNT TOP T|B <part> <pin> (which pins each net reaches)
    steps/<step>/layers/comp_+_top|bot/components
                                         TOP <pin> <x> <y> <rot> <mirror> <net> <subnet> <name>
    steps/<step>/profile                 the board outline (OB / OS / OC / OE)

The net of a pin is read from its TOP record; when a file leaves that out, from the SNT records.
"""

import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path

from . import odb as odblib

VERSION = 1      # bump when what's read changes, so cached copies are read again


def _arc(p0, end, centre, cw: bool, n: int = 8) -> list:
    """Points along an arc from p0 to end about centre (the end included), for outlines and pad sizes."""
    a0 = math.atan2(p0[1] - centre[1], p0[0] - centre[0])
    a1 = math.atan2(end[1] - centre[1], end[0] - centre[0])
    r = math.hypot(p0[0] - centre[0], p0[1] - centre[1])
    d = a1 - a0
    if cw and d >= 0:
        d -= 2 * math.pi
    elif not cw and d <= 0:
        d += 2 * math.pi
    return [[centre[0] + r * math.cos(a0 + d * i / n), centre[1] + r * math.sin(a0 + d * i / n)] for i in range(1, n + 1)]


class _Shape:
    """Collects the records of one pad (or a package body) into a bounding box."""

    def __init__(self):
        self.pts, self.round, self.last = [], False, None

    def add(self, t):
        try:
            k = t[0]
            if k == "RC":
                x, y, w, h = map(float, t[1:5])
                self.pts += [[x, y], [x + w, y + h]]
            elif k == "CR":
                x, y, r = map(float, t[1:4])
                self.pts += [[x - r, y - r], [x + r, y + r]]
                self.round = True
            elif k == "SQ":
                x, y, h = map(float, t[1:4])
                self.pts += [[x - h, y - h], [x + h, y + h]]
            elif k in ("OB", "OS"):
                self.last = [float(t[1]), float(t[2])]
                self.pts.append(self.last)
            elif k == "OC" and self.last:
                end, c = [float(t[1]), float(t[2])], [float(t[3]), float(t[4])]
                self.pts += _arc(self.last, end, c, len(t) < 6 or t[5].upper() == "Y")
                self.last = end
        except (ValueError, IndexError):
            pass

    def size(self) -> list:
        if not self.pts:
            return [0, 0, False]
        xs, ys = [p[0] for p in self.pts], [p[1] for p in self.pts]
        return [max(xs) - min(xs), max(ys) - min(ys), self.round]


def _outline(step: Path) -> list:
    """The board outline from the profile: the first island's points, arcs as short lines."""
    pts, last, done = [], None, False
    for t in odblib._records(step / "profile"):
        try:
            if t[0] == "OB":
                if pts and done:
                    break
                last = [float(t[1]), float(t[2])]
                if len(t) > 3 and t[3].upper() == "H":     # a hole: only the outer edge is drawn
                    last = None
                    continue
                pts = [last]
            elif last is not None and t[0] == "OS":
                last = [float(t[1]), float(t[2])]
                pts.append(last)
            elif last is not None and t[0] == "OC":
                end, c = [float(t[1]), float(t[2])], [float(t[3]), float(t[4])]
                pts += _arc(last, end, c, len(t) < 6 or t[5].upper() == "Y", 12)
                last = end
            elif t[0] == "OE" and pts:
                done = True
                last = None
        except (ValueError, IndexError):
            pass
    return pts if len(pts) >= 3 else []


def read_step(step: Path) -> dict:
    """{"units", "nets": [name], "packages": [{"name", "pins": [[name, w, h, round]]}],
    "parts": {"top"|"bot": [{"ref", "package", "pins": [[name, x, y, net]]}]}, "outline": [[x, y]],
    "notes": [...]}. net is the net's number in "nets", or -1 when the pin is on no net."""
    step = Path(step)
    units, _ = odblib._units(step)
    nets, pkgs, snt = [], [], {}
    owner, net = None, None
    for t in odblib._records(step / "eda" / "data"):
        k = t[0]
        if k == "NET" and len(t) >= 2:
            nets.append(t[1])
            net, owner = len(nets) - 1, None
        elif k == "SNT" and net is not None and len(t) >= 5 and t[1] == "TOP":
            try:
                snt[(t[2].upper(), int(t[3]), int(t[4]))] = net
            except ValueError:
                pass
        elif k == "PKG" and len(t) >= 2:
            pkgs.append({"name": t[1], "pins": [], "_shapes": []})
            owner, net = None, None
        elif k == "PIN" and pkgs and len(t) >= 2:
            owner = _Shape()
            pkgs[-1]["pins"].append(t[1])
            pkgs[-1]["_shapes"].append(owner)
        elif owner is not None and k in ("RC", "CR", "SQ", "OB", "OS", "OC"):
            owner.add(t)
    for p in pkgs:
        p["pins"] = [[n, *s.size()] for n, s in zip(p["pins"], p.pop("_shapes"))]
    parts, from_snt, no_net = {"top": [], "bot": []}, 0, 0
    for side in ("top", "bot"):
        cur, i = None, -1
        for t in odblib._records(step / "layers" / f"comp_+_{side}" / "components"):
            if t[0] == "CMP" and len(t) >= 7:
                i += 1
                try:
                    pk = int(t[1])
                except ValueError:
                    pk = -1
                cur = {"ref": t[6], "package": pk if 0 <= pk < len(pkgs) else -1, "pins": []}
                parts[side].append(cur)
            elif t[0] == "TOP" and cur is not None and len(t) >= 4:
                try:
                    pin, x, y = int(t[1]), float(t[2]), float(t[3])
                except ValueError:
                    continue
                n = None
                if len(t) >= 9:
                    try:
                        n = int(t[6])
                    except ValueError:
                        n = None
                if n is None or not 0 <= n < len(nets):
                    n2 = snt.get(("T" if side == "top" else "B", i, pin))
                    if n2 is not None:
                        n, from_snt = n2, from_snt + 1
                    else:
                        n = -1
                if n >= 0 and nets[n].upper() == "$NONE$":     # how ODB++ writes "not connected"
                    n = -1
                if n < 0:
                    no_net += 1
                cur["pins"].append([t[-1], x, y, n, pin])
    notes = []
    if not nets:
        notes.append("The ODB++ has no nets (no NET records in eda/data), so pins can't be traced.")
    elif from_snt:
        notes.append(f"{from_snt} pin(s) took their net from the eda/data records.")
    if not any(pk["pins"] for pk in pkgs):
        notes.append("The packages have no pad shapes, so pins are drawn as dots.")
    return {"version": VERSION, "units": units, "nets": nets, "packages": pkgs, "parts": parts,
            "outline": _outline(step), "notes": notes, "pins_without_net": no_net}


def load(src: Path, step_name: str | None = None) -> dict:
    """Reads the nets of one step (the named one, or the first board step) of an ODB++ archive."""
    tmp = Path(tempfile.mkdtemp(prefix="aoimap-nets-"))
    try:
        root = odblib.unpack(Path(src), tmp)
        steps = odblib.find_steps(root)
        if not steps:
            raise odblib.OdbError("No ODB++ step found (no stephdr file).")
        pick = next((s for s in steps if s["name"] == step_name), None) or steps[0]
        return read_step(Path(pick["path"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cached(src: Path, step_name: str | None, cache: Path) -> dict:
    """load(), kept in cache (a JSON file, one per board design) until the ODB++ or step changes."""
    key = f"{VERSION}:{hashlib.sha1(Path(src).read_bytes()).hexdigest()}:{step_name or ''}"
    try:
        d = json.loads(Path(cache).read_text())
        if d.get("key") == key:
            return d
    except (OSError, ValueError):
        pass
    d = {**load(src, step_name), "key": key}
    cache = Path(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, separators=(",", ":")))
    tmp.replace(cache)
    return d
