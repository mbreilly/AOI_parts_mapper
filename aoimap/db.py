"""The project history in MariaDB (or MySQL).

The project folders are complete on their own (see projects.py); the database keeps the same
information so the history page can list and search every board mapped on this station. Writes go
through a background thread a couple of seconds after the last change, so a burst of clicks makes
one write and the page never waits for the database. If the database can't be reached the app
carries on, the history page reads the folders instead, and the database catches up from the
folders the next time it can be reached.

The login is read from a MySQL option file, by default ~/.config/aoimap/db.cnf:

    [client]
    user=aoimap
    password=...
    database=aoimap
    host=localhost
"""

import threading
import time
import traceback
from pathlib import Path

from . import check as ck
from . import projects as pj

try:
    import pymysql
except ImportError:   # the app still runs, without the database
    pymysql = None

DEFAULT_CONFIG = Path.home() / ".config" / "aoimap" / "db.cnf"
SCHEMA_VERSION = 1

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS aoi_schema (version INT NOT NULL) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS aoi_projects (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        uid VARCHAR(80) NOT NULL UNIQUE,
        board VARCHAR(128) NOT NULL,
        serial VARCHAR(128) NOT NULL DEFAULT '',
        notes TEXT,
        station VARCHAR(64) NOT NULL DEFAULT '',
        odb_file VARCHAR(255) NOT NULL DEFAULT '',
        bom_file VARCHAR(255) NOT NULL DEFAULT '',
        units VARCHAR(8) NOT NULL DEFAULT '',
        parts INT NOT NULL DEFAULT 0,
        front_parts INT NOT NULL DEFAULT 0, front_photo TINYINT(1) NOT NULL DEFAULT 0,
        front_aligned TINYINT(1) NOT NULL DEFAULT 0, front_checkable INT NULL,
        back_parts INT NOT NULL DEFAULT 0, back_photo TINYINT(1) NOT NULL DEFAULT 0,
        back_aligned TINYINT(1) NOT NULL DEFAULT 0, back_checkable INT NULL,
        closeups INT NOT NULL DEFAULT 0,
        folder VARCHAR(512) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        KEY board (board), KEY serial (serial), KEY updated_at (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS aoi_project_files (
        project_id INT UNSIGNED NOT NULL,
        path VARCHAR(255) NOT NULL,
        kind VARCHAR(16) NOT NULL,
        side VARCHAR(8) NOT NULL DEFAULT '',
        bytes BIGINT NOT NULL,
        modified DATETIME NOT NULL,
        PRIMARY KEY (project_id, path),
        FOREIGN KEY (project_id) REFERENCES aoi_projects (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS aoi_project_log (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        project_id INT UNSIGNED NOT NULL,
        at DATETIME NOT NULL,
        what VARCHAR(500) NOT NULL,
        KEY project_at (project_id, at),
        FOREIGN KEY (project_id) REFERENCES aoi_projects (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS aoi_project_parts (
        project_id INT UNSIGNED NOT NULL,
        side VARCHAR(8) NOT NULL,
        ref VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL DEFAULT '',
        value VARCHAR(255) NOT NULL DEFAULT '',
        checkable TINYINT(1) NOT NULL,
        via_closeup INT NULL,
        reason VARCHAR(255) NOT NULL DEFAULT '',
        px FLOAT NULL,
        PRIMARY KEY (project_id, side, ref),
        KEY ref (ref),
        FOREIGN KEY (project_id) REFERENCES aoi_projects (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS aoi_checks (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        uid VARCHAR(80) NOT NULL UNIQUE,
        board VARCHAR(128) NOT NULL,
        reference_uid VARCHAR(80) NOT NULL,
        serial VARCHAR(128) NOT NULL DEFAULT '',
        notes TEXT,
        station VARCHAR(64) NOT NULL DEFAULT '',
        result VARCHAR(8) NOT NULL,
        front_checked INT NOT NULL DEFAULT 0, front_flagged INT NOT NULL DEFAULT 0,
        back_checked INT NOT NULL DEFAULT 0, back_flagged INT NOT NULL DEFAULT 0,
        wrong_parts TEXT,
        log TEXT,
        folder VARCHAR(512) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        KEY board (board), KEY serial (serial), KEY updated_at (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS aoi_check_parts (
        check_id INT UNSIGNED NOT NULL,
        side VARCHAR(8) NOT NULL,
        ref VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL DEFAULT '',
        value VARCHAR(255) NOT NULL DEFAULT '',
        status VARCHAR(10) NOT NULL,
        answer VARCHAR(10) NULL,
        why VARCHAR(255) NOT NULL DEFAULT '',
        score FLOAT NULL, turned FLOAT NULL, mark FLOAT NULL,
        PRIMARY KEY (check_id, side, ref),
        KEY ref (ref),
        FOREIGN KEY (check_id) REFERENCES aoi_checks (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

LIST_COLS = ("uid, board, serial, notes, station, odb_file, bom_file, units, parts, closeups, "
             "front_parts, front_photo, front_aligned, front_checkable, back_parts, back_photo, back_aligned, back_checkable, "
             "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created, DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS updated")


def _row(r: dict) -> dict:
    out = {k: r[k] for k in ("uid", "board", "serial", "notes", "station", "created", "updated", "odb_file", "bom_file",
                             "units", "parts", "closeups")}
    out["notes"] = out["notes"] or ""
    for side in pj.SIDES:
        out[side] = {"parts": r[f"{side}_parts"], "photo": bool(r[f"{side}_photo"]), "aligned": bool(r[f"{side}_aligned"]),
                     "checkable": r[f"{side}_checkable"]}
    return out


def like(pattern: str) -> str:
    """'*' matches any part; without one, the text can be anywhere."""
    p = pattern.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return p.replace("*", "%").replace("?", "_") if ("*" in p or "?" in p) else f"%{p}%"


class History:
    def __init__(self, data: Path, config: Path | None = DEFAULT_CONFIG, delay: float = 2.0):
        self.data = Path(data)
        self.config = Path(config) if config else None
        self.delay = delay
        self.status = "starting"
        self.ok = False
        self._conn = None
        self._dirty = {}            # uid -> time of the last change
        self._cv = threading.Condition()
        self._db_lock = threading.Lock()
        if not self.config:
            self.status = "Database turned off (--no-db)."
        elif pymysql is None:
            self.status = "PyMySQL isn't installed (pip install pymysql), so history comes from the project folders."
        elif not self.config.is_file():
            self.status = f"No database login in {self.config}, so history comes from the project folders."
        else:
            self.status = f"Connecting to the database with the login in {self.config}."
            threading.Thread(target=self._worker, name="aoimap-db", daemon=True).start()

    # ---- connection ---------------------------------------------------------------------------
    def _connect(self):
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:   # noqa: BLE001 - reconnect below
                self._conn = None
        self._conn = pymysql.connect(read_default_file=str(self.config), read_default_group="client", charset="utf8mb4",
                                     cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=5)
        with self._conn.cursor() as c:
            for sql in SCHEMA:
                c.execute(sql)
            c.execute("SELECT MAX(version) AS v FROM aoi_schema")
            if not (c.fetchone() or {}).get("v"):
                c.execute("INSERT INTO aoi_schema (version) VALUES (%s)", (SCHEMA_VERSION,))
        self._conn.commit()
        return self._conn

    def _run(self, fn):
        """Runs fn(cursor) on the database, or returns None when it can't be reached."""
        if not self.config or pymysql is None or not self.config.is_file():
            return None
        with self._db_lock:
            try:
                conn = self._connect()
                with conn.cursor() as c:
                    out = fn(c)
                conn.commit()
                if not self.ok:
                    self.ok, self.status = True, "Connected to the database."
                return out
            except Exception as e:   # noqa: BLE001 - shown on the history page, the app carries on
                self.ok, self.status = False, f"Can't reach the database ({e}), so history comes from the project folders."
                try:
                    self._conn and self._conn.close()
                except Exception:   # noqa: BLE001
                    pass
                self._conn = None
                return None

    # ---- writing --------------------------------------------------------------------------------
    def changed(self, uid: str):
        """A project changed; it is written to the database a moment later."""
        with self._cv:
            self._dirty[uid] = time.monotonic()
            self._cv.notify()

    def _worker(self):
        self.catch_up()
        while True:
            with self._cv:
                self._cv.wait(timeout=self.delay if self._dirty else 60)
                t = time.monotonic()
                due = [u for u, at in self._dirty.items() if t - at >= self.delay]
                for u in due:
                    self._dirty.pop(u)
            if not self.ok:
                self.catch_up()   # every folder newer than the database, these included
                continue
            for u in due:
                self.write(u)

    def flush(self):
        """Writes waiting changes now, so the list is up to date."""
        with self._cv:
            due = list(self._dirty)
            self._dirty.clear()
        for u in due:
            self.write(u)

    def changed_check(self, uid: str):
        self.changed("check:" + uid)

    def catch_up(self):
        """Writes every project and check folder the database doesn't have, or has an older copy of."""
        for table, ids, load, key in (("aoi_projects", pj.uids, pj.load, ""), ("aoi_checks", ck.uids, ck.load, "check:")):
            known = self._run(lambda c: (c.execute(f"SELECT uid, DATE_FORMAT(updated_at, '%Y-%m-%d %H:%i:%s') AS u FROM {table}"),
                                         {r["uid"]: r["u"] for r in c.fetchall()})[1])
            if known is None:
                return
            for uid in ids(self.data):
                try:
                    p = load(self.data, uid)
                except ValueError:
                    continue
                if known.get(uid) != p.get("updated"):
                    self.write(key + uid)

    def write_check(self, uid: str):
        try:
            c = ck.load(self.data, uid)
        except (ValueError, OSError):
            traceback.print_exc()
            return False
        r = ck.row(c)
        folder = str(ck.folder(self.data, uid).resolve())
        wrong = ", ".join(f"{s} {ref}" for s in pj.SIDES for ref in r[s]["wrong"])
        log = "\n".join(f"{e['at']}  {e['what']}" for e in c.get("log", []))

        def go(cur):
            cur.execute("""INSERT INTO aoi_checks (board, reference_uid, serial, notes, station, result, front_checked, front_flagged,
                             back_checked, back_flagged, wrong_parts, log, folder, created_at, updated_at, uid)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE board=VALUES(board), reference_uid=VALUES(reference_uid), serial=VALUES(serial),
                             notes=VALUES(notes), station=VALUES(station), result=VALUES(result),
                             front_checked=VALUES(front_checked), front_flagged=VALUES(front_flagged),
                             back_checked=VALUES(back_checked), back_flagged=VALUES(back_flagged), wrong_parts=VALUES(wrong_parts),
                             log=VALUES(log), folder=VALUES(folder), created_at=VALUES(created_at), updated_at=VALUES(updated_at)""",
                        (c["board"], c["reference"], c.get("serial", ""), c.get("notes", ""), c.get("station", ""), c["result"],
                         r["front"]["checked"], r["front"]["flagged"], r["back"]["checked"], r["back"]["flagged"], wrong, log,
                         folder, c["created"], c["updated"], uid))
            cur.execute("SELECT id FROM aoi_checks WHERE uid=%s", (uid,))
            cid = cur.fetchone()["id"]
            cur.execute("DELETE FROM aoi_check_parts WHERE check_id=%s", (cid,))
            rows = [(cid, side, p["ref"][:64], p["kind"] or "", (p.get("value") or "")[:255], p["status"], p.get("answer"),
                     (p.get("why") or "")[:255], p.get("score"), p.get("turned"), p.get("mark"))
                    for side, st in c["sides"].items() for p in st.get("results", [])]
            if rows:
                cur.executemany("""INSERT INTO aoi_check_parts (check_id, side, ref, kind, value, status, answer, why, score, turned, mark)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", rows)
            return True
        return self._run(go)

    def search_checks(self, q: str = "", limit: int = 50) -> dict:
        limit = max(1, min(500, int(limit)))
        if self.ok:
            self.flush()
        cols = ("uid, board, reference_uid, serial, notes, station, result, front_checked, front_flagged, back_checked, "
                "back_flagged, wrong_parts, DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created, "
                "DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS updated")

        def go(c):
            if q.strip():
                pat = like(q)
                c.execute(f"""SELECT {cols} FROM aoi_checks WHERE board LIKE %s OR serial LIKE %s OR notes LIKE %s
                              OR result LIKE %s OR uid LIKE %s OR wrong_parts LIKE %s ORDER BY updated_at DESC LIMIT %s""",
                          (pat,) * 6 + (limit,))
            else:
                c.execute(f"SELECT {cols} FROM aoi_checks ORDER BY updated_at DESC LIMIT %s", (limit,))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) AS n FROM aoi_checks")
            return rows, c.fetchone()["n"]
        got = self._run(go)
        if got is not None:
            rows, total = got
            out = []
            for r in rows:
                wrong = [w for w in (r["wrong_parts"] or "").split(", ") if w]
                d = {k: r[k] for k in ("uid", "board", "serial", "station", "created", "updated", "result")}
                d.update(reference=r["reference_uid"], notes=r["notes"] or "")
                for side in pj.SIDES:
                    d[side] = {"aligned": r[f"{side}_checked"] > 0, "checked": r[f"{side}_checked"], "flagged": r[f"{side}_flagged"],
                               "wrong": [w.split(" ", 1)[1] for w in wrong if w.startswith(side + " ")]}
                out.append(d)
            return {"source": "database", "status": self.status, "ok": True, "checks": out, "total": total}
        import fnmatch
        pat = q.strip().lower()
        if pat and "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        rows = []
        for uid in ck.uids(self.data):
            try:
                c = ck.load(self.data, uid)
            except ValueError:
                continue
            hay = [c.get(k) or "" for k in ("board", "serial", "notes", "result", "uid")]
            if pat and not any(fnmatch.fnmatch(h.lower(), pat) for h in hay):
                continue
            rows.append(ck.row(c))
        rows.sort(key=lambda r: r["updated"], reverse=True)
        return {"source": "folders", "status": self.status, "ok": False, "checks": rows[:limit], "total": len(ck.uids(self.data))}

    def write(self, uid: str):
        if uid.startswith("check:"):
            return self.write_check(uid[6:])
        try:
            p = pj.load(self.data, uid)
            s = pj.summary(self.data, uid)
            fl = pj.files(self.data, uid)
        except (ValueError, OSError):
            traceback.print_exc()
            return False
        folder = str(pj.folder(self.data, uid).resolve())

        def go(c):
            v = (p["board"], p.get("serial", ""), p.get("notes", ""), p.get("station", ""), p.get("odb_file", ""),
                 p.get("bom_file", ""), s["units"], s["parts"],
                 s["front"]["parts"], s["front"]["photo"], s["front"]["aligned"], s["front"]["checkable"],
                 s["back"]["parts"], s["back"]["photo"], s["back"]["aligned"], s["back"]["checkable"],
                 s["closeups"], folder, p["created"], p["updated"])
            c.execute("""INSERT INTO aoi_projects (board, serial, notes, station, odb_file, bom_file, units, parts,
                           front_parts, front_photo, front_aligned, front_checkable, back_parts, back_photo, back_aligned,
                           back_checkable, closeups, folder, created_at, updated_at, uid)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                         ON DUPLICATE KEY UPDATE board=VALUES(board), serial=VALUES(serial), notes=VALUES(notes),
                           station=VALUES(station), odb_file=VALUES(odb_file), bom_file=VALUES(bom_file), units=VALUES(units),
                           parts=VALUES(parts), front_parts=VALUES(front_parts), front_photo=VALUES(front_photo),
                           front_aligned=VALUES(front_aligned), front_checkable=VALUES(front_checkable),
                           back_parts=VALUES(back_parts), back_photo=VALUES(back_photo), back_aligned=VALUES(back_aligned),
                           back_checkable=VALUES(back_checkable), closeups=VALUES(closeups), folder=VALUES(folder),
                           created_at=VALUES(created_at), updated_at=VALUES(updated_at)""", v + (uid,))
            c.execute("SELECT id FROM aoi_projects WHERE uid=%s", (uid,))
            pid = c.fetchone()["id"]
            for table in ("aoi_project_files", "aoi_project_log", "aoi_project_parts"):
                c.execute(f"DELETE FROM {table} WHERE project_id=%s", (pid,))
            if fl:
                c.executemany("INSERT INTO aoi_project_files (project_id, path, kind, side, bytes, modified) VALUES (%s, %s, %s, %s, %s, %s)",
                              [(pid, f["path"], f["kind"], f["side"], f["bytes"], f["modified"]) for f in fl])
            if p.get("log"):
                c.executemany("INSERT INTO aoi_project_log (project_id, at, what) VALUES (%s, %s, %s)",
                              [(pid, e["at"], e["what"][:500]) for e in p["log"]])
            if s["rows"]:
                c.executemany("""INSERT INTO aoi_project_parts (project_id, side, ref, kind, value, checkable, via_closeup, reason, px)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                              [(pid, r["side"], r["ref"][:64], r["kind"] or "", (r["value"] or "")[:255], r["checkable"],
                                r["via"], r["reason"][:255], r["px"]) for r in s["rows"]])
            return True
        return self._run(go)

    # ---- reading --------------------------------------------------------------------------------
    def search(self, q: str = "", limit: int = 50) -> dict:
        """The newest projects, or those whose board, serial, notes or file names match q."""
        limit = max(1, min(500, int(limit)))
        if self.ok:
            self.flush()

        def go(c):
            if q.strip():
                pat = like(q)
                c.execute(f"""SELECT {LIST_COLS} FROM aoi_projects
                              WHERE board LIKE %s OR serial LIKE %s OR notes LIKE %s OR odb_file LIKE %s OR bom_file LIKE %s OR uid LIKE %s
                              ORDER BY updated_at DESC LIMIT %s""", (pat,) * 6 + (limit,))
            else:
                c.execute(f"SELECT {LIST_COLS} FROM aoi_projects ORDER BY updated_at DESC LIMIT %s", (limit,))
            rows = [_row(r) for r in c.fetchall()]
            c.execute("SELECT COUNT(*) AS n FROM aoi_projects")
            return rows, c.fetchone()["n"]
        got = self._run(go)
        if got is not None:
            rows, total = got
            return {"source": "database", "status": self.status, "ok": True, "projects": rows, "total": total}
        return {"source": "folders", "status": self.status, "ok": False, **self._search_folders(q, limit)}

    def _search_folders(self, q: str, limit: int) -> dict:
        import fnmatch
        rows = []
        pat = q.strip().lower()
        if pat and "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        for uid in pj.uids(self.data):
            try:
                p = pj.load(self.data, uid)
            except ValueError:
                continue
            hay = [p.get(k) or "" for k in ("board", "serial", "notes", "odb_file", "bom_file", "uid")]
            if pat and not any(fnmatch.fnmatch(h.lower(), pat) for h in hay):
                continue
            rows.append(p)
        total = len(pj.uids(self.data))
        rows.sort(key=lambda p: p.get("updated", ""), reverse=True)
        return {"projects": [pj.row(self.data, p) for p in rows[:limit]], "total": total}

    def detail(self, uid: str) -> dict:
        """Everything about one project for the history page. The folder is read directly (it's
        on this station), so this is right even before the database has caught up."""
        p = pj.load(self.data, uid)
        s = pj.summary(self.data, uid)
        out = pj.row(self.data, p, s)
        out["log"] = list(reversed(p.get("log", [])))
        out["files"] = pj.files(self.data, uid)
        out["unchecked"] = [r for r in s["rows"] if not r["checkable"]]
        out["folder"] = str(pj.folder(self.data, uid).resolve())
        out["in_db"] = self._run(lambda c: (c.execute("SELECT 1 FROM aoi_projects WHERE uid=%s", (uid,)), bool(c.fetchone()))[1])
        return out

    def info(self) -> dict:
        return {"ok": self.ok, "status": self.status}
