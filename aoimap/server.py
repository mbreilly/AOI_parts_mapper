"""The local web page: python -m aoimap  (opens http://localhost:8090).

The camera only works on a page served from localhost (or https), which is why this is a small
local server rather than a file to double-click."""

import hashlib
import json
import re
import threading
import traceback
import webbrowser
from argparse import ArgumentParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import cv2

from . import canon as canonlib
from . import check as ck
from . import db as dblib
from . import groups as gr
from . import models as md
from . import photo as ph
from . import projects as pj
from . import refchange as rc
from . import teach as tl
from . import viewer as vw
from .board import Board
from .bom import BomError
from .odb import OdbError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 400 * 1024 * 1024


ASSET_RE = re.compile(r"""(static/[\w.-]+\.(?:js|css))(\?v=[^"']*)?""")


def asset_version(name: str) -> str:
    """Changes whenever the file does, so a browser can't run an old app.js under a new page."""
    f = STATIC / name
    return hashlib.sha1(f.read_bytes()).hexdigest()[:10] if f.is_file() else "0"


def index_html() -> bytes:
    """The page, with every app.js / app.css it loads stamped with that file's version."""
    html = (STATIC / "index.html").read_text()
    return ASSET_RE.sub(lambda m: f"{m.group(1)}?v={asset_version(m.group(1).split('/', 1)[1])}", html).encode()


class App:
    def __init__(self, data_dir: Path, db_config: Path | None = dblib.DEFAULT_CONFIG):
        self.board = Board(data_dir)
        self.history = dblib.History(data_dir, db_config)
        self.board.on_change = self.history.changed
        self.board.on_profile = self.history.changed_profile
        ck.LEARNED_LISTENERS.append(self.history.changed_learned)
        rc.LISTENERS.append(self.history.changed_refchange)
        self.checks = ck.Checks(data_dir)
        self.checks.on_change = self.history.changed_check
        self.lock = threading.Lock()
        self.data = Path(data_dir)
        _, changed = gr.ensure(self.data)
        if changed:
            self.history.changed_groups()
        self.board.group_info = self.group_info
        self.models_seen = {}       # design -> metrics.json time last sent to the database
        self.open_last()

    def models(self, q: dict) -> dict:
        """The MODELS page: each board design of the project chosen (or every one) and its models."""
        boards = self.boards_for(q)
        if boards is None:
            boards = {b for g in gr.load(self.data)["projects"] for b in g["boards"]}
        out = []
        for b in sorted(boards):
            st = md.status(self.data, b)
            f = md.folder(self.data, b) / "metrics.json"
            t = f.stat().st_mtime_ns if f.is_file() else 0
            if t and self.models_seen.get(b) != t:      # trained since: the database copy follows
                self.models_seen[b] = t
                self.history.changed_models(b)
            out.append(st)
        return {"boards": out, "jobs": md.JOBS, "minimum_parts": md.MIN_PARTS,
                "extractor": (self.data / "models" / md.EXTRACTOR).is_file()}

    # ---- changing the reference (refchange.py) -------------------------------------------------
    def refresh_project(self, uid: str):
        """A project's placed parts brought up to the design's choices (as opening it does)."""
        b = self.board
        if b.project and b.project["uid"] == uid:
            b.open_project(uid)
            return
        o = Board(self.data)
        o.on_change = self.history.changed
        o.open_project(uid)

    def projects_changed(self, uid: str):
        """A project changed on disk outside the board open here (its reference mark): the open copy follows."""
        b = self.board
        if b.project and b.project["uid"] == uid:
            b.project = pj.load(self.data, uid)
        self.history.changed(uid)

    def reference(self, uid: str, on: bool) -> dict:
        """Marks a project the reference; when the design has one already, a change to review instead."""
        b = self.board
        p = pj.load(self.data, uid)
        cur = next((r for r in ck.references(self.data) if r["board"] == p["board"] and r["uid"] != uid), None)
        if on and cur and not p.get("reference"):
            c = rc.start(self.data, cur["uid"], uid, "switch", self.refresh_project)
            return {"review": rc.brief(c)}
        b.set_reference(uid, on)
        return {}

    def start_review(self, uid: str) -> dict:
        """A review for a board whose own photo changed while it was the reference (or any board against
        the reference in use)."""
        p = pj.load(self.data, uid)
        cur = next((r for r in ck.references(self.data) if r["board"] == p["board"] and r["uid"] != uid), None)
        if not cur:
            self.board.set_reference(uid, True)
            return {}
        c = rc.start(self.data, cur["uid"], uid, "photo" if p.get("replaced_reference") else "switch", self.refresh_project)
        return {"review": rc.brief(c)}

    # ---- the project chosen above the tabs (groups.py) ------------------------------------------
    def group(self) -> dict | None:
        d = gr.load(self.data)
        return gr.get(d, d["current"])

    def group_info(self) -> dict | None:
        g = self.group()
        return {k: g[k] for k in ("id", "name", "boards")} if g else None

    def boards_for(self, q: dict):
        """The board designs a list is limited to: group=all for every project, group=<id> for that
        one, and none given for the project chosen now."""
        want = q.get("group", "")
        if want == "all":
            return None
        d = gr.load(self.data)
        g = gr.get(d, want or d["current"])
        return set(g["boards"]) if g else None

    def open_last(self):
        """Opens the board the chosen project had open last (or its newest), so the tabs show it."""
        d = gr.load(self.data)
        g = gr.get(d, d["current"])
        if not g:
            return
        ms = gr.mapped(self.data, set(g["boards"]))
        want = d["open"].get(g["id"])
        for uid in ([want] if want else []) + [p["uid"] for p in ms if p["uid"] != want]:
            try:
                self.board.open_project(uid)
                return
            except (ValueError, OSError, OdbError, BomError):
                traceback.print_exc()
        self.board.close()

    def switch(self, gid: str):
        """Chooses a project. A board or check open from another project is put away (both are saved)."""
        gr.set_current(self.data, gid)
        g = self.group()
        if self.board.name not in g["boards"] or not self.board.project:
            self.open_last()
        c = self.checks.cur
        if c and c["board"] not in g["boards"]:
            self.checks.close()
        self.history.changed_groups()

    def new_group(self, name: str):
        gr.create(self.data, name)
        self.board.close()
        if self.checks.cur:
            self.checks.close()
        self.history.changed_groups()

    def adopt(self):
        """After a board is loaded or opened: its design joins the chosen project if it is in none,
        or the project it is in is chosen (and the page told)."""
        b = self.board
        if not b.name:
            return
        d = gr.load(self.data)
        owner, cur = gr.of_board(d, b.name), gr.get(d, d["current"])
        if owner and cur and owner["id"] != cur["id"]:
            gr.set_current(self.data, owner["id"])
            if self.checks.cur and self.checks.cur["board"] not in owner["boards"]:
                self.checks.close()
            b.messages.append(f"{b.name} is in project {owner['name']}, so that project is chosen now.")
        elif not owner:
            if cur:
                gr.add_board(self.data, cur["id"], b.name)
            else:
                gr.create(self.data, b.name, [b.name])
        if b.project:
            gr.set_open(self.data, self.group()["id"], b.project["uid"])
        self.history.changed_groups()


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "aoimap"

        def log_message(self, fmt, *args):
            pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---- replies ----
        def _send(self, code, body: bytes, ctype: str, cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass   # the page moved on before the image arrived

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _err(self, msg, code=400):
            self._json({"error": msg}, code)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ValueError("That file is too big.")
            return self.rfile.read(n)

        def _jbody(self):
            b = self._body()
            return json.loads(b or b"{}")

        # ---- routes ----
        def do_GET(self):
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if not parts or parts == ["index.html"]:
                    return self._send(200, index_html(), "text/html; charset=utf-8")
                if parts == ["favicon.ico"]:
                    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#20232C"/><rect x="3" y="4" width="10" height="8" fill="none" stroke="#E0A33A" stroke-width="1.6"/><circle cx="5" cy="6" r="1.2" fill="#E3E3E3"/></svg>'
                    return self._send(200, svg, "image/svg+xml", cache=True)
                if parts[0] == "static" and len(parts) == 2 and (STATIC / parts[1]).is_file() and "/" not in parts[1]:
                    ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "html": "text/html; charset=utf-8"}.get(parts[1].rsplit(".", 1)[-1], "application/octet-stream")
                    # versioned (?v=, see index_html) may be kept; anything else is checked every time
                    return self._send(200, (STATIC / parts[1]).read_bytes(), ctype, cache="v" in q)
                if parts[0] != "api":
                    return self._err("Not found", 404)
                if parts[1:] == ["canon"]:
                    return self._json(canonlib.shared().detect())
                if parts[1:] == ["canon", "preview.jpg"]:
                    try:
                        return self._send(200, canonlib.shared().preview(), "image/jpeg")
                    except canonlib.CameraError as e:
                        return self._err(str(e), 503)
                if parts[1:] == ["groups"]:
                    return self._json(gr.view(app.data))
                if parts[1:3] == ["history", "checks"]:
                    return self._json(app.history.search_checks(q.get("q", ""), q.get("limit", 50), app.boards_for(q)))
                if parts[1:2] == ["history"]:
                    return self._history_get(parts[2:], q)
                if parts[1:] == ["models"]:
                    return self._json(app.models(q))
                if parts[1:2] == ["check"]:
                    with app.lock:
                        return self._check_get(parts[2:], q)
                if parts[1:2] == ["part"]:
                    with app.lock:
                        return self._part_get(parts[2:], q)
                if parts[1:2] == ["refchange"]:
                    return self._refchange_get(parts[2:], q)
                b = app.board
                with app.lock:
                    if parts[1:] == ["state"]:
                        return self._json(b.state())
                    if len(parts) >= 4 and parts[1] == "side" and parts[2] in ("front", "back"):
                        side, what = parts[2], parts[3]
                        st = b.sides.get(side) or {}
                        if what == "photo.jpg" and st.get("img") is not None:
                            return self._send(200, (b.dir / side / "reference.jpg").read_bytes(), "image/jpeg")
                        if what == "map.jpg" and st.get("mapping"):
                            hl = q.get("highlight")
                            marks = q.get("marks", "1") != "0"
                            taught = tl.answers(b.data, b.name, f"project/{b.project['uid']}/{side}") if b.project else {}
                            return self._send(200, ph.overlay(st["img"], st["placed"], width=int(q.get("w", 1600)),
                                                              highlight=hl, show_marks=marks, answers=taught,
                                                              outlines=q.get("outlines", "1") != "0"), "image/jpeg")
                        if what == "ghost.svg":
                            return self._send(200, ph.ghost_svg(b.design, side, b.profile["orient"][side]).encode(),
                                              "image/svg+xml")
                        if what == "anchors":
                            return self._json(b.anchors(side))
                        if what == "rotation":
                            return self._json(b.rotation_choice(side))
                        if what == "closeup" and len(parts) == 6:
                            i = int(parts[4])
                            c = st.get("closeups", {}).get(i)
                            if not c:
                                return self._err("No closeup photo yet", 404)
                            if parts[5] == "photo.jpg":
                                return self._send(200, (b.dir / side / "closeups" / f"{i:02d}.jpg").read_bytes(), "image/jpeg")
                            if parts[5] == "map.jpg":
                                return self._send(200, b.closeup_overlay(side, i), "image/jpeg")
                    if parts[1:] == ["parts_map.csv"] and b.dir and (b.dir / "parts_map.csv").is_file():
                        self.send_response(200)
                        data = (b.dir / "parts_map.csv").read_bytes()
                        self.send_header("Content-Type", "text/csv")
                        self.send_header("Content-Disposition", f'attachment; filename="{b.name}_parts_map.csv"')
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        return self.wfile.write(data)
                    if parts[1:2] == ["viewer"] and b.design:
                        return self._viewer_get(parts[2:], q)
                    if parts[1:] == ["design.json"] and b.dir:
                        return self._send(200, (b.dir / "design.json").read_bytes(), "application/json")
                return self._err("Not found", 404)
            except (ValueError, OdbError, BomError, KeyError) as e:
                return self._err(str(e))
            except Exception as e:   # noqa: BLE001 - shown to the user, logged for us
                traceback.print_exc()
                return self._err(f"Something went wrong: {e}", 500)

        def _history_get(self, r, q):
            """The history page reads project folders and the database, not the board being mapped,
            so it doesn't wait for the board's lock."""
            h = app.history
            if not r:
                return self._json({**h.search(q.get("q", ""), q.get("limit", 50), app.boards_for(q)),
                                   "current": app.board.project["uid"] if app.board.project else None})
            uid = r[0]
            if len(r) == 1:
                return self._json(h.detail(uid))
            if r[1] == "file" and len(r) > 2:
                rel = "/".join(r[2:])
                if q.get("thumb"):
                    return self._send(200, pj.thumb(app.board.data, uid, rel, int(q["thumb"])), "image/jpeg", cache=True)
                f = pj.file_path(app.board.data, uid, rel)
                ctype = {".jpg": "image/jpeg", ".json": "application/json", ".csv": "text/csv"}.get(f.suffix, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(f.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                if q.get("download"):
                    self.send_header("Content-Disposition", f'attachment; filename="{q["download"].replace(chr(34), "")}"')
                self.end_headers()
                with f.open("rb") as fh:
                    while chunk := fh.read(1 << 20):
                        self.wfile.write(chunk)
                return None
            return self._err("Not found", 404)

        def _refchange_get(self, r, q):
            """Reference changes: the list (of a design, or the project chosen), one change, a crop."""
            if not r:
                boards = app.boards_for(q) if not q.get("board") else {q["board"]}
                return self._json({"changes": [c for c in rc.changes(app.data) if boards is None or c["board"] in boards]})
            if len(r) == 1:
                return self._json(rc.view(app.data, r[0]))
            if len(r) == 2 and r[1] == "crop":
                c = rc.load(app.data, r[0])
                side, ref = q["key"].split("/", 1)
                which = "reference" if q.get("which") == "old" else "board"
                return self._send(200, tl.crop(app.data, f"project/{c['new']}/{side}", ref, which, c["old"]), "image/jpeg", cache=True)
            return self._err("Not found", 404)

        def _refchange_post(self, r):
            j = self._jbody()
            if r == ["start"]:
                return self._json(app.start_review(j["uid"]))
            cid = r[0] if r else ""
            what = r[1] if len(r) == 2 else ""
            if what == "decide":
                return self._json(rc.decide(app.data, cid, list(j.get("keys") or []), j.get("choice") or None))
            if what == "approve":
                return self._json(rc.approve_matching(app.data, cid))
            if what == "finish":
                return self._json(rc.finish(app.data, cid, app.projects_changed))
            if what == "undo":
                return self._json(rc.undo(app.data, cid, app.projects_changed))
            if what == "cancel":
                v = rc.cancel(app.data, cid)
                app.projects_changed(v["new"])
                return self._json(v)
            return self._err("Not found", 404)

        def _viewer_get(self, r, q):
            """The board viewer on the Map tab (viewer.py). The page asks with ?v=<uid> so geometry and
            pins may be kept by the browser; the photo's URL carries its stamp."""
            b = app.board
            if r == ["geometry"]:
                return self._json(vw.geometry(b))
            if r == ["pins"]:
                return self._send(200, json.dumps(vw.pins(b), separators=(",", ":")).encode(), "application/json")
            if r == ["status"]:
                return self._json(vw.status(b))
            if len(r) == 2 and r[0] == "photo" and r[1] in ("front.jpg", "back.jpg"):
                return self._send(200, vw.photo(b, r[1][:-4]), "image/jpeg", cache="v" in q)
            return self._err("Not found", 404)

        def _part_get(self, r, q):
            """Any part on any lined-up photo (see teach.py)."""
            data = app.board.data
            if r == ["list"]:
                return self._json({"parts": tl.part_list(data, q["uid"]), "reasons": ck.REASONS})
            if r == ["sources"]:
                return self._json({"sources": tl.sources(data, q["board"], q["side"])})
            if r == ["info"]:
                return self._json(tl.inspect(data, q["src"], q["ref"]))
            if r == ["crop"]:
                return self._send(200, tl.crop(data, q["src"], q["ref"], q.get("which", "board")), "image/jpeg")
            if r == ["thumb"]:     # the URL carries the photo's stamp, so the browser may keep it
                return self._send(200, tl.thumb(data, q["src"], q["ref"], int(q.get("size", 120))), "image/jpeg", cache=True)
            return self._err("Not found", 404)

        def _part_post(self, r):
            b, data = app.board, app.board.data
            j = self._jbody()
            if r == ["pick"]:
                return self._json(tl.pick(data, j["src"], float(j["fx"]), float(j["fy"])))
            if r == ["teach"]:
                src, ref, ans = j["src"], j["ref"], j.get("answer")
                if ans not in ("good", "bad"):
                    raise ValueError("Answer good or bad.")
                c = app.checks.cur
                kind, uid, side = src.split("/") if src.count("/") == 2 else ("", "", "")
                res = None
                if kind == "check" and c and c["uid"] == uid:
                    res = next((x for x in c["sides"].get(side, {}).get("results", []) if x["ref"] == ref), None)
                if res:     # a part of the check open here: the answer is the check's answer too
                    app.checks.answer(side, ref, "correct" if ans == "good" else "wrong", j.get("note", ""),
                                      res["status"] == "ok" and ans == "bad", j.get("reason", ""))
                else:
                    b.teach(src, ref, ans, j.get("reason", ""), j.get("note", ""), j.get("kind"))
                return self._json(tl.inspect(data, src, ref))
            if r == ["ns"]:     # not stuffed: on this board, or every board of the design; on=false undoes it
                src, ref, on, every = j["src"], j["ref"], j.get("on", True) is not False, bool(j.get("all"))
                c = app.checks.cur
                kind, uid, side = src.split("/") if src.count("/") == 2 else ("", "", "")
                if kind == "check" and c and c["uid"] == uid:
                    app.checks.set_ns(side, ref, on, every)
                else:
                    b.set_ns(src, ref, on, every)
                return self._json(tl.inspect(data, src, ref))
            if r == ["extra"]:
                b.set_extra(j["ref"], j.get("kind"), j.get("wait"), bool(j.get("remove")), j.get("board"))
                return self._json(b.state() if b.design else {"ok": True})
            return self._err("Not found", 404)

        def _check_get(self, r, q):
            c = app.checks
            if r == ["state"]:
                return self._json(c.state())
            if r == ["learned"]:
                boards = app.boards_for(q)
                return self._json({"boards": [x for x in c.learned_boards() if boards is None or x["board"] in boards]})
            if len(r) == 3 and r[0] == "learned":
                return self._json(c.learned(r[1], r[2]))
            if len(r) == 4 and r[0] == "learned":
                return self._send(200, ck.example_path(app.board.data, r[1], r[2], r[3]).read_bytes(), "image/jpeg", cache=True)
            if len(r) == 3 and r[0] == "side" and r[2] == "anchors":
                return self._json(c.anchors(r[1]))
            if len(r) == 1:
                return self._json(c.detail(r[0]))
            if len(r) == 4 and r[1] == "file":
                f = ck.file_path(app.board.data, r[0], f"{r[2]}/{r[3]}")
                if q.get("thumb"):
                    w = max(80, min(1600, int(q["thumb"])))
                    img = cv2.imread(str(f), cv2.IMREAD_REDUCED_COLOR_2)
                    img = cv2.resize(img, (w, round(img.shape[0] * w / img.shape[1])), interpolation=cv2.INTER_AREA)
                    return self._send(200, cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes(), "image/jpeg")
                return self._send(200, f.read_bytes(), "image/jpeg")
            if len(r) == 4 and r[1] == "crop":
                return self._send(200, c.crop(r[0], r[2], r[3], q.get("which", "board")), "image/jpeg", cache=False)
            return self._err("Not found", 404)

        def _check_post(self, r):
            c = app.checks
            if r == ["start"]:
                j = self._jbody()
                c.start(j["reference"], j.get("serial", ""), j.get("notes", ""))
            elif r == ["open"]:
                c.open(self._jbody()["uid"])
                d = gr.load(app.data)
                owner = gr.of_board(d, c.cur["board"])
                if owner and owner["id"] != d["current"]:
                    app.switch(owner["id"])       # a check of another project (from All projects) chooses it
            elif r == ["close"]:
                c.close()
            elif len(r) == 3 and r[0] == "side" and r[1] in ("front", "back"):
                side, what = r[1], r[2]
                if what == "photo":
                    c.photo(side, self._body())
                elif what == "snap":
                    xy, snapped = c.snap(side, self._jbody()["xy"])
                    return self._json({"xy": xy, "snapped": snapped})
                elif what == "align":
                    c.align_points(side, self._jbody()["points"])
                elif what == "answer":
                    j = self._jbody()
                    c.answer(side, j["ref"], j.get("answer"), j.get("note"), bool(j.get("missed")), j.get("reason"))
                elif what == "ns":
                    j = self._jbody()
                    c.set_ns(side, j["ref"], j.get("on", True) is not False, bool(j.get("all")))
                else:
                    return self._err("Not found", 404)
            elif len(r) == 4 and r[0] == "learned" and r[3] in ("delete", "trust"):
                j = self._jbody()
                if r[3] == "delete":
                    c.forget(r[1], r[2], j["id"])
                else:
                    c.trust_reference(r[1], r[2], j["reference"], j["ref"])
                return self._json(c.learned(r[1], r[2]))
            elif len(r) == 2 and r[1] == "info":
                j = self._jbody()
                c.set_info(r[0], j.get("serial"), j.get("notes"))
                return self._json(c.detail(r[0]))
            else:
                return self._err("Not found", 404)
            return self._json(c.state())

        def do_POST(self):
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            b = app.board
            try:
                if parts[:1] != ["api"]:
                    return self._err("Not found", 404)
                if parts[1:] == ["canon", "capture"]:
                    try:
                        return self._send(200, canonlib.shared().capture(), "image/jpeg")
                    except canonlib.CameraError as e:
                        return self._err(str(e), 503)
                with app.lock:
                    r = parts[1:]
                    if r == ["upload"]:
                        kind = q.get("kind")
                        if kind not in ("odb", "bom"):
                            return self._err("Upload kind must be odb or bom.")
                        b.add_file(kind, q.get("name", kind), self._body())
                        app.adopt()
                        return self._json(b.state())
                    if len(r) == 2 and r[0] == "groups":
                        j = self._jbody()
                        if r[1] == "switch":
                            app.switch(j["id"])
                        elif r[1] == "new":
                            app.new_group(j.get("name", ""))
                        elif r[1] == "rename":
                            gr.rename(app.data, j["id"], j.get("name", ""))
                            app.history.changed_groups()
                        else:
                            return self._err("Not found", 404)
                        return self._json({"groups": gr.view(app.data), "state": b.state()})
                    if r == ["models", "train"]:
                        j = self._jbody()
                        if j.get("board") not in {b for g in gr.load(app.data)["projects"] for b in g["boards"]}:
                            return self._err("No such board design.")
                        jobs = [x for x in (j.get("jobs") or md.JOBS) if x in md.JOBS]
                        md.start(app.data, j["board"], jobs or md.JOBS)
                        return self._json(app.models({}))
                    if r[:1] == ["check"]:
                        return self._check_post(r[1:])
                    if r[:1] == ["part"]:
                        return self._part_post(r[1:])
                    if r[:1] == ["refchange"]:
                        return self._refchange_post(r[1:])
                    if len(r) == 3 and r[0] == "history":
                        uid, what = r[1], r[2]
                        if what == "open":
                            b.open_project(uid)
                            app.adopt()
                            return self._json(b.state())
                        if what == "new":
                            b.new_from(uid)
                            app.adopt()
                            return self._json(b.state())
                        if what == "info":
                            j = self._jbody()
                            b.set_info(uid, j.get("serial"), j.get("notes"))
                            return self._json(app.history.detail(uid))
                        if what == "reference":
                            out = app.reference(uid, bool(self._jbody().get("on", True)))
                            return self._json({**app.history.detail(uid), **out})
                    if r == ["columns"]:
                        b.set_columns(self._jbody())
                        return self._json(b.state())
                    if r == ["lines"]:
                        b.set_lines(self._jbody())
                        return self._json(b.state())
                    if r == ["option"]:
                        j = self._jbody()
                        b.set_option(j["key"], j.get("value"))
                        return self._json(b.state())
                    if len(r) >= 3 and r[0] == "side" and r[1] in ("front", "back"):
                        side, what = r[1], r[2]
                        if what == "photo":
                            b.set_photo(side, self._body())
                            return self._json(b.state())
                        if what == "orient":
                            j = self._jbody()
                            b.set_orient(side, j["orient"])
                            return self._json(b.state())
                        if what == "snap":
                            j = self._jbody()
                            xy, snapped = b.snap(side, j["xy"])
                            return self._json({"xy": xy, "snapped": snapped})
                        if what == "auto":
                            return self._json({"points": b.auto_find(side)})
                        if what == "align":
                            b.align(side, self._jbody()["points"])
                            return self._json(b.state())
                        if what == "closeup" and len(r) == 5:
                            i = int(r[3])
                            if r[4] == "photo":
                                b.closeup_photo(side, i, self._body())
                                return self._json(b.state())
                            if r[4] == "snap":
                                c = b.sides[side]["closeups"][i]
                                xy, snapped = ph.snap(c["img"], self._jbody()["xy"])
                                return self._json({"xy": xy, "snapped": snapped})
                            if r[4] == "align":
                                b.closeup_align(side, i, self._jbody()["points"])
                                return self._json(b.state())
                    return self._err("Not found", 404)
            except (ValueError, OdbError, BomError, KeyError) as e:
                return self._err(str(e))
            except Exception as e:   # noqa: BLE001
                traceback.print_exc()
                return self._err(f"Something went wrong: {e}", 500)

    return H


def main(argv=None):
    ap = ArgumentParser(prog="python -m aoimap", description="Map the polarised parts of a PCB from its ODB++ and BOM.")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--data", type=Path, default=Path.home() / "aoimap-data", help="where boards are kept")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--db", type=Path, default=dblib.DEFAULT_CONFIG,
                    help="MySQL option file with the database login (default ~/.config/aoimap/db.cnf)")
    ap.add_argument("--no-db", action="store_true", help="keep the history in the project folders only")
    a = ap.parse_args(argv)
    app = App(a.data, None if a.no_db else a.db)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    url = f"http://localhost:{a.port}/"
    print(f"AOI parts mapper at {url}  (boards kept in {a.data})  Ctrl+C to stop")
    print(f"History: {app.history.status}")
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
