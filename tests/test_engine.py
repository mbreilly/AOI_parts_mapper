"""Engine tests on the made-up board in fixture.py. Run with  python -m pytest tests  or  python tests/test_engine.py"""

import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture as fx
from aoimap import bom as bomlib, odb as odblib, photo as ph
from aoimap.board import Board

TMP = Path(tempfile.mkdtemp(prefix="aoimap-test-"))
ODB, BOM = fx.make_odb(TMP), fx.make_bom(TMP)


def test_odb_reads_parts_units_and_steps():
    d, steps = odblib.load(ODB)
    assert steps[0]["name"] == "pcb" and steps[0]["has_components"]
    assert any(s["name"] == "panel" and not s["has_components"] for s in steps)
    assert d.units == "inch" and d.units_known
    assert len(d.parts) == len(fx.PARTS)
    assert set(d.fiducials) == {"FIDT1", "FIDT2", "FIDT3", "FIDT4", "FIDB1", "FIDB2", "FIDB3"}
    u2 = next(p for p in d.parts if p.ref == "U2")
    assert u2.rot == 90 and u2.package == "SOIC8_OFF" and "1" in u2.pins
    assert d.outline["xmax"] == 6.0 and d.outline["ymax"] == 4.0


def test_bom_columns_ranges_dnp_and_kinds():
    b = bomlib.read_bom(BOM)
    h = b["header"]
    assert h[b["columns"]["refdes"]] == "Designator" and h[b["columns"]["value"]] == "Comment"
    assert "R9" in b["dnp"] and "R9" not in b["parts"]
    kinds = {ln["value"]: ln["kind"] for ln in b["lines"]}
    assert kinds["T491B106K"] == "tantalum" and kinds["LTST-C191"] == "led" and kinds["SS14"] == "diode"
    assert kinds["STM32G0"] == "ic" and kinds["TS3A5017"] == "ic" and kinds["OPA2333"] == "ic"
    assert kinds["SMAJ12CA"] is None and kinds["10k"] is None
    assert bomlib.expand_refs("C1-C3, R5 LED1-2") == ["C1", "C2", "C3", "R5", "LED1", "LED2"]


def new_board():
    b = Board(Path(tempfile.mkdtemp(prefix="aoimap-data-")))
    b.add_file("odb", ODB.name, ODB.read_bytes())
    b.add_file("bom", BOM.name, BOM.read_bytes())
    return b


def test_design_counts_and_rotation_direction():
    b = new_board()
    c = b.design["counts"]
    assert c["top"] == {"ic": 3, "tantalum": 8, "led": 3, "diode": 1}, c
    assert c["bot"] == {"ic": 1, "led": 4}, c
    # the off-centre SOIC boxes say the file's rotation is clockwise
    assert b.design["rot_sign"] == -1, b.design["rotation_evidence"]
    assert b.design["bom_only"] == [] and b.design["odb_only"] == []


def click(Hm, design, refs, side, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    pts = {}
    for r in refs:
        f = design["fiducials"].get(r)
        xy = [f["x"], f["y"]] if f else next([p["x"], p["y"]] for p in design["parts"] if p["ref"] == r)
        pts[r] = (fx.to_px(Hm, [xy])[0] + rng.normal(0, noise, 2)).tolist()
    return pts


def placement_error(b, side, Hm):
    st = b.sides[side]
    errs = []
    for r in st["placed"]:
        p = next(q for q in b.design["parts"] if q["ref"] == r["ref"])
        true = fx.to_px(Hm, [[p["x"], p["y"]]])[0]
        errs.append(np.linalg.norm(np.float32(r["centre"]) - true))
    return max(errs)


def test_front_four_fiducials_and_snapping():
    b = new_board()
    Hm = fx.camera("front")
    img = fx.render("front", Hm)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    b.set_photo("front", buf.tobytes())
    prompts = b.anchors("front")["prompts"]
    assert [p["ref"] for p in prompts] == ["FIDT1", "FIDT2", "FIDT4", "FIDT3"], prompts   # TL, TR, BR, BL
    assert prompts[0]["where"] == "top-left"
    # clicks a few pixels off snap to the pads
    rough = click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front", noise=3, seed=2)
    snapped = {r: b.snap("front", xy)[0] for r, xy in rough.items()}
    exact = click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front")
    assert max(np.linalg.norm(np.float32(snapped[r]) - exact[r]) for r in exact) < 1.5
    s = b.align("front", snapped)
    assert s["aligned"] and not s["warnings"], s["warnings"]
    assert placement_error(b, "front", Hm) < 2.0
    placed = {r["ref"]: r for r in s["placed"]}
    assert placed["C1"]["checkable"] and placed["U1"]["checkable"]
    assert not placed["D1"]["checkable"] and placed["D1"]["reason"].startswith("too small")
    # automatic find gives the same points
    auto = b.auto_find("front")
    assert auto and max(np.linalg.norm(np.float32(auto[r]) - exact[r]) for r in exact) < 1.5
    # the orientation check's parts.json shape
    import json
    pj = json.loads((b.dir / "front" / "parts.json").read_text())
    assert all(set(p) == {"ref", "kind", "box"} for p in pj) and any(p["ref"] == "C1" for p in pj)
    assert (b.dir / "parts_map.csv").read_text().count("\n") == len(b.design["parts"]) + 1


def test_back_three_fiducials_affine_and_closeups():
    b = new_board()
    Hm = fx.camera("back", ppi=250)
    img = fx.render("back", Hm)
    b.set_photo("back", cv2.imencode(".png", img)[1].tobytes())
    s = b.align("back", click(Hm, b.design, ["FIDB1", "FIDB2", "FIDB3"], "back"))
    assert b.sides["back"]["mapping"].kind == "affine"
    assert placement_error(b, "back", Hm) < 3.0       # affine can't follow the small perspective
    small = [r["ref"] for r in s["placed"] if r["reason"].startswith("too small")]
    assert sorted(small) == ["DB1", "DB2", "DB3", "DB4"], small
    tiles = s["tiles"]
    assert {r for t in tiles for r in t["refs"]} == set(small)
    assert len(tiles) == 2, tiles   # DB1-3 together, DB4 on its own
    t = next(t for t in tiles if "DB1" in t["refs"])
    assert len(t["anchors"]) >= 2
    # take the closeup framed on the region and click its anchors
    Hc = fx.camera("back", region=t["region"])
    cimg = fx.render("back", Hc)
    b.closeup_photo("back", t["index"], cv2.imencode(".png", cimg)[1].tobytes())
    s = b.closeup_align("back", t["index"], click(Hc, b.design, [a["ref"] for a in t["anchors"]], "back"))
    placed = {r["ref"]: r for r in s["placed"]}
    assert all(placed[r]["checkable"] and placed[r]["via"] == t["index"] for r in t["refs"]), \
        [(r, placed[r]["reason"]) for r in t["refs"]]
    assert not placed["DB4"]["checkable"]
    # the state survives reopening the board
    b2 = Board(b.data)
    b2.open_project(b.project["uid"])
    p2 = {r["ref"]: r for r in b2.sides["back"]["placed"]}
    assert p2["DB1"]["checkable"] and not p2["DB4"]["checkable"]


def test_two_points_wrong_order_and_rotation_choice():
    b = new_board()
    Hm = fx.camera("front", tilt_deg=0)
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm))[1].tobytes())
    s = b.align("front", click(Hm, b.design, ["FIDT1", "FIDT4"], "front"))
    assert b.sides["front"]["mapping"].kind == "similarity"
    assert placement_error(b, "front", Hm) < 3.0
    # swapped clicks are caught
    pts = click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front")
    pts["FIDT1"], pts["FIDT2"] = pts["FIDT2"], pts["FIDT1"]
    s = b.align("front", pts)
    assert s["warnings"], "swapped fiducials should warn"
    # rotation: the design already decided (-1); the choice shows U2 or U3 both ways
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    rc = b.rotation_choice("front")
    assert rc["needed"] and rc["ref"] in ("U2", "U3") and len(rc["crops"]) == 2
    # forcing the wrong direction moves the off-centre ICs
    b.set_option("rot_sign", 1)
    wrong = {r["ref"]: r for r in b.sides["front"]["placed"]}
    b.set_option("rot_sign", "auto")
    right = {r["ref"]: r for r in b.sides["front"]["placed"]}
    assert np.linalg.norm(np.float32(wrong["U2"]["poly"]).mean(0) - np.float32(right["U2"]["poly"]).mean(0)) > 20


def test_profile_kept_for_next_board_and_markings():
    b = new_board()
    key = next(ln["key"] for ln in b.bom["lines"] if ln["value"] == "SMAJ12CA")
    b.set_lines({key: {"kind": "diode"}})
    hk = next(ln["key"] for ln in b.bom["lines"] if ln["value"] == "STM32G0")
    b.set_lines({hk: {"hidden": "under a shield"}})
    b.set_option("min_px", 10)
    b.set_option("marking", {"kind": "led", "end": "other"})
    b2 = Board(b.data)
    b2.add_file("odb", ODB.name, ODB.read_bytes())
    b2.add_file("bom", BOM.name, BOM.read_bytes())
    assert any("saved choices" in m for m in b2.messages)
    kinds = {p["ref"]: p for p in b2.design["parts"]}
    assert kinds["D7"]["kind"] == "diode" and kinds["U1"]["hidden"] == "under a shield"
    assert b2.profile["min_px"] == 10 and b2.profile["markings"]["led"] == "other"
    # marking: LED band at the other pin now
    from aoimap.design import marking
    p = kinds["D1"]
    assert marking(p, b2.profile["markings"])["at"] == p["other_pin"]
    assert marking(kinds["C1"], b2.profile["markings"])["at"] == kinds["C1"]["pin1"]
    assert marking(kinds["UB1"], b2.profile["markings"])["type"] == "dot"


def test_no_fiducials_uses_board_corners():
    b = new_board()
    b.design["fiducials"] = {}
    a = b.anchors("front")
    assert [p["what"] for p in a["prompts"]] == ["corner"] * 4


def test_projects_history_and_new_board_of_same_design():
    from aoimap import projects as pj
    b = new_board()
    first = b.project["uid"]
    assert b.dir == b.data / "projects" / first and (b.dir / "source").is_dir()
    assert b.project["odb_file"] == ODB.name and b.project["bom_file"] == BOM.name
    # a BOM loaded again before any photo stays in the same project
    b.add_file("bom", BOM.name, BOM.read_bytes())
    assert b.project["uid"] == first and pj.uids(b.data) == [first]
    Hm = fx.camera("front")
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm))[1].tobytes())
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    b.set_info(first, serial="SN-001", notes="first one")
    log = [e["what"] for e in pj.load(b.data, first)["log"]]
    assert log[0].startswith("Loaded ODB++") and any(w.startswith("Front lined up") for w in log) and "Serial set to SN-001" in log[-1]
    s = pj.summary(b.data, first)
    assert s["front"]["aligned"] and s["front"]["checkable"] > 0 and not s["back"]["photo"]
    kinds = {f["path"]: f["kind"] for f in pj.files(b.data, first)}
    assert kinds["front/reference.jpg"] == "photo" and kinds["front/map.jpg"] == "map" and kinds["parts_map.csv"] == "csv"
    assert any(k == "odb" for k in kinds.values()) and any(k == "bom" for k in kinds.values())
    assert pj.thumb(b.data, first, "front/map.jpg", 200)[:2] == b"\xff\xd8"
    for bad in ("../project.json", "/etc/passwd", "thumbs/200/front/map.jpg"):
        try:
            pj.file_path(b.data, first, bad); raise AssertionError(bad)
        except ValueError:
            pass
    # the next board of the same design is a new project; the first is left as it was
    b.set_option("min_px", 11)
    b.new_from(first)
    second = b.project["uid"]
    assert second != first and not b._has_photos() and b.profile["min_px"] == 11
    assert b.project["odb_file"] == ODB.name and b.design and b.project["serial"] == ""
    assert (b.data / "projects" / first / "front" / "reference.jpg").is_file()
    assert not (b.dir / "front" / "reference.jpg").exists()
    # reopening the first one brings back its photo and alignment, without changing it
    before = pj.load(b.data, first)["updated"]
    b.open_project(first)
    assert b.sides["front"]["mapping"] is not None and b.project["serial"] == "SN-001"
    assert pj.load(b.data, first)["updated"] == before
    # loading the files again after photos starts another project
    b.add_file("odb", ODB.name, ODB.read_bytes())
    assert b.project["uid"] not in (first, second) and len(pj.uids(b.data)) == 3


def test_old_board_folders_become_projects():
    import shutil
    from aoimap import projects as pj
    b = new_board()
    old = b.data / "boards" / "oldboard"
    shutil.copytree(b.dir / "source", old / "source")
    shutil.copy(b.dir / "design.json", old / "design.json")
    b2 = Board(b.data)
    got = [pj.load(b.data, u) for u in pj.uids(b.data)]
    imported = [p for p in got if p["board"] == "oldboard"]
    assert len(imported) == 1 and (old / "imported.txt").is_file()
    Board(b.data)   # only once
    assert len([u for u in pj.uids(b.data) if "oldboard" in u]) == 1
    b2.open_project(imported[0]["uid"])
    assert b2.design["parts"]


def reference_board():
    b = new_board()
    Hm = fx.camera("front")
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm))[1].tobytes())
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    b.set_reference(b.project["uid"])
    return b


def test_check_another_board_finds_reversed_parts():
    from aoimap.check import Checks, load, references
    b = reference_board()
    refs = references(b.data)
    assert [r["uid"] for r in refs] == [b.project["uid"]] and "front" in refs[0]["sides"]
    c = Checks(b.data)
    c.start(b.project["uid"], "SN-9")
    # another board, photographed a little differently, with C3 and C7 the wrong way round
    H2 = fx.camera("front", tilt_deg=-3, ppi=280)
    c.photo("front", cv2.imencode(".png", fx.render("front", H2, reversed_refs=("C3", "C7")))[1].tobytes())
    st = c.cur["sides"]["front"]
    assert st["aligned"], st["message"]
    res = {r["ref"]: r for r in st["results"]}
    assert res["C3"]["status"] == "reversed" and res["C7"]["status"] == "reversed", res
    # D8 is drawn with no mark, so it can't be told either way and is left to the eye
    assert res["D8"]["status"] == "check" and "no mark" in res["D8"]["why"]
    for ok in ("C1", "C5", "C8", "U1", "U3"):
        assert res[ok]["status"] == "ok", (ok, res[ok])
    assert {s["ref"] for s in st["skipped"]} >= {"D1", "D2", "D3"}
    assert c.cur["result"] == "OPEN"
    flagged = [r["ref"] for r in st["results"] if r["status"] != "ok"]
    for ref in flagged:
        c.answer("front", ref, "wrong" if ref in ("C3", "C7") else "correct")
    saved = load(b.data, c.cur["uid"])
    assert saved["result"] == "FAIL" and saved["serial"] == "SN-9"
    assert c.crop(c.cur["uid"], "front", "C3", "board")[:2] == b"\xff\xd8"
    # the same board with everything right passes once the flagged parts are looked at
    c.start(b.project["uid"], "SN-10")
    c.photo("front", cv2.imencode(".png", fx.render("front", H2))[1].tobytes())
    st = c.cur["sides"]["front"]
    assert not [r for r in st["results"] if r["status"] in ("reversed", "rotated")], st["results"]
    for r in st["results"]:
        if r["status"] != "ok":
            c.answer("front", r["ref"], "correct")
    assert c.cur["result"] == "PASS"


def test_check_lined_up_from_clicked_fiducials():
    import numpy as np
    from aoimap.check import Checks
    b = reference_board()
    c = Checks(b.data)
    c.start(b.project["uid"])
    H2 = fx.camera("front", tilt_deg=4, ppi=260)
    img = fx.render("front", H2, reversed_refs=("C3",))
    blank = img.copy()
    for r in ("FIDT1", "FIDT2", "FIDT3", "FIDT4"):   # paint the fiducials over, so neither automatic way works
        f = b.design["fiducials"][r]
        x, y = fx.to_px(H2, [[f["x"], f["y"]]])[0]
        cv2.circle(blank, (int(x), int(y)), 9, (40, 110, 30), -1)
    c.photo("front", cv2.imencode(".png", blank)[1].tobytes())
    st = c.cur["sides"]["front"]
    a = c.anchors("front")
    assert not st["aligned"] and st["need_points"] and len(a["prompts"]) == 4, st["message"]
    pts = {}
    for p in a["prompts"]:
        f = b.design["fiducials"][p["ref"]]
        pts[p["ref"]] = [float(v) for v in fx.to_px(H2, [[f["x"], f["y"]]])[0]]
    c.align_points("front", pts)
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["C3"]["status"] == "reversed" and res["C1"]["status"] == "ok", res


def test_database_history():
    """Needs a MariaDB login: AOIMAP_TEST_DB=path/to/option-file (a throwaway database)."""
    import os
    import time
    from aoimap import db as dblib
    cfg = os.environ.get("AOIMAP_TEST_DB")
    if not cfg:
        print("skip test_database_history (set AOIMAP_TEST_DB)")
        return
    b = new_board()
    h = dblib.History(b.data, Path(cfg), delay=0.2)
    b.on_change = h.changed
    first = b.project["uid"]
    Hm = fx.camera("front")
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm))[1].tobytes())
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    b.set_info(first, serial="DBTEST-7")
    for _ in range(100):
        r = h.search("DBTEST-7")
        if r["source"] == "database" and r["projects"] and r["projects"][0]["front"]["aligned"]:
            break
        time.sleep(0.1)
    assert r["source"] == "database" and [p["uid"] for p in r["projects"]] == [first], r
    row = r["projects"][0]
    assert row["serial"] == "DBTEST-7" and row["front"]["checkable"] > 0 and row["front"]["photo"]
    n = h._run(lambda c: (c.execute("SELECT COUNT(*) AS n FROM aoi_project_parts p JOIN aoi_projects j ON j.id=p.project_id WHERE j.uid=%s", (first,)), c.fetchone()["n"])[1])
    assert n == row["front"]["parts"]
    files = h._run(lambda c: (c.execute("SELECT kind FROM aoi_project_files f JOIN aoi_projects j ON j.id=f.project_id WHERE j.uid=%s", (first,)), [x["kind"] for x in c.fetchall()])[1])
    assert {"odb", "bom", "photo", "map", "csv"} <= set(files)
    assert h.search("DBTEST*")["projects"] and not h.search("nothing-like-this")["projects"]
    d = h.detail(first)
    assert d["in_db"] and d["log"][0]["what"] == "Serial set to DBTEST-7"
    # a folder the database missed is written when it catches up
    h2 = dblib.History(b.data, Path(cfg), delay=0.2)
    h._run(lambda c: c.execute("DELETE FROM aoi_projects WHERE uid=%s", (first,)))
    h2.catch_up()
    assert h2.search("DBTEST-7")["projects"]
    h._run(lambda c: c.execute("DELETE FROM aoi_projects WHERE uid=%s", (first,)))
    # without a login the history comes from the folders
    h3 = dblib.History(b.data, b.data / "no-such.cnf")
    r3 = h3.search("DBTEST")
    assert r3["source"] == "folders" and r3["projects"][0]["serial"] == "DBTEST-7"
    # checks of other boards
    from aoimap.check import Checks
    b.set_reference(first)
    ch = Checks(b.data)
    ch.on_change = h.changed_check
    ch.start(first, "DBCHK-1")
    ch.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-2), reversed_refs=("C3",)))[1].tobytes())
    ch.answer("front", "C3", "wrong")
    r = h.search_checks("DBCHK-1")
    assert r["source"] == "database" and r["checks"][0]["result"] == "FAIL" and r["checks"][0]["front"]["wrong"] == ["C3"], r
    n = h._run(lambda c: (c.execute("SELECT COUNT(*) AS n FROM aoi_check_parts p JOIN aoi_checks k ON k.id=p.check_id WHERE k.uid=%s AND p.answer='wrong'", (ch.cur["uid"],)), c.fetchone()["n"])[1])
    assert n == 1
    assert h3.search_checks("DBCHK")["checks"][0]["result"] == "FAIL"
    h._run(lambda c: c.execute("DELETE FROM aoi_checks WHERE uid=%s", (ch.cur["uid"],)))
    h._run(lambda c: c.execute("DELETE FROM aoi_projects WHERE uid=%s", (first,)))


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("ok  ", name)
            except Exception as e:
                fails += 1
                import traceback; traceback.print_exc()
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
