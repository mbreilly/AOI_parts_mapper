"""The local web page: python -m aoimap  (opens http://localhost:8090).

The camera only works on a page served from localhost (or https), which is why this is a small
local server rather than a file to double-click."""

import json
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
from . import photo as ph
from . import projects as pj
from .board import Board
from .bom import BomError
from .odb import OdbError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 400 * 1024 * 1024


class App:
    def __init__(self, data_dir: Path, db_config: Path | None = dblib.DEFAULT_CONFIG):
        self.board = Board(data_dir)
        self.history = dblib.History(data_dir, db_config)
        self.board.on_change = self.history.changed
        self.checks = ck.Checks(data_dir)
        self.checks.on_change = self.history.changed_check
        self.lock = threading.Lock()


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
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
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
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if parts == ["favicon.ico"]:
                    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#20232C"/><rect x="3" y="4" width="10" height="8" fill="none" stroke="#E0A33A" stroke-width="1.6"/><circle cx="5" cy="6" r="1.2" fill="#E3E3E3"/></svg>'
                    return self._send(200, svg, "image/svg+xml", cache=True)
                if parts[0] == "static" and len(parts) == 2 and (STATIC / parts[1]).is_file() and "/" not in parts[1]:
                    ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "html": "text/html; charset=utf-8"}.get(parts[1].rsplit(".", 1)[-1], "application/octet-stream")
                    return self._send(200, (STATIC / parts[1]).read_bytes(), ctype, cache=True)
                if parts[0] != "api":
                    return self._err("Not found", 404)
                if parts[1:] == ["canon"]:
                    return self._json(canonlib.shared().detect())
                if parts[1:] == ["canon", "preview.jpg"]:
                    try:
                        return self._send(200, canonlib.shared().preview(), "image/jpeg")
                    except canonlib.CameraError as e:
                        return self._err(str(e), 503)
                if parts[1:3] == ["history", "checks"]:
                    return self._json(app.history.search_checks(q.get("q", ""), q.get("limit", 50)))
                if parts[1:2] == ["history"]:
                    return self._history_get(parts[2:], q)
                if parts[1:2] == ["check"]:
                    with app.lock:
                        return self._check_get(parts[2:], q)
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
                            return self._send(200, ph.overlay(st["img"], st["placed"], width=int(q.get("w", 1600)),
                                                              highlight=hl, show_marks=marks), "image/jpeg")
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
                return self._json({**h.search(q.get("q", ""), q.get("limit", 50)),
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

        def _check_get(self, r, q):
            c = app.checks
            if r == ["state"]:
                return self._json(c.state())
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
                    c.answer(side, j["ref"], j.get("answer"))
                else:
                    return self._err("Not found", 404)
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
                        return self._json(b.state())
                    if r[:1] == ["check"]:
                        return self._check_post(r[1:])
                    if len(r) == 3 and r[0] == "history":
                        uid, what = r[1], r[2]
                        if what == "open":
                            b.open_project(uid)
                            return self._json(b.state())
                        if what == "new":
                            b.new_from(uid)
                            return self._json(b.state())
                        if what == "info":
                            j = self._jbody()
                            b.set_info(uid, j.get("serial"), j.get("notes"))
                            return self._json(app.history.detail(uid))
                        if what == "reference":
                            b.set_reference(uid, bool(self._jbody().get("on", True)))
                            return self._json(app.history.detail(uid))
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
