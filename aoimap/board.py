"""One board being mapped: its files, the user's choices (the board profile), the photos of each
side and their alignment. Each board mapped is a project with its own folder (see projects.py), so
earlier boards stay in the history:

    <data>/boards/<board>/profile.json      choices kept for the next board of the same design
    <data>/projects/<uid>/                  everything for one board mapped:
        project.json            board name, serial, notes, original file names, log
        profile.json            the choices used for this project
        design.json             the parts map from the ODB++ and BOM
        source/                 the ODB++ and BOM as loaded
        front/ back/            reference.jpg, alignment.json, parts.json, map.jpg, closeups/
        parts_map.csv
"""

import csv
import io
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import bom as bomlib
from . import design as designlib
from . import odb as odblib
from . import photo as ph
from . import projects as pj

DEFAULT_PROFILE = {
    "columns": None, "kinds": {}, "hidden": {}, "rot_sign": None, "markings": dict(designlib.DEFAULT_MARKINGS),
    "orient": {"front": list(ph.DEFAULT_ORIENT["front"]), "back": list(ph.DEFAULT_ORIENT["back"])},
    "min_px": 14, "camera": None, "step": None, "use_tantalums": False,
}


slug = pj.slug


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


class Board:
    def __init__(self, data_dir: Path):
        self.data = Path(data_dir)
        (self.data / "boards").mkdir(parents=True, exist_ok=True)
        self.odb_path = self.bom_path = None
        self.odb = None
        self.steps = []
        self.bom = None
        self.design = None
        self.name = None
        self.profile = json.loads(json.dumps(DEFAULT_PROFILE))
        self.messages = []
        self.sides = {}
        self.project = None         # project.json of the board being mapped
        self.on_change = None       # called with a project's uid whenever it changes
        self._quiet = False         # set while reopening, which changes nothing
        self._import_old_boards()

    # ---- folders, profiles and projects ------------------------------------------------------
    @property
    def dir(self) -> Path | None:
        return pj.folder(self.data, self.project["uid"]) if self.project else None

    def _profile_path(self, name: str) -> Path:
        return self.data / "boards" / slug(name) / "profile.json"

    def _touch(self, what: str | None = None, p: dict | None = None):
        """Notes that a project changed (and what was done, for its log)."""
        p = p or self.project
        if not p or (self._quiet and p is self.project):
            return
        if what:
            pj.log(self.data, p, what)
        else:
            p["updated"] = pj.now()
            pj.save(self.data, p)
        if self.on_change:
            self.on_change(p["uid"])

    def _has_photos(self) -> bool:
        return any(st.get("img") is not None for st in self.sides.values())

    def open_project(self, uid: str):
        """Reopens a project from the history, with its photos, as it was left."""
        p = pj.load(self.data, uid)
        d = pj.folder(self.data, uid)
        src = d / "source"
        odbs = sorted(src.glob("odb.*")) if src.is_dir() else []
        boms = sorted(src.glob("bom.*")) if src.is_dir() else []
        if not (odbs and boms):
            raise ValueError(f"{p['board']} has no saved ODB++ and BOM.")
        self.project, self.name = p, p["board"]
        saved = _read_json(d / "profile.json") or _read_json(self._profile_path(self.name), {})
        self.profile = {**json.loads(json.dumps(DEFAULT_PROFILE)), **saved}
        self.odb_path, self.bom_path, self.odb = odbs[0], boms[0], None
        self._odb_filename, self._bom_filename = p.get("odb_file") or None, p.get("bom_file") or None
        self.messages = []
        self.sides = {}
        self._quiet = True
        try:
            self._load_design()
            for side in ("front", "back"):
                self._load_side(side)
        finally:
            self._quiet = False

    def new_from(self, uid: str):
        """Starts a new project for another board of the same design as a past one."""
        old = pj.load(self.data, uid)
        p = pj.new(self.data, old["board"])
        p.update(odb_file=old.get("odb_file", ""), bom_file=old.get("bom_file", ""))
        pj.copy_source(self.data, uid, p)
        # the latest choices for this design, not the ones the old project was made with
        if self._profile_path(old["board"]).is_file():
            shutil.copy(self._profile_path(old["board"]), pj.folder(self.data, p["uid"]) / "profile.json")
        pj.log(self.data, p, f"Started from the design of {uid}")
        self.open_project(p["uid"])
        self._touch()

    def set_info(self, uid: str, serial: str | None = None, notes: str | None = None):
        """The serial (or other label) and notes of a project, from the page."""
        p = self.project if self.project and self.project["uid"] == uid else pj.load(self.data, uid)
        said = []
        if serial is not None and serial.strip() != p.get("serial", ""):
            p["serial"] = serial.strip()[:128]
            said.append(f"Serial set to {p['serial']}" if p["serial"] else "Serial cleared")
        if notes is not None and notes != p.get("notes", ""):
            p["notes"] = notes[:5000]
            said.append("Notes changed")
        if said:
            self._touch("; ".join(said), p)
        return p

    def _import_old_boards(self):
        """Boards saved by earlier versions (one folder per board name) become projects once."""
        for d in sorted((self.data / "boards").iterdir()):
            src = d / "source"
            if not src.is_dir() or (d / "imported.txt").is_file():
                continue
            when = datetime.fromtimestamp(src.stat().st_mtime)
            p = pj.new(self.data, d.name, when)
            nd = pj.folder(self.data, p["uid"])
            for item in ("source", "front", "back"):
                if (d / item).is_dir():
                    shutil.copytree(d / item, nd / item)
            for f in ("design.json", "parts_map.csv", "profile.json"):
                if (d / f).is_file():
                    shutil.copy(d / f, nd / f)
            p["log"].append({"at": p["created"], "what": f"Imported from the old boards/{d.name} folder"})
            pj.save(self.data, p)
            (d / "imported.txt").write_text(f"Copied to projects/{p['uid']} on {pj.now()}\n")

    # ---- loading files --------------------------------------------------------------------
    def add_file(self, kind: str, filename: str, data: bytes) -> list[str]:
        """Stores an uploaded ODB++ or BOM and rebuilds the design once both are in."""
        ext = "".join(Path(filename).suffixes[-2:]).lower() if filename.lower().endswith(".tar.gz") else Path(filename).suffix.lower()
        staging = self.data / "uploads"
        staging.mkdir(parents=True, exist_ok=True)
        dest = staging / f"{kind}{ext or '.bin'}"
        for old in staging.glob(f"{kind}.*"):
            old.unlink()
        dest.write_bytes(data)
        if kind == "odb":
            self.odb_path, self.odb = dest, None
            self._odb_filename = filename
        else:
            self.bom_path, self.bom = dest, None
            self._bom_filename = filename
        other = "bom" if kind == "odb" else "odb"
        op = getattr(self, other + "_path")
        if op and op.parent != staging:
            # one file replaced on a board already open: the other is copied from its project
            keep = staging / f"{other}{''.join(op.suffixes)}"
            for old in staging.glob(f"{other}.*"):
                old.unlink()
            shutil.copy(op, keep)
            setattr(self, other + "_path", keep)
        self.messages = []
        if self.odb_path and self.bom_path:
            self._load_design(new_files=True)
        elif kind == "odb":
            self.odb, self.steps = odblib.load(self.odb_path)
            self.messages = [f"Read the ODB++: {len(self.odb.parts)} parts. Now load the BOM."]
        else:
            self.bom = bomlib.read_bom(self.bom_path)
            self.messages = [f"Read the BOM: {len(self.bom['lines'])} lines. Now load the ODB++."]
        return self.messages

    def _load_design(self, new_files=False):
        if self.odb is None or new_files:
            self.odb, self.steps = odblib.load(self.odb_path, self.profile.get("step"))
            if not new_files and self.name:
                self.odb.name = self.name   # the saved copy is called odb.*
            if new_files and getattr(self, "_odb_filename", None):
                self.odb.name = re.sub(r"(_odb)?(\.tgz|\.tar\.gz|\.tar|\.zip)$", "", self._odb_filename, flags=re.I)
        if new_files:
            # a profile saved for this board name starts the choices
            self.name = slug(self.odb.name)
            saved = _read_json(self._profile_path(self.name))
            self.profile = {**json.loads(json.dumps(DEFAULT_PROFILE)), **(saved or {})}
            if saved:
                self.messages.append(f"Using the saved choices for {self.name}.")
            # files changed before any photo was taken stay in the same project
            if not (self.project and self.project["board"] == self.name and not self._has_photos()):
                self.project = pj.new(self.data, self.name)
                self.messages.append("Started a new project; it is kept in the history.")
            self.project["odb_file"] = getattr(self, "_odb_filename", None) or self.project.get("odb_file", "")
            self.project["bom_file"] = getattr(self, "_bom_filename", None) or self.project.get("bom_file", "")
            self.sides = {}
            for side in ("front", "back"):
                self._load_side(side)
        self.bom = bomlib.read_bom(self.bom_path, self.profile.get("columns"))
        self.rebuild()
        if new_files:
            src = self.dir / "source"
            if src.exists():
                shutil.rmtree(src)
            src.mkdir(parents=True)
            shutil.copy(self.odb_path, src / ("odb" + "".join(self.odb_path.suffixes)))
            shutil.copy(self.bom_path, src / ("bom" + self.bom_path.suffix))
            self.odb_path = src / ("odb" + "".join(self.odb_path.suffixes))
            self.bom_path = src / ("bom" + self.bom_path.suffix)
            self.save_profile()
            self._touch(f"Loaded ODB++ {self.project['odb_file']} and BOM {self.project['bom_file']}: "
                        f"{len(self.design['parts'])} polarised parts")

    def rebuild(self):
        self.design = designlib.build(self.odb, self.bom, self.profile)
        self.design["markings"] = self.profile["markings"]
        if self.profile.get("rot_sign") is None:
            ev = self.design["rotation_evidence"]["votes"]
            if ev[1] != ev[-1]:
                self.design["rot_note"] = (f"Rotation direction taken from the design's pin 1 positions "
                                           f"({max(ev.values())} part(s) agree).")
        _write_json(self.dir / "design.json", self.design)
        self._touch()
        for side in list(self.sides):
            if self.sides[side].get("mapping"):
                self._place(side)

    def save_profile(self):
        _write_json(self.dir / "profile.json", self.profile)
        _write_json(self._profile_path(self.name), self.profile)

    def set_columns(self, cols: dict):
        self.profile["columns"] = {k: (int(v) if v not in (None, "") else None) for k, v in cols.items()}
        self.bom = bomlib.read_bom(self.bom_path, self.profile["columns"])
        self.rebuild(); self.save_profile()

    def set_lines(self, changes: dict):
        """{line key: {"kind": kind or "" for none, "hidden": reason or ""}}"""
        for key, c in changes.items():
            if "kind" in c:
                self.profile["kinds"][key] = c["kind"] or None
            if "hidden" in c:
                if c["hidden"]:
                    self.profile["hidden"][key] = c["hidden"]
                else:
                    self.profile["hidden"].pop(key, None)
        self.rebuild(); self.save_profile()

    def set_option(self, key: str, value):
        if key == "rot_sign":
            self.profile["rot_sign"] = None if value in (None, "", "auto") else (1 if int(value) > 0 else -1)
        elif key == "min_px":
            self.profile["min_px"] = max(4.0, float(value))
        elif key == "marking":
            self.profile["markings"][value["kind"]] = value["end"]
        elif key == "use_tantalums":
            self.profile["use_tantalums"] = bool(value)
            self.save_profile()
            for side, st in self.sides.items():
                if st.get("points"):
                    self.align(side, st["points"])
            return
        elif key == "camera":
            self.profile["camera"] = value
        elif key == "step":
            self.profile["step"] = value or None
            self.odb = None
            self._load_design()
            self.save_profile()
            return
        else:
            raise ValueError(f"Unknown setting {key}.")
        self.rebuild(); self.save_profile()

    # ---- photos of each side --------------------------------------------------------------
    def _side_dir(self, side) -> Path:
        return self.dir / side

    def _load_side(self, side):
        d = self._side_dir(side) if self.dir else None
        st = {"img": None, "mapping": None, "points": {}, "placed": [], "warnings": [], "closeups": {}}
        if d and (d / "reference.jpg").is_file():
            st["img"] = cv2.imread(str(d / "reference.jpg"))
            a = _read_json(d / "alignment.json")
            if a and a.get("mapping"):
                st["mapping"] = ph.Mapping.from_dict(a["mapping"])
                st["points"] = a.get("points", {})
                st["anchors"] = a.get("anchors", {})
            for f in sorted((d / "closeups").glob("*.json")) if (d / "closeups").is_dir() else []:
                c = _read_json(f)
                img = cv2.imread(str(f.with_suffix(".jpg")))
                if c and img is not None:
                    c["img"] = img
                    if c.get("mapping"):
                        c["mapping"] = ph.Mapping.from_dict(c["mapping"])
                    st["closeups"][c["index"]] = c
        self.sides[side] = st
        if st["mapping"] and self.design:
            self._place(side)

    def set_orient(self, side, orient):
        self.profile["orient"][side] = [int(orient[0]), int(orient[1])]
        self.save_profile()
        st = self.sides.get(side)
        if st and st.get("points"):
            self.align(side, st["points"])

    def set_photo(self, side, data: bytes):
        img = ph.decode(data)
        d = self._side_dir(side)
        d.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(d / "reference.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if (d / "closeups").is_dir():
            shutil.rmtree(d / "closeups")
        for f in ("alignment.json", "parts.json", "map.jpg"):
            (d / f).unlink(missing_ok=True)
        self.sides[side] = {"img": img, "mapping": None, "points": {}, "placed": [], "warnings": [], "closeups": {}}
        self._touch(f"{side.capitalize()} photo taken ({img.shape[1]} x {img.shape[0]})")
        return {"width": img.shape[1], "height": img.shape[0]}

    def anchors(self, side) -> dict:
        """The points the user is asked to click: the side's fiducials, or (with fewer than 2) the
        board corners and the biggest parts."""
        fids = ph.side_fiducials(self.design, side)
        pts = {r: [f["x"], f["y"]] for r, f in fids.items()}
        what = {r: "fiducial" for r in pts}
        if len(pts) < 2:
            o = self.design["outline"]
            for r, xy in (("board corner A", [o["xmin"], o["ymin"]]), ("board corner B", [o["xmax"], o["ymin"]]),
                          ("board corner C", [o["xmax"], o["ymax"]]), ("board corner D", [o["xmin"], o["ymax"]])):
                pts[r] = xy; what[r] = "corner"
        prompts = ph.anchor_prompts(pts, self.profile["orient"][side])
        for p in prompts:
            p["what"] = what[p["ref"]]
        return {"prompts": prompts, "points": pts}

    def snap(self, side, xy):
        st = self.sides[side]
        if st.get("pads") is None:
            st["pads"] = ph.find_pads(st["img"])
        return ph.snap(st["img"], xy, st["pads"])

    def auto_find(self, side):
        st = self.sides[side]
        fids = ph.side_fiducials(self.design, side)
        return ph.auto_fiducials(st["img"], fids, self.profile["orient"][side])

    def align(self, side, points: dict):
        """points: {anchor ref: [x, y] photo pixels}, as clicked (already snapped by the page)."""
        st = self.sides[side]
        if st.get("img") is None:
            raise ValueError("Take or choose a photo of this side first.")
        anchors = self.anchors(side)["points"]
        use = {r: xy for r, xy in points.items() if r in anchors and xy}
        if len(use) < 2:
            raise ValueError("Click at least 2 points to line the photo up.")
        refs = list(use)
        b = [anchors[r] for r in refs]; p = [use[r] for r in refs]
        orient = self.profile["orient"][side]
        m = ph.fit(b, p, orient)
        warnings = ph.check_points(b, p, orient, st["img"].shape[1], m)
        m2, n_found, n_tant = ph.refine_with_tantalums(st["img"], self.design, side, m, b, p)
        used_tant = n_found >= 2 and bool(self.profile.get("use_tantalums"))
        if used_tant:
            m = m2

        ws = ph.wrong_side_warning(st["img"], self.design, side, n_found, n_tant)
        if ws:
            warnings.append(ws)
        st.update(mapping=m, points=use, warnings=warnings, extra=n_found,
                  how=f"{m.kind} from {len(use)} point(s)" + (f" and {n_found} tantalum capacitors" if used_tant else ""))
        self._place(side)
        _write_json(self._side_dir(side) / "alignment.json",
                    {"points": use, "orient": orient, "rot_sign": self.design["rot_sign"], "mapping": m.to_dict(),
                     "how": st["how"], "warnings": warnings})
        placed = st["placed"]
        self._touch(f"{side.capitalize()} lined up ({st['how']}): {sum(r['checkable'] for r in placed)} of {len(placed)} parts checkable"
                    + (f"; {len(warnings)} warning(s)" if warnings else ""))
        return self.side_state(side)

    def _place(self, side):
        st = self.sides[side]
        placed = ph.place(self.design, side, st["img"].shape, st["mapping"], self.profile["min_px"], self.design["rot_sign"])
        # parts a closeup made checkable
        for c in st.get("closeups", {}).values():
            if not c.get("mapping"):
                continue
            sub = {r["ref"]: r for r in ph.place(self.design, side, c["img"].shape, c["mapping"], self.profile["min_px"],
                                                 self.design["rot_sign"], only_refs=set(c["refs"]))}
            for r in placed:
                s = sub.get(r["ref"])
                if s and s["checkable"] and not r["checkable"] and r["reason"].startswith("too small"):
                    r["checkable"], r["via"] = True, c["index"]
                    r["reason"] = f"checked on closeup {c['index'] + 1}"
            c["placed"] = list(sub.values())
        st["placed"] = placed
        st["tiles"] = ph.plan_closeups(self.design, side, [r for r in placed if "via" not in r],
                                       st["img"].shape, self.profile["min_px"], self.design["rot_sign"])
        # keep closeups already taken whose parts are still small in the full photo
        d = self._side_dir(side)
        _write_json(d / "parts.json", [{"ref": r["ref"], "kind": r["kind"], "box": r["box"]} for r in placed
                                       if r["checkable"] and "via" not in r])
        _write_json(d / "parts_full.json", placed)
        (d / "map.jpg").write_bytes(ph.overlay(st["img"], placed, width=0))
        self.write_csv()
        self._touch()

    # ---- closeups -------------------------------------------------------------------------
    def closeup_photo(self, side, index, data: bytes):
        st = self.sides[side]
        tile = next((t for t in st.get("tiles", []) if t["index"] == index), None) or st["closeups"].get(index)
        if not tile:
            raise ValueError("That closeup isn't needed any more.")
        img = ph.decode(data)
        d = self._side_dir(side) / "closeups"
        d.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(d / f"{index:02d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        c = {"index": index, "refs": tile["refs"], "region": tile["region"], "anchors": tile["anchors"], "img": img,
             "mapping": None, "points": {}}
        st["closeups"][index] = c
        self._save_closeup(side, c)
        self._touch(f"{side.capitalize()} closeup {index + 1} taken")
        return {"width": img.shape[1], "height": img.shape[0]}

    def closeup_align(self, side, index, points: dict):
        st = self.sides[side]
        c = st["closeups"].get(index)
        if not c:
            raise ValueError("Take the closeup photo first.")
        an = {a["ref"]: [a["x"], a["y"]] for a in c["anchors"]}
        use = {r: xy for r, xy in points.items() if r in an and xy}
        if len(use) < 2:
            raise ValueError("Click at least 2 of the points on the closeup.")
        refs = list(use)
        m = ph.fit([an[r] for r in refs], [use[r] for r in refs], self.profile["orient"][side])
        c.update(mapping=m, points=use)
        self._save_closeup(side, c)
        self._place(side)
        self._touch(f"{side.capitalize()} closeup {index + 1} lined up: "
                    f"{sum(1 for r in st['placed'] if r.get('via') == index)} parts checked on it")
        return self.side_state(side)

    def _save_closeup(self, side, c):
        d = self._side_dir(side) / "closeups"
        _write_json(d / f"{c['index']:02d}.json", {k: (v.to_dict() if isinstance(v, ph.Mapping) else v)
                                                    for k, v in c.items() if k not in ("img", "placed")})

    def closeup_overlay(self, side, index) -> bytes:
        c = self.sides[side]["closeups"][index]
        return ph.overlay(c["img"], c.get("placed", []), width=1400)

    # ---- rotation check -------------------------------------------------------------------
    def rotation_choice(self, side):
        st = self.sides[side]
        if not st.get("mapping"):
            raise ValueError("Line the photo up first.")
        r = ph.rotation_choice(st["img"], self.design, side, st["mapping"])
        if r and r.get("needed"):
            import base64
            r["crops"] = ["data:image/jpeg;base64," + base64.b64encode(c).decode() for c in r["crops"]]
            r["current"] = self.design["rot_sign"]
        return r

    # ---- what the page shows --------------------------------------------------------------
    def side_state(self, side) -> dict:
        st = self.sides.get(side) or {}
        if st.get("img") is None:
            return {"photo": False}
        placed = st.get("placed", [])
        tiles = st.get("tiles", [])
        closeups = st.get("closeups", {})
        return {
            "photo": True, "width": st["img"].shape[1], "height": st["img"].shape[0],
            "aligned": bool(st.get("mapping")), "points": st.get("points", {}), "how": st.get("how", ""),
            "errors": st["mapping"].errors if st.get("mapping") else [], "warnings": st.get("warnings", []),
            "placed": placed,
            "summary": {"parts": len(placed), "checkable": sum(r["checkable"] for r in placed),
                        "via_closeup": sum(1 for r in placed if "via" in r)},
            "tiles": [{**t, "taken": t["index"] in closeups,
                       "aligned": bool(closeups.get(t["index"], {}).get("mapping"))} for t in tiles],
            "closeups": [{"index": i, "refs": c["refs"], "anchors": c["anchors"], "aligned": bool(c.get("mapping")),
                          "points": c.get("points", {}), "width": c["img"].shape[1], "height": c["img"].shape[0]}
                         for i, c in sorted(closeups.items())],
        }

    def state(self) -> dict:
        d = self.design
        lines = []
        if self.bom:
            for ln in self.bom["lines"]:
                lines.append({k: ln.get(k) for k in ("key", "value", "description", "type", "refs", "kind", "why",
                                                     "chosen_kind", "hidden", "on_board")})
        return {
            "name": self.name, "messages": self.messages,
            "files": {"odb": bool(self.odb_path), "bom": bool(self.bom_path)},
            "steps": [{k: s[k] for k in ("name", "sides", "has_components", "panel")} for s in self.steps],
            "bom_header": self.bom["header"] if self.bom else [],
            "bom_columns": self.bom["columns"] if self.bom else {},
            "lines": lines, "kinds": bomlib.KINDS,
            "profile": {k: self.profile[k] for k in ("kinds", "hidden", "rot_sign", "markings", "orient", "min_px", "camera", "step", "use_tantalums")},
            "design": None if not d else {
                "name": d["name"], "units": d["units"], "units_known": d["units_known"], "outline": d["outline"],
                "fiducials": d["fiducials"], "counts": d["counts"], "rot_sign": d["rot_sign"],
                "rot_note": d.get("rot_note", ""), "rotation_votes": d["rotation_evidence"]["votes"],
                "bom_only": d["bom_only"], "odb_only": d["odb_only"][:200], "odb_only_count": len(d["odb_only"]),
                "dnp_on_board": d["dnp_on_board"], "notes": d["notes"],
                "parts": [{k: p[k] for k in ("ref", "side", "kind", "value", "package", "pin1_dir", "pin1_how", "hidden", "size")}
                          for p in d["parts"]]},
            "sides": {s: self.side_state(s) for s in ("front", "back")} if d else {},
            "project": None if not self.project else {k: self.project.get(k, "") for k in
                                                      ("uid", "board", "serial", "notes", "created", "odb_file", "bom_file")},
        }

    def write_csv(self):
        if not self.design:
            return
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ref", "side", "kind", "value", "description", "package", "x", "y", "rot", "pin1_x", "pin1_y",
                    "pin1_dir", "pin1_how", "hidden", "photo", "checkable", "reason", "px", "photo_x", "photo_y"])
        placed = {}
        for side, st in self.sides.items():
            for r in st.get("placed", []):
                placed[(ph.SIDE_LAYER[side], r["ref"])] = (side, r)
        for p in self.design["parts"]:
            side, r = placed.get((p["side"], p["ref"]), (None, None))
            p1 = [round(v, 5) for v in p["pin1"]] if p["pin1"] else ["", ""]
            w.writerow([p["ref"], p["side"], p["kind"], p["value"], p["description"], p["package"],
                        round(p["x"], 5), round(p["y"], 5), p["rot"], *p1,
                        p["pin1_dir"], p["pin1_how"], p.get("hidden") or "", side or "",
                        "" if r is None else ("yes" if r["checkable"] else "no"), "" if r is None else r["reason"],
                        "" if r is None else r["px"], *(r["centre"] if r else ["", ""])])
        (self.dir / "parts_map.csv").write_text(buf.getvalue())
