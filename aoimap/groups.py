"""Projects: the level above the tabs. A project is a product (PTC, say) and the board design(s) it
is made from. Choose one and the tabs work inside it: its boards mapped, its references, checks,
history, Parts-tab choices and learned examples.

Everything else is already kept by board design name (projects/*/project.json "board",
boards/<design>/, learned/<design>/, checks/*/check.json "board"), so a project only names its
designs and nothing is moved:

    <data>/groups.json
        {"projects": [{"id", "name", "boards": [design, ...], "notes", "created", "updated"}],
         "current": id,                    the project chosen last
         "open": {id: board uid}}          the board each project had open last

(In the code "project" already meant one board mapped (projects.py), so these are "groups" here;
the page calls them projects.) The database keeps a copy (db.py: aoi_groups, aoi_group_boards).
"""

from pathlib import Path

from . import projects as pj

FILE = "groups.json"


def path(data: Path) -> Path:
    return Path(data) / FILE


def load(data: Path) -> dict:
    d = pj._read_json(path(data), None) or {}
    d.setdefault("projects", [])
    d.setdefault("current", None)
    d.setdefault("open", {})
    return d


def save(data: Path, d: dict):
    pj._write_json(path(data), d)


def get(d: dict, gid: str | None) -> dict | None:
    return next((g for g in d["projects"] if g["id"] == gid), None)


def of_board(d: dict, board: str | None) -> dict | None:
    """The project a board design belongs to."""
    return next((g for g in d["projects"] if board and board in g["boards"]), None)


def designs_found(data: Path) -> set[str]:
    """Every board design something is kept for: boards mapped, checks, choices, learned examples."""
    data = Path(data)
    names = set()
    for uid in pj.uids(data):
        b = (pj._read_json(pj.folder(data, uid) / "project.json", {}) or {}).get("board")
        if b:
            names.add(b)
    for f in (data / "checks").glob("*/check.json"):
        b = (pj._read_json(f, {}) or {}).get("board")
        if b:
            names.add(b)
    for f in (data / "boards").glob("*/profile.json"):
        names.add(f.parent.name)
    for f in (data / "learned").glob("*/*/examples.json"):
        names.add((pj._read_json(f, {}) or {}).get("board") or f.parent.parent.name)
    return names


def _new_id(d: dict, name: str) -> str:
    base = pj.slug(name)[:60]
    gid, n = base, 2
    while get(d, gid):
        gid, n = f"{base}-{n}", n + 1
    return gid


def _add(d: dict, name: str, boards=()) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("Give the project a name.")
    if any(g["name"].lower() == name.lower() for g in d["projects"]):
        raise ValueError(f"There is already a project called {name}.")
    stamp = pj.now()
    g = {"id": _new_id(d, name), "name": name, "boards": list(boards), "notes": "", "created": stamp, "updated": stamp}
    d["projects"].append(g)
    return g


def ensure(data: Path) -> tuple[dict, bool]:
    """The project list, made the first time and kept complete: a board design that isn't in any
    project (boards mapped before projects existed, say) gets a project of its own, named after it,
    so nothing kept is ever out of sight. A project is always chosen when there is one."""
    d = load(data)
    changed = not path(data).is_file()
    for name in sorted(designs_found(data)):
        if not of_board(d, name):
            _add(d, name, [name])
            changed = True
    if d["projects"] and not get(d, d["current"]):
        d["current"] = d["projects"][0]["id"]
        changed = True
    if changed:
        save(data, d)
    return d, changed


def create(data: Path, name: str, boards=()) -> dict:
    d = load(data)
    for b in boards:
        owner = of_board(d, b)
        if owner:
            raise ValueError(f"{b} is already in project {owner['name']}.")
    g = _add(d, name, boards)
    d["current"] = g["id"]
    save(data, d)
    return g


def rename(data: Path, gid: str, name: str) -> dict:
    d = load(data)
    g = get(d, gid)
    if not g:
        raise ValueError("No such project.")
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("Give the project a name.")
    if any(o is not g and o["name"].lower() == name.lower() for o in d["projects"]):
        raise ValueError(f"There is already a project called {name}.")
    g["name"], g["updated"] = name, pj.now()
    save(data, d)
    return g


def set_current(data: Path, gid: str) -> dict:
    d = load(data)
    if not get(d, gid):
        raise ValueError("No such project.")
    d["current"] = gid
    save(data, d)
    return d


def add_board(data: Path, gid: str, board: str) -> dict:
    d = load(data)
    g = get(d, gid)
    if not g:
        raise ValueError("No such project.")
    owner = of_board(d, board)
    if owner and owner is not g:
        raise ValueError(f"{board} is already in project {owner['name']}.")
    if board not in g["boards"]:
        g["boards"].append(board)
        g["updated"] = pj.now()
        save(data, d)
    return g


def set_open(data: Path, gid: str, uid: str | None):
    d = load(data)
    if d["open"].get(gid) != uid:
        d["open"][gid] = uid
        save(data, d)


def mapped(data: Path, boards) -> list[dict]:
    """The boards mapped for these designs, newest change first."""
    out = []
    for uid in pj.uids(data):
        p = pj._read_json(pj.folder(data, uid) / "project.json", {}) or {}
        if p.get("board") in boards:
            out.append(p)
    return sorted(out, key=lambda p: p.get("updated", ""), reverse=True)


def counts(data: Path, g: dict) -> dict:
    """What a project holds, for the project bar."""
    from . import check as ck
    boards = set(g["boards"])
    ms = mapped(data, boards)
    checks = 0
    for f in (Path(data) / "checks").glob("*/check.json"):
        if (pj._read_json(f, {}) or {}).get("board") in boards:
            checks += 1
    learned = sum(len(ck.load_examples(data, b, s)["examples"]) for b in boards for s in pj.SIDES)
    return {"mapped": len(ms), "references": sum(1 for p in ms if p.get("reference")), "checks": checks, "learned": learned}


def view(data: Path, d: dict | None = None) -> dict:
    d = d or load(data)
    return {"current": d["current"],
            "projects": [{**{k: g[k] for k in ("id", "name", "boards", "notes", "created", "updated")}, **counts(data, g)}
                         for g in d["projects"]]}
