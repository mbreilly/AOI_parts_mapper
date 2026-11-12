"""Engine tests on the made-up board in fixture.py. Run with  python -m pytest tests  or  python tests/test_engine.py"""

import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture as fx
from aoimap import bom as bomlib, design as designlib, odb as odblib, photo as ph
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
    # rotation: every part's pins say which way it turns, so there's nothing to choose
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    assert not b.rotation_choice("front")["needed"]
    assert all(p["turn"] == -1 for p in b.design["parts"] if p["side"] == "top")
    # ...and forcing a direction by hand doesn't move a part whose pins already told
    right = {r["ref"]: r for r in b.sides["front"]["placed"]}
    b.set_option("rot_sign", 1)
    forced = {r["ref"]: r for r in b.sides["front"]["placed"]}
    b.set_option("rot_sign", "auto")
    assert np.linalg.norm(np.float32(forced["U2"]["poly"]).mean(0) - np.float32(right["U2"]["poly"]).mean(0)) < 0.5
    # without pin data in the ODB++ the hand-set direction still applies
    g = dict(next(p for p in b.design["parts"] if p["ref"] == "U2"), turn=0)
    assert designlib.outline_corners(g, 1) != designlib.outline_corners(g, -1)


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


def test_orientation_kept_per_board_not_for_the_design():
    b = new_board()
    first = b.project["uid"]
    design_orient = json.loads((b._profile_path(b.name)).read_text())["orient"]
    b.new_from(first)
    second = b.project["uid"]
    b.set_orient("front", [-1, 1])          # this board photographed turned round
    b.set_option("min_px", 12)              # any later save keeps it to the board too
    assert b.profile["orient"]["front"] == [-1, 1]
    assert json.loads((b._profile_path(b.name)).read_text())["orient"] == design_orient
    assert json.loads((b._profile_path(b.name)).read_text())["min_px"] == 12
    b.open_project(first)
    assert b.profile["orient"] == design_orient
    b.open_project(second)
    assert b.profile["orient"]["front"] == [-1, 1]
    b.new_from(second)
    assert b.profile["orient"] == design_orient


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


def test_check_learns_from_answers():
    from aoimap.check import Checks, load_examples, references
    b = reference_board()
    c = Checks(b.data)
    board, H2 = b.project["board"], fx.camera("front", tilt_deg=-3, ppi=280)
    shot = lambda *rev: cv2.imencode(".png", fx.render("front", H2, reversed_refs=rev))[1].tobytes()

    def run(serial, *rev):
        c.start(b.project["uid"], serial)
        c.photo("front", shot(*rev))
        assert c.cur["sides"]["front"]["aligned"]
        return {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}

    res = run("SN-1", "C3")
    assert res["D8"]["status"] == "check" and res["C3"]["status"] == "reversed"
    c.answer("front", "D8", "correct")               # a false alarm: D8 has no mark to go by
    c.answer("front", "C3", "wrong", "tantalum in backwards")
    ex = load_examples(b.data, board, "front")["examples"]
    assert {(e["ref"], e["label"]) for e in ex} == {("D8", "good"), ("C3", "bad")} and ex[0]["serial"] == "SN-1"
    # the next board: D8 isn't raised again, and C3 the wrong way round again is a known fault with the note
    res = run("SN-2", "C3")
    assert res["D8"]["status"] == "ok" and res["D8"]["learned"] == "good", res["D8"]
    assert res["C3"]["status"] == "fault" and "tantalum in backwards" in res["C3"]["why"], res["C3"]
    assert c.cur["result"] == "OPEN"
    # a board with C3 the right way round isn't taken for the fault
    res = run("SN-3")
    assert res["C3"]["status"] == "ok" and res["D8"]["status"] == "ok", (res["C3"], res["D8"])
    # changing an answer forgets the example it made
    c.answer("front", "C5", "wrong", "wrong value", missed=True)    # one the check passed
    d = load_examples(b.data, board, "front")
    assert d["reference_wrong"] == {b.project["uid"]: ["C5"]} and c.cur["result"] == "FAIL"
    assert sum(e["ref"] == "C5" for e in d["examples"]) == 1
    c.answer("front", "C5", None)
    d = load_examples(b.data, board, "front")
    assert d["reference_wrong"] == {} and not [e for e in d["examples"] if e["ref"] == "C5"]
    # a missed part: the reference's look of it is no longer trusted, so the same look is flagged next time
    c.answer("front", "C5", "wrong", "wrong value", missed=True)
    res = run("SN-4")
    assert res["C5"]["status"] != "ok", res["C5"]
    c.trust_reference(board, "front", b.project["uid"], "C5")
    # deleting the examples puts the check back as it was
    for e in c.learned(board, "front")["examples"]:
        c.forget(board, "front", e["id"])
    assert not load_examples(b.data, board, "front")["examples"]
    assert c.learned_boards()[0]["front"] == {"good": 0, "bad": 0, "covered": 0, "reference_wrong": 0}
    res = run("SN-5", "C3")
    assert res["D8"]["status"] == "check" and res["C3"]["status"] == "reversed" and res["C5"]["status"] == "ok"
    assert [x["board"] for x in c.learned_boards()] == [r["board"] for r in references(b.data)]



def test_reopened_check_is_compared_again_with_later_answers():
    from aoimap.check import Checks
    b = reference_board()
    c = Checks(b.data)
    H2 = fx.camera("front", tilt_deg=-3, ppi=280)
    shot = lambda *rev: cv2.imencode(".png", fx.render("front", H2, reversed_refs=rev))[1].tobytes()
    c.start(b.project["uid"], "SN-1")
    c.photo("front", shot("C3"))
    first = c.cur["uid"]
    c.answer("front", "D8", "correct")
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["C3"]["status"] == "reversed" and "learned" not in res["C3"]
    # another board answers C3 the wrong way round as a fault: SN-1, opened again, goes by that answer
    c.start(b.project["uid"], "SN-2")
    c.photo("front", shot("C3"))
    c.answer("front", "C3", "wrong", "tantalum in backwards")
    c.open(first)
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["C3"]["status"] == "fault" and "tantalum in backwards" in res["C3"]["why"], res["C3"]
    assert res["D8"]["answer"] == "correct"            # the answer given on SN-1 is kept
    assert c.cur["log"][-1]["what"].startswith("Front: 1 part compared again"), c.cur["log"][-1]   # only C3
    # nothing changed since: opening it again compares nothing
    n = len(c.cur["log"])
    c2 = Checks(b.data)
    c2.open(first)
    assert len(c2.cur["log"]) == n, c2.cur["log"][-1]

def test_learned_examples_kept_per_part_up_to_the_limit():
    import numpy as np
    from aoimap import check as ck
    data = Path(tempfile.mkdtemp(prefix="aoimap-learn-"))
    img = np.random.default_rng(0).integers(0, 255, (100, 100, 3), np.uint8)
    for i in range(ck.MAX_EXAMPLES + 3):
        ck.add_example(data, "B1", "front", img, [10, 10, 20, 20], "C1", "bad", f"n{i}")
    ck.add_example(data, "B1", "front", img, [10, 10, 20, 20], "C1", "good")
    ex = ck.load_examples(data, "B1", "front")["examples"]
    bad = [e for e in ex if e["label"] == "bad"]
    assert len(bad) == ck.MAX_EXAMPLES and bad[0]["note"] == "n3" and len(ex) == ck.MAX_EXAMPLES + 1
    assert len(list(ck.learned_dir(data, "B1", "front").glob("*.jpg"))) == len(ex)
    assert ck.example_path(data, "B1", "front", ex[-1]["id"]).is_file()


def test_add_any_part_to_the_check_and_references_follow():
    from aoimap import check as ck, projects as pj
    b = reference_board()
    ref_uid = b.project["uid"]
    # D7 is a TVS diode: in the BOM, but not polarised, so not in the check
    assert not any(p["ref"] == "D7" for p in b.design["parts"])
    b.set_extra("D7", "look")
    d7 = next(p for p in b.design["parts"] if p["ref"] == "D7")
    assert d7["kind"] == "look" and d7["extra"] and b.state()["extras"][0]["ref"] == "D7"
    placed = {r["ref"]: r for r in pj._read_json(b.dir / "front" / "parts_full.json")}
    assert placed["D7"]["checkable"] and "mark" not in placed["D7"]       # placed again without lining up again
    # LEDs too small for the photo are left for a closeup, with no light-test excuse
    assert placed["D1"]["reason"].startswith("too small") and "light" not in placed["D1"]["reason"]
    # another board of the design open here: adding a part updates the reference too
    b.new_from(ref_uid)
    assert "D7" in b.profile["extra"]
    b.set_extra("R1", "look")
    ref_parts = {r["ref"] for r in pj._read_json(pj.folder(b.data, ref_uid) / "front" / "parts_full.json")}
    assert {"D7", "R1"} <= ref_parts
    assert "R1" in pj._read_json(pj.folder(b.data, ref_uid) / "profile.json")["extra"]
    b.set_extra("R1", remove=True)
    assert "R1" not in {r["ref"] for r in pj._read_json(pj.folder(b.data, ref_uid) / "front" / "parts_full.json")}
    try:
        b.set_extra("NOPE1", "look"); raise AssertionError("unknown ref")
    except ValueError:
        pass
    # kept for the next board loaded from the files
    b2 = Board(b.data)
    b2.add_file("odb", ODB.name, ODB.read_bytes())
    b2.add_file("bom", BOM.name, BOM.read_bytes())
    assert list(b2.profile["extra"]) == ["D7"] and any(p["ref"] == "D7" for p in b2.design["parts"])
    # a check of another board covers it, by its look
    c = ck.Checks(b.data)
    c.start(ref_uid)
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["D7"]["status"] == "ok", res["D7"]


def test_teach_a_part_on_a_photo_already_taken():
    from aoimap import check as ck, teach as tl
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    c = ck.Checks(b.data)
    c.start(ref_uid, "SN-T1")
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    src = f"check/{c.cur['uid']}/front"
    assert [s["id"] for s in tl.sources(b.data, board, "front")] == [f"project/{ref_uid}/front", src]
    # any part can be looked at: one not in the check is skipped, with why
    info = tl.inspect(b.data, src, "R2")
    assert info["verdict"] == "skipped" and not info["in_check"] and "not in the check" in info["why"], info
    assert tl.crop(b.data, src, "R2", "board")[:2] == b"\xff\xd8" and tl.crop(b.data, src, "R2", "reference")[:2] == b"\xff\xd8"
    assert tl.inspect(b.data, src, "C3")["verdict"] == "correct"
    assert tl.inspect(b.data, f"project/{ref_uid}/front", "C3")["verdict"] == "reference"
    # clicking on a part picks it
    img = cv2.imread(str(ck.folder(b.data, c.cur["uid"]) / "front" / "aligned.jpg"))
    r = next(r for r in pj_parts(b, "front") if r["ref"] == "U1")
    cx, cy = r["centre"]
    assert tl.pick(b.data, src, cx / img.shape[1], cy / img.shape[0])["ref"] == "U1"
    # taught BAD, with a reason: kept as an example, and R2 joins the check
    b.teach(src, "R2", "bad", "missing", "not fitted on this one")
    ex = ck.load_examples(b.data, board, "front")["examples"]
    assert [(e["ref"], e["label"], e["reason"], e["note"], e["serial"]) for e in ex] == [("R2", "bad", "missing", "not fitted on this one", "SN-T1")]
    assert b.profile["extra"]["R2"]["kind"] == "look" and tl.inspect(b.data, src, "R2")["in_check"]
    assert c.cur["sides"]["front"]["results"] == ck.load(b.data, c.cur["uid"])["sides"]["front"]["results"]   # the check isn't answered
    # a part too small for the full photo (an LED) is checked once taught, here from the reference photo
    b.teach(f"project/{ref_uid}/front", "D1", "good", note="green mark at pin 1")
    assert [e["note"] for e in ck.load_examples(b.data, board, "front")["examples"] if e["ref"] == "D1"] == ["green mark at pin 1"]
    c.start(ref_uid, "SN-T2")
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=2, ppi=290)))[1].tobytes())
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert "D1" in res and "D2" not in res and "R2" in res
    assert "D2" in {k["ref"] for k in c.cur["sides"]["front"]["skipped"]}


def test_part_thumbnails_are_cached_on_disk():
    from aoimap import teach as tl
    b = reference_board()
    src = f"project/{b.project['uid']}/front"
    first = tl.thumb(b.data, src, "R2", 120)
    assert first[:2] == b"\xff\xd8" and max(cv2.imdecode(np.frombuffer(first, np.uint8), 1).shape[:2]) == 120
    cache = b.dir / "front" / "thumbs"
    files = list(cache.glob("R2@120.*.jpg"))
    assert len(files) == 1 and files[0].read_bytes() == first
    assert tl.thumb(b.data, src, "R2", 120) == first and list(cache.glob("R2@120.*.jpg")) == files   # read back, not cut again
    assert b.state()["sides"]["front"]["stamp"]                                                       # the page names thumbs by it
    # a new line-up of the photo makes a new picture and drops the old one
    al = b.dir / "front" / "alignment.json"
    al.write_text(al.read_text())
    tl.thumb(b.data, src, "R2", 120)
    assert len(list(cache.glob("R2@120.*.jpg"))) == 1 and list(cache.glob("R2@120.*.jpg")) != files
    try:
        tl.thumb(b.data, src, "DB1", 120)      # on the back
        raise AssertionError("a back part on the front photo")
    except ValueError as e:
        assert "back" in str(e)


def test_page_loads_versioned_scripts():
    """index.html must load app.js / app.css by a version that changes with them, so a browser never
    runs an old script under a new page (the parts table then shifted a column)."""
    import re
    from aoimap import server
    raw = (server.STATIC / "index.html").read_text()
    assets = re.findall(r'(?:src|href)="(static/[^"]+\.(?:js|css)[^"]*)"', raw)
    assert {"static/app.js", "static/app.css"} <= {a.split("?")[0] for a in assets}
    assert all("?v=" in a for a in assets), assets
    html = server.index_html().decode()
    for name in ("app.js", "app.css"):
        assert f"static/{name}?v={server.asset_version(name)}" in html
    # the parts table's header is drawn by the script with its rows, not written in the page
    assert re.search(r'<table id="lines"[^>]*><thead></thead>', raw)
    # the add bar sits above the table, with the filters
    assert raw.index('id="xp-add"') < raw.index('id="lines"') and raw.index('id="line-filter"') < raw.index('id="xp-add"')


def pj_parts(b, side):
    from aoimap import projects as pj
    return pj._read_json(b.dir / side / "parts_full.json")


def test_part_waiting_for_a_new_photo_is_asked_until_answered():
    from aoimap import check as ck
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    b.set_extra("D7", "look", wait=True)
    assert ck.pending_refs(b.data, board, "front") == {"D7"}
    c = ck.Checks(b.data)
    shot = lambda: cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes()
    c.start(ref_uid, "SN-W1")
    c.photo("front", shot())
    d7 = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "D7")
    assert d7["status"] == "check" and d7["new"] and "teach me" in d7["why"] and c.cur["result"] == "OPEN"
    c.start(ref_uid, "SN-W2")                       # not answered: asked again
    c.photo("front", shot())
    assert next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "D7").get("new")
    # answered right way round, with a note: the note is kept with the good example
    c.answer("front", "D7", "correct", "looks fine")
    ex = [e for e in ck.load_examples(b.data, board, "front")["examples"] if e["ref"] == "D7"]
    assert [(e["label"], e["note"]) for e in ex] == [("good", "looks fine")]
    r = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "D7")
    assert r["note"] == "looks fine"
    c.answer("front", "D7", "correct", "looks fine, checked twice")     # the note changes on its own
    assert [e["note"] for e in ck.load_examples(b.data, board, "front")["examples"] if e["ref"] == "D7"] == ["looks fine, checked twice"]
    assert not ck.pending_refs(b.data, board, "front")
    c.start(ref_uid, "SN-W3")
    c.photo("front", shot())
    d7 = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "D7")
    assert not d7.get("new") and d7["status"] == "ok", d7
    # a wrong answer keeps its reason, and a known fault says it
    c.answer("front", "C5", "wrong", "", missed=True, reason="reversed")
    e = next(e for e in ck.load_examples(b.data, board, "front")["examples"] if e["ref"] == "C5")
    assert e["reason"] == "reversed"


def lines_by_key(b):
    return {l["key"]: l for l in b.state()["lines"]}


def test_tick_bom_lines_into_the_check_and_status():
    from aoimap import check as ck, projects as pj
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    L = lines_by_key(b)
    # every BOM line is listed, DNP ones too, each with whether its parts are checked on this board
    assert L["DNP:R9"]["dnp"] and not L["DNP:R9"]["chosen_kind"] and L["DNP:R9"]["status"][0]["why"].startswith("not on the board")
    st = {r["ref"]: r for l in L.values() for r in l["status"]}
    assert st["C3"]["status"] == "checked" and st["UB1"]["status"] == "nophoto"
    assert st["D1"]["status"] == "skipped" and "too small" in st["D1"]["why"] and "closeup planned" in st["D1"]["why"], st["D1"]
    assert st["R1"]["status"] == "off" and st["R1"]["why"] == "not ticked"
    # ticking the resistors' line puts R1, R2 (front) and RB1 (back) in the check and redraws the map
    before = (b.dir / "front" / "map.jpg").read_bytes()
    b.set_lines({"10K": {"kind": "look"}})
    front = {r["ref"]: r for r in pj_parts(b, "front")}
    assert {"R1", "R2"} <= set(front) and (b.dir / "front" / "map.jpg").read_bytes() != before
    assert "R1" in (b.dir / "parts_map.csv").read_text()
    st = {r["ref"]: r for r in lines_by_key(b)["10K"]["status"]}
    assert st["R1"]["status"] == "skipped" and "closeup planned" in st["R1"]["why"]     # 0603s are small in this photo
    assert st["RB1"]["status"] == "nophoto" and st["RB1"]["side"] == "back"
    assert b.profile["kinds"]["10K"] == "look" and pj._read_json(b._profile_path(board))["kinds"]["10K"] == "look"
    assert "BOM line 10k (3 parts) added to the check" in pj.load(b.data, ref_uid)["log"][-1]["what"]
    # the learned examples of its parts are counted
    b.teach(f"project/{ref_uid}/front", "R1", "good")
    assert {r["ref"]: r for r in lines_by_key(b)["10K"]["status"]}["R1"]["good"] == 1
    # a hidden part says so; unticking takes the parts out again, with the reason kept
    b.set_lines({"T491B106K": {"hidden": "under the shield"}})
    assert {r["ref"]: r for r in lines_by_key(b)["T491B106K"]["status"]}["C1"]["why"] == "hidden (under the shield)"
    b.set_lines({"T491B106K": {"hidden": ""}, "SS14": {"kind": "", "hidden": "checked by the light test"}})
    assert "D8" not in {r["ref"] for r in pj_parts(b, "front")}
    assert lines_by_key(b)["SS14"]["status"][0]["why"] == "not ticked: checked by the light test"
    try:
        b.set_lines({"SS14": {"kind": "nonsense"}}); raise AssertionError("unknown kind")
    except ValueError:
        pass
    # a check of another board follows the reference's new parts: R1 (taught, so checked though small), not D8
    c = ck.Checks(b.data)
    c.start(ref_uid)
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert "R1" in res and "D8" not in res and "R2" in {k["ref"] for k in c.cur["sides"]["front"]["skipped"]}


def test_ticked_lines_carry_to_the_reference_and_the_next_board():
    from aoimap import check as ck, projects as pj
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    b.new_from(ref_uid)                         # another board of the design, not the reference
    b.set_lines({"SMAJ12CA": {"kind": "look"}})
    # the reference is placed again with D7, from its saved alignment
    assert "D7" in {r["ref"] for r in pj._read_json(pj.folder(b.data, ref_uid) / "front" / "parts_full.json")}
    assert pj._read_json(pj.folder(b.data, ref_uid) / "profile.json")["kinds"]["SMAJ12CA"] == "look"
    b.teach(f"project/{ref_uid}/front", "D7", "good", note="no band")
    # a new board of the same design: its maps include D7 as soon as a photo is lined up
    b.new_from(b.project["uid"])
    assert b.profile["kinds"]["SMAJ12CA"] == "look" and any(p["ref"] == "D7" for p in b.design["parts"])
    Hm = fx.camera("front", tilt_deg=1.5)
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm))[1].tobytes())
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    assert {r["ref"]: r for r in pj_parts(b, "front")}["D7"]["checkable"]
    d7 = {r["ref"]: r for r in lines_by_key(b)["SMAJ12CA"]["status"]}["D7"]
    assert d7["status"] == "checked" and d7["good"] == 1        # what was learned applies to this board too
    # and the same after loading the files again
    b2 = Board(b.data)
    b2.add_file("odb", ODB.name, ODB.read_bytes())
    b2.add_file("bom", BOM.name, BOM.read_bytes())
    assert any(p["ref"] == "D7" for p in b2.design["parts"])
    # a check against the reference uses D7 with its learned example
    c = ck.Checks(b.data)
    c.start(ref_uid)
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    assert {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}["D7"]["status"] == "ok"


def test_dnp_line_can_be_ticked():
    from aoimap import projects as pj
    bom = TMP / "dnp" / "bom.csv"
    bom.parent.mkdir(exist_ok=True)
    text = BOM.read_text().replace('"R1, R2, RB1"', '"R1, RB1"')
    bom.write_text(text + '99,1,R2,DNP,"RES 0R 0603",DNP\n98,1,FIDT1,DNP,"Fiducial",DNP\n')
    b = Board(Path(tempfile.mkdtemp(prefix="aoimap-data-")))
    b.add_file("odb", ODB.name, ODB.read_bytes())
    b.add_file("bom", "bom.csv", bom.read_bytes())
    L = lines_by_key(b)
    dnp = L["DNP:R2"]
    assert not L["DNP:FIDT1"]["on_board"] and L["DNP:FIDT1"]["status"][0]["why"].startswith("fiducial")
    assert dnp["dnp"] and dnp["on_board"] == ["R2"] and not dnp["chosen_kind"]
    assert dnp["status"][0]["why"] == "DNP, not ticked" and "R2" in b.design["dnp_on_board"]
    b.set_lines({"DNP:R2": {"kind": "look"}})
    r2 = next(p for p in b.design["parts"] if p["ref"] == "R2")
    assert r2["kind"] == "look" and r2["key"] == "DNP:R2"
    assert pj._read_json(b._profile_path(b.name))["kinds"]["DNP:R2"] == "look"


def test_taught_parts_turn_green_or_red_on_the_map():
    from aoimap import teach as tl
    b = reference_board()
    src = f"project/{b.project['uid']}/front"
    b.teach(src, "C3", "bad", "reversed")
    b.teach(src, "U1", "good")
    b.teach(src, "U1", "bad")      # the latest answer wins
    b.teach(src, "C3", "good")
    assert tl.answers(b.data, b.name, src) == {"C3": "good", "U1": "bad"}
    st = b.sides["front"]
    plain = cv2.imdecode(np.frombuffer(ph.overlay(st["img"], st["placed"], width=0), np.uint8), cv2.IMREAD_COLOR)
    vis = cv2.imdecode(np.frombuffer(ph.overlay(st["img"], st["placed"], width=0, answers=tl.answers(b.data, b.name, src)),
                                     np.uint8), cv2.IMREAD_COLOR)
    def edge(img, ref):        # the colour drawn along the middle of the outline's top edge
        q = np.float32(next(r for r in st["placed"] if r["ref"] == ref)["poly"])
        x, y = ((q[0] + q[1]) / 2).astype(int)
        return img[y, x].astype(int)
    red, green = np.array(ph.ANSWER_COLOURS["bad"]), np.array(ph.ANSWER_COLOURS["good"])
    assert np.abs(edge(vis, "U1") - red).max() < 40 and np.abs(edge(plain, "U1") - red).max() > 40
    assert np.abs(edge(vis, "C3") - green).max() < 40
    # a check's answer is its own: teaching a check photo leaves the reference's map as it was
    assert tl.answers(b.data, b.name, f"check/x/front") == {}


def test_side_lists_every_part_not_only_the_checked_ones():
    b = reference_board()
    s = b.side_state("front")
    on_side = [c["ref"] for c in b.design["components"] if c["side"] == "top"]
    refs = [r["ref"] for r in s["placed"]] + [r["ref"] for r in s["others"]]
    assert sorted(refs) == sorted(on_side) and len(set(refs)) == len(refs)
    assert s["summary"]["parts"] == len(on_side) and s["summary"]["in_check"] == len(s["placed"])
    assert s["others"] and all(not r["checkable"] and r["reason"] and r["px"] > 0 for r in s["others"])
    dnp = next(r for r in s["others"] if r["ref"] == "R9") if "R9" in on_side else None
    assert dnp is None or dnp["reason"].startswith("DNP")
    # ticked into the check, a part leaves the not-checked list
    r = s["others"][0]["ref"]
    b.set_extra(r, "look")
    s = b.side_state("front")
    assert r in {p["ref"] for p in s["placed"]} and r not in {p["ref"] for p in s["others"]}


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
    # what the check learns, the parts added by hand and the board's choices are kept in the database too
    from aoimap import check as ck
    ck.LEARNED_LISTENERS.append(h.changed_learned)
    b.on_profile = h.changed_profile
    try:
        board = b.project["board"]
        b.set_extra("D7", "look", wait=True)
        cnt = lambda: h._run(lambda c: (c.execute("SELECT COUNT(*) AS n FROM aoi_learned WHERE board=%s", (board,)), c.fetchone()["n"])[1])
        for _ in range(100):
            h.flush()
            if cnt() == 1:
                break
            time.sleep(0.1)
        row = h._run(lambda c: (c.execute("SELECT * FROM aoi_learned WHERE board=%s", (board,)), c.fetchone())[1])
        img = (ck.learned_dir(b.data, board, "front") / row["file"]).read_bytes()
        assert row["ref"] == "C3" and row["label"] == "bad" and row["image"] == img and row["bytes"] == len(img)
        import hashlib
        assert row["sha256"] == hashlib.sha256(img).hexdigest()
        ex = h._run(lambda c: (c.execute("SELECT ref, kind, wait_for_photo FROM aoi_board_extra WHERE board=%s", (board,)), c.fetchall())[1])
        assert [(e["ref"], e["kind"], e["wait_for_photo"]) for e in ex] == [("D7", "look", 1)]
        prof = h._run(lambda c: (c.execute("SELECT profile FROM aoi_board_profiles WHERE board=%s", (board,)), c.fetchone())[1])
        assert "D7" in json.loads(prof["profile"])["extra"]
        # ticking a BOM line is kept in the database's copy of the board's choices
        b.set_lines({"10K": {"kind": "look"}})
        for _ in range(100):
            h.flush()
            prof = h._run(lambda c: (c.execute("SELECT profile FROM aoi_board_profiles WHERE board=%s", (board,)), c.fetchone())[1])
            if json.loads(prof["profile"])["kinds"].get("10K") == "look":
                break
            time.sleep(0.1)
        assert json.loads(prof["profile"])["kinds"]["10K"] == "look"
        # deleting an example deletes it from the database; a fresh start backfills from the folders
        ch.answer("front", "C3", None)
        h.flush()
        assert cnt() == 0
        ch.answer("front", "C3", "wrong", reason="reversed")
        h._run(lambda c: c.execute("DELETE FROM aoi_learned WHERE board=%s", (board,)))
        h._dirty.clear()
        h.catch_up()
        assert cnt() == 1
        assert h._run(lambda c: (c.execute("SELECT reason FROM aoi_learned WHERE board=%s", (board,)), c.fetchone()["reason"])[1]) == "reversed"
    finally:
        ck.LEARNED_LISTENERS.remove(h.changed_learned)
        for t in ("aoi_learned", "aoi_board_extra", "aoi_board_profiles", "aoi_learned_reference_wrong"):
            h._run(lambda c: c.execute(f"DELETE FROM {t} WHERE board=%s", (b.project["board"],)))
    h._run(lambda c: c.execute("DELETE FROM aoi_checks WHERE uid=%s", (ch.cur["uid"],)))
    h._run(lambda c: c.execute("DELETE FROM aoi_projects WHERE uid=%s", (first,)))


def _api(port, path, body=None):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def _serve(data):
    """The app on a free port, without the database (as with --no-db)."""
    import threading
    from http.server import ThreadingHTTPServer
    from aoimap import server
    app = server.App(data, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return app, srv, srv.server_address[1]


def _tree(d: Path) -> dict:
    """Every file's content (opening a board writes its map and parts lists again, the same)."""
    import hashlib
    return {f.relative_to(d).as_posix(): hashlib.sha1(f.read_bytes()).hexdigest() for f in d.rglob("*") if f.is_file()}


def test_boards_mapped_before_projects_become_a_project_unmoved():
    """Data from before projects existed: its design gets a project of its own (named after it, to be
    renamed), chosen and opened, and not one file is moved or changed."""
    from aoimap import groups as gr
    from aoimap.check import Checks
    b = reference_board()
    design, ref_uid = b.name, b.project["uid"]
    c = Checks(b.data)
    c.start(ref_uid, "SN-1")
    c.close()
    before = _tree(b.data)
    app, srv, port = _serve(b.data)
    try:
        after = _tree(b.data)
        assert set(after) - set(before) == {"groups.json"}
        # (map.jpg is drawn again on the photo as saved, a JPEG, where the test drew it on the PNG taken)
        assert all(after[k] == v for k, v in before.items() if not k.endswith("map.jpg")), \
            [k for k, v in before.items() if after.get(k) != v]
        g = _api(port, "api/groups")
        assert [x["name"] for x in g["projects"]] == [design] and g["current"] == g["projects"][0]["id"]
        p = g["projects"][0]
        assert p["boards"] == [design] and p["mapped"] == 1 and p["references"] == 1 and p["checks"] == 1
        st = _api(port, "api/state")
        assert st["project"]["uid"] == ref_uid and st["group"]["name"] == design
        # renamed, it keeps everything: boards, checks, the reference
        r = _api(port, "api/groups/rename", {"id": p["id"], "name": "PTC"})
        assert r["groups"]["projects"][0]["name"] == "PTC" and r["state"]["project"]["uid"] == ref_uid
        assert [x["uid"] for x in _api(port, "api/history")["projects"]] == [ref_uid]
        assert len(_api(port, "api/history/checks")["checks"]) == 1
        assert _api(port, "api/history")["total"] == 1
        # a restart leaves it chosen, with the same board open
        gr.ensure(b.data)
        assert gr.load(b.data)["current"] == p["id"] and len(gr.load(b.data)["projects"]) == 1
    finally:
        srv.shutdown()
    app2, srv2, port2 = _serve(b.data)
    try:
        assert _api(port2, "api/state")["project"]["uid"] == ref_uid
        assert _api(port2, "api/state")["group"]["name"] == "PTC"
    finally:
        srv2.shutdown()


def test_new_project_and_switching_keep_each_projects_boards_apart():
    import urllib.request
    from aoimap import groups as gr
    b = reference_board()
    first_design, first_uid = b.name, b.project["uid"]
    app, srv, port = _serve(b.data)
    try:
        ptc = _api(port, "api/groups")["current"]
        _api(port, "api/groups/rename", {"id": ptc, "name": "PTC"})
        assert "error" in _api(port, "api/groups/new", {"name": "ptc"})          # names are unique
        assert "error" in _api(port, "api/groups/new", {"name": "  "})
        r = _api(port, "api/groups/new", {"name": "Motor driver"})
        other = r["groups"]["current"]
        assert other != ptc and r["state"]["project"] is None and r["state"]["design"] is None
        assert r["state"]["group"] == {"id": other, "name": "Motor driver", "boards": []}
        # the new project shows none of PTC's boards, checks, references or learned examples
        assert _api(port, "api/history")["projects"] == [] and _api(port, "api/history/checks")["checks"] == []
        assert _api(port, "api/check/learned")["boards"] == []
        assert [x["uid"] for x in _api(port, "api/history?group=all")["projects"]] == [first_uid]
        # its ODB++ and BOM go in with the Files tab's loaders; the design joins the project chosen
        odb2 = TMP / "motor_odb.tgz"
        odb2.write_bytes(ODB.read_bytes())

        def up(kind, f):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/upload?kind={kind}&name={f.name}", data=f.read_bytes(), method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        up("odb", odb2)
        st = up("bom", BOM)
        assert st["name"] == "motor" and st["group"]["boards"] == ["motor"]
        motor_uid = st["project"]["uid"]
        assert [x["uid"] for x in _api(port, "api/history")["projects"]] == [motor_uid]
        assert gr.of_board(gr.load(b.data), "motor")["name"] == "Motor driver"
        # back to PTC: its board is opened again, and the lists are PTC's
        r = _api(port, "api/groups/switch", {"id": ptc})
        assert r["groups"]["current"] == ptc and r["state"]["project"]["uid"] == first_uid and r["state"]["name"] == first_design
        assert [x["uid"] for x in _api(port, "api/history")["projects"]] == [first_uid]
        assert {x["uid"] for x in _api(port, "api/history?group=all")["projects"]} == {first_uid, motor_uid}
        assert [x["uid"] for x in _api(port, f"api/history?group={other}")["projects"]] == [motor_uid]
        # and back again: the board that project had open
        assert _api(port, "api/groups/switch", {"id": other})["state"]["project"]["uid"] == motor_uid
        # loading PTC's design while in another project chooses PTC (a design is in one project only)
        _api(port, f"api/history/{first_uid}/open", {})
        assert gr.load(b.data)["current"] == ptc
        assert "error" in _api(port, "api/groups/switch", {"id": "nope"})
        # remembered for the next start
        assert gr.load(b.data)["open"] == {ptc: first_uid, other: motor_uid}
    finally:
        srv.shutdown()


def test_check_in_progress_is_put_away_when_another_project_is_chosen():
    from aoimap import groups as gr
    b = reference_board()
    app, srv, port = _serve(b.data)
    try:
        ptc = _api(port, "api/groups")["current"]
        assert _api(port, "api/check/start", {"reference": b.project["uid"], "serial": "X1"})["check"]
        _api(port, "api/groups/new", {"name": "Other"})
        assert _api(port, "api/check/state")["check"] is None
        _api(port, "api/groups/switch", {"id": ptc})
        assert len(_api(port, "api/history/checks")["checks"]) == 1      # kept, to reopen from History
        assert gr.counts(b.data, gr.get(gr.load(b.data), ptc))["checks"] == 1
        # opened from History's All projects while in another project, it chooses its own project
        other = next(g["id"] for g in _api(port, "api/groups")["projects"] if g["name"] == "Other")
        _api(port, "api/groups/switch", {"id": other})
        uid = _api(port, "api/history/checks?group=all")["checks"][0]["uid"]
        assert _api(port, "api/check/open", {"uid": uid})["check"]["uid"] == uid
        assert gr.load(b.data)["current"] == ptc and _api(port, "api/state")["project"]["uid"] == b.project["uid"]
    finally:
        srv.shutdown()


def test_non_polarised_parts_have_no_pin_1_dot_and_any_turn_passes():
    from aoimap import check as ck
    img = np.full((120, 200, 3), 60, np.uint8)
    poly = [[40, 40], [80, 40], [80, 60], [40, 60]]
    rec = {"ref": "X", "value": "", "poly": poly, "centre": [60, 50], "box": [40, 40, 40, 20], "px": 20.0,
           "pin1": [42, 50], "checkable": True, "reason": ""}
    red = lambda jpg: int(((lambda v: (v[..., 2] > 150) & (v[..., 0] < 100) & (v[..., 1] < 100))(
        cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR))).sum())
    # the side map: a pin 1 dot for a polarised part, just the outline for a two-pin look-only one;
    # a look-only IC still shows its pin 1
    assert red(ph.overlay(img, [dict(rec, kind="ic")], width=0)) > 0
    class m:         # board units are photo pixels here
        apply = staticmethod(lambda pts: np.float32(pts).reshape(-1, 2))
    p = {"ref": "X", "kind": "look", "value": "", "side": "top", "x": 60, "y": 50, "rot": 0, "mirror": False, "turn": -1,
         "bbox": [-20, -10, 20, 10], "pin1": [42, 50], "pin1_dir": "W", "pin1_how": "named", "pins": 2}
    two = ph.place({"parts": [p]}, "front", img.shape, m, 1, -1)
    many = ph.place({"parts": [dict(p, pins=14)]}, "front", img.shape, m, 1, -1)
    assert "pin1" not in two[0] and red(ph.overlay(img, two, width=0)) == 0
    assert "pin1" in many[0] and red(ph.overlay(img, many, width=0)) > 0
    # a look-only part with one end marked, turned round on the board: fine, and its learned examples count either way
    ref = np.full((80, 120, 3), 90, np.uint8); ref[30:50, 40:80] = 200; ref[30:50, 40:48] = 20
    board = ref.copy(); board[30:50, 40:80] = cv2.rotate(ref[30:50, 40:80], cv2.ROTATE_180)
    part = {"ref": "C1", "kind": "look", "value": "", "box": [40, 30, 40, 20]}
    assert ck.check_part(board, ref, part, 10)["status"] == "ok"
    # unless the reference shows it wrong (answered BAD or MISSING there): then matching the reference
    # doesn't pass it, a good example does
    assert ck.check_part(board, ref, part, 10, reference_wrong=True)["status"] == "check"
    good = board[24:56, 34:86].astype(np.float32)
    assert ck.check_part(board, ref, part, 10, {"good": [(good, {"serial": "8"})]}, reference_wrong=True)["status"] == "ok"
    pol = ck.check_part(board, ref, dict(part, kind="tantalum"), 10)
    assert pol["status"] == "reversed", pol
    # a fault taught before (here: the part missing) is still found
    empty = ref.copy(); empty[30:50, 40:80] = 90; empty[34:46, 42:46] = 160; empty[34:46, 74:78] = 160
    ex = empty[24:56, 34:86].astype(np.float32)
    bad = ck.check_part(empty, ref, part, 10, learned={"bad": [(ex, {"serial": "7"})]})
    assert bad["status"] == "fault", bad


def test_missing_and_not_stuffed_answers():
    from aoimap import teach as tl
    from aoimap.check import Checks, load, load_examples
    b = reference_board()
    c = Checks(b.data)
    board, H2 = b.project["board"], fx.camera("front", tilt_deg=-3, ppi=280)
    shot = lambda *rev: cv2.imencode(".png", fx.render("front", H2, reversed_refs=rev))[1].tobytes()

    def run(serial, *rev):
        c.start(b.project["uid"], serial)
        c.photo("front", shot(*rev))
        return {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}

    res = run("SN-1", "C3")
    assert res["C3"]["status"] == "reversed" and res["D8"]["status"] == "check"
    # MISSING is a bad answer with the reason "missing": a fault
    c.answer("front", "C3", "wrong", reason="missing")
    assert load_examples(b.data, board, "front")["examples"][-1]["reason"] == "missing" and c.cur["result"] == "FAIL"
    # NS on this board: not a fault, its answer and example go, and nothing is left to answer
    c.set_ns("front", "C3")
    c.set_ns("front", "D8")
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["C3"]["status"] == res["D8"]["status"] == "ns" and not res["C3"].get("answer")
    assert not load_examples(b.data, board, "front")["examples"]
    for r in c.cur["sides"]["front"]["results"]:
        if r["status"] not in ("ok", "ns"):
            c.answer("front", r["ref"], "correct")
    assert load(b.data, c.cur["uid"])["result"] == "PASS"
    assert load_examples(b.data, board, "front")["not_stuffed"] == {"SN-1": ["C3", "D8"]}
    src = f"check/{c.cur['uid']}/front"
    assert tl.inspect(b.data, src, "C3")["verdict"] == "ns" and tl.answers(b.data, board, src)["D8"] == "ns"
    try:
        c.answer("front", "C3", "correct"); raise AssertionError("answered an NS part")
    except ValueError:
        pass
    # undo: checked and asked about again, as before
    c.set_ns("front", "D8", False)
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["D8"]["status"] == "check" and c.cur["result"] == "OPEN"
    # only that serial: the next board is checked as usual, until NS is set for every board of the design
    res = run("SN-2")
    assert res["C3"]["status"] == "ok" and res["D8"]["status"] == "check"
    c.set_ns("front", "D8", everywhere=True)
    res = run("SN-3")
    assert res["D8"]["status"] == "ns" and "any board" in res["D8"]["why"]
    # the side map of a mapped board shows it NS, and undoing it there clears every scope
    psrc = f"project/{b.project['uid']}/front"
    assert tl.answers(b.data, board, psrc)["D8"] == "ns"
    b.set_ns(psrc, "D8", False)
    assert load_examples(b.data, board, "front")["not_stuffed"] == {"SN-1": ["C3"]}
    assert run("SN-4")["D8"]["status"] == "check"



def test_covered_on_one_board_is_still_checked_on_the_others():
    """COVERED (a heatsink...) on one board: not a fault there, not asked about, and never a good or bad
    example; every other board still checks the part."""
    from aoimap import teach as tl, viewer as vw
    from aoimap.check import Checks, load, load_examples
    b = reference_board()
    c = Checks(b.data)
    board, H2 = b.project["board"], fx.camera("front", tilt_deg=-3, ppi=280)
    shot = lambda *rev: cv2.imencode(".png", fx.render("front", H2, reversed_refs=rev))[1].tobytes()

    def run(serial, *rev):
        c.start(b.project["uid"], serial)
        c.photo("front", shot(*rev))
        return {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}

    res = run("BRD-H1", "C3")
    assert res["C3"]["status"] == "reversed"
    c.answer("front", "C3", "wrong")
    c.set_covered("front", "C3")                      # under a heatsink on this board: its BAD answer goes
    c.set_covered("front", "D8", reason="Label")
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["C3"]["status"] == "covered" and res["C3"]["cover"] == "heatsink" and not res["C3"].get("answer")
    assert res["D8"]["cover"] == "label" and "covered: label" in res["D8"]["why"]
    lx = load_examples(b.data, board, "front")
    assert lx["covered"] == {"BRD-H1": {"C3": "heatsink", "D8": "label"}}
    assert [e["label"] for e in lx["examples"]] == ["covered", "covered"]     # kept only as covered looks
    for r in c.cur["sides"]["front"]["results"]:
        if r["status"] not in ("ok", "ns", "covered"):
            c.answer("front", r["ref"], "correct")
    assert load(b.data, c.cur["uid"])["result"] == "PASS"
    src = f"check/{c.cur['uid']}/front"
    assert tl.inspect(b.data, src, "C3")["verdict"] == "covered" and tl.answers(b.data, board, src)["C3"] == "covered"
    assert "C3" in ck_row_covered(c.cur)
    # undo by pressing it again: asked about as before
    c.set_covered("front", "D8", False)
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["D8"]["status"] == "check" and c.cur["result"] == "OPEN"
    assert [e["ref"] for e in load_examples(b.data, board, "front")["examples"] if e["label"] == "covered"] == ["C3"]
    # answering it GOOD or BAD says it can be seen after all
    c.answer("front", "C3", "wrong", reason="reversed")
    assert c.cur["sides"]["front"]["results"][0] and load_examples(b.data, board, "front")["covered"] == {}
    c.set_covered("front", "C3")
    # the next board checks C3 as usual, and its good one isn't taken for the covered look
    res = run("BRD-H2")
    assert res["C3"]["status"] == "ok" and "suggest" not in res["C3"], res["C3"]
    # COVERED on a mapped board's photo (Front/Back): shown as covered there, the part stays in the check
    psrc = f"project/{b.project['uid']}/front"
    b.set_covered(psrc, "R2", reason="shield")
    assert "R2" in {p["ref"] for p in b.design["parts"]}
    st = vw.status(b)["parts"]["R2"]
    assert st["st"] == "covered" and st["cover"] == "shield"
    assert tl.inspect(b.data, psrc, "R2")["covered"] == "shield"
    b.set_covered(psrc, "R2", False)
    assert vw.status(b)["parts"]["R2"]["st"] != "covered"


def ck_row_covered(c):
    from aoimap.check import row
    return row(c)["front"]["covered"]


def test_covered_on_the_reference_isnt_the_standard():
    """BRD006 has heatsinks on parts PTC002 shows: answered COVERED on the reference, later boards still
    check those parts, against good answers from boards where they can be seen (or ask for one)."""
    from aoimap import check as ck, teach as tl
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    src = f"project/{ref_uid}/front"
    b.teach(src, "C3", "bad", "missing")              # answered before: COVERED takes its place
    b.set_covered(src, "C3")
    lx = ck.load_examples(b.data, board, "front")
    assert not lx["reference_wrong"] and [e["label"] for e in lx["examples"] if e["ref"] == "C3"] == ["covered"]
    assert tl.inspect(b.data, src, "C3")["verdict"] == "covered"
    shot = lambda: cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes()
    c = ck.Checks(b.data)
    c.start(ref_uid, "PTC-H1")
    c.photo("front", shot())
    r = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "C3")
    # asked about, not passed for matching the reference (the drawn board has no heatsink, so it looks like
    # the reference's covered look: "covered?"; with no such look it says there's no standard yet)
    assert r["status"] == "check" and not r.get("learned") and r.get("suggest") == "covered", r
    # answered GOOD on a board where it can be seen: later boards pass it by that
    c.answer("front", "C3", "correct")
    c.start(ref_uid, "PTC-H2")
    c.photo("front", shot())
    r = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "C3")
    assert r["status"] == "ok" and r.get("learned") == "good", r
    # the other mapped boards of the design still check it (Front/Back of PTC002)
    uid2 = second_board(b, ref_uid)
    info = tl.inspect(b.data, f"project/{uid2}/front", "C3")
    assert info["verdict"] == "correct" and info["reference_covered"] == "heatsink" and not info["covered"], info
    # undone on the reference: its look is the standard again, and an open check compares C3 again
    c.start(ref_uid, "PTC-H3")
    c.photo("front", shot())
    b.open_project(ref_uid)
    b.set_covered(src, "C3", False)
    c.refresh()
    r = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "C3")
    assert r["status"] == "ok" and not r.get("learned"), r


def test_a_part_that_looks_covered_is_asked_covered():
    from aoimap import check as ck
    ref = np.full((80, 120, 3), 90, np.uint8); ref[30:50, 40:80] = 200; ref[30:50, 40:48] = 20
    part = {"ref": "U3", "kind": "ic", "value": "", "box": [40, 30, 40, 20]}
    sink = ref.copy(); sink[22:58, 32:88] = 140; sink[22:58, 32:88:6] = 230     # fins over the part
    ex = sink[24:56, 34:86].astype(np.float32)
    r = ck.check_part(sink, ref, part, 10)
    assert r["status"] != "ok" and "suggest" not in r
    r = ck.check_part(sink, ref, part, 10, {"covered": [(ex, {"note": "heatsink", "serial": "BRD006"})]})
    assert r["status"] == "check" and r["suggest"] == "covered" and "heatsink" in r["why"], r
    # a board where the part can be seen is passed as usual
    assert ck.check_part(ref, ref, part, 10, {"covered": [(ex, {"note": "heatsink"})]})["status"] == "ok"
    # the reference has it covered: no standard until a good example from another board
    r = ck.check_part(ref, sink, part, 10, reference_covered="heatsink")
    assert r["status"] == "check" and "no standard yet" in r["why"]
    good = ref[24:56, 34:86].astype(np.float32)
    assert ck.check_part(ref, sink, part, 10, {"good": [(good, {"serial": "PTC002"})]}, reference_covered="heatsink")["status"] == "ok"


def test_covered_on_the_new_reference_keeps_the_old_by_default():
    from aoimap import check as ck, refchange as rc
    b = reference_board()
    old, board = b.project["uid"], b.project["board"]
    new = second_board(b, old)
    b.set_covered(f"project/{new}/front", "C3")
    c = _built(b.data, rc.start(b.data, old, new)["id"])
    row = next(r for r in c["rows"] if r["ref"] == "C3")
    assert "covered_new" in row["kinds"] and c["decisions"][row["key"]]["choice"] == "keep_old"
    for r in c["rows"]:
        if r["key"] not in c["decisions"]:
            rc.decide(b.data, c["id"], [r["key"]], "accept")
    rc.finish(b.data, c["id"])
    lx = ck.load_examples(b.data, board, "front")
    kept = [e for e in lx["examples"] if e["ref"] == "C3" and e["label"] == "good"]
    assert kept and kept[0]["source"] == f"project/{old}/front"
    # later boards: checked against the old look kept as a good example
    chk = ck.Checks(b.data)
    chk.start(new, "SN-R1")
    chk.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    r = next(r for r in chk.cur["sides"]["front"]["results"] if r["ref"] == "C3")
    assert r["status"] == "ok" and r.get("learned") == "good", r


# ---- answers carry to every board of the design; changing the reference ----------------------

def second_board(b, ref_uid, *rev, tilt=-2.0):
    """Another mapped board of the design, its front lined up (rev: parts drawn the wrong way round)."""
    b.new_from(ref_uid)
    Hm = fx.camera("front", tilt_deg=tilt, ppi=270)
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm, reversed_refs=rev))[1].tobytes())
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    return b.project["uid"]


def test_an_answer_on_the_reference_is_looked_at_on_every_board():
    from aoimap import check as ck, viewer as vw
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    uid2 = second_board(b, ref_uid)
    in_check = lambda: {p["ref"] for p in b.design["parts"]}
    assert not {"R1", "R2", "D7"} & in_check()
    # on the reference: R1 answered GOOD, R2 NS on this board, D7 NS on every board
    b.open_project(ref_uid)
    src = f"project/{ref_uid}/front"
    b.teach(src, "R1", "good")
    b.set_ns(src, "R2")
    b.set_ns(src, "D7", everywhere=True)
    assert {"R1", "R2"} <= in_check() and "D7" not in in_check()
    # the other board, opened again: R1 and R2 are in its check, placed on its photo, new since it was open
    b.open_project(uid2)
    assert {"R1", "R2"} <= in_check() and "D7" not in in_check()
    placed = {r["ref"]: r for r in b.sides["front"]["placed"]}
    assert "R1" in placed and "R2" in placed      # placed on its photo (small: checked once answered)
    assert set(b.project["new_in_check"]) == {"R1", "R2"} and any("New in the check" in m for m in b.messages)
    st = vw.status(b)["parts"]
    assert st["R1"]["st"] == "checked" and st["R1"]["new"] and st["R1"]["verdict"] == "correct", st["R1"]
    assert st["D7"]["st"] == "ns"
    # answered here: no longer new
    b.teach(f"project/{uid2}/front", "R1", "good")
    assert "R1" not in b.project["new_in_check"]
    # a board open here can't put back an older list of what to check that another board has added to
    other = Board(b.data)
    other.open_project(ref_uid)
    other.set_extra("D7", "look")
    b.set_option("min_px", 13)               # saves this board's choices
    prof = ck.board_profile(b.data, board)
    assert {"R1", "R2", "D7"} <= set(prof["extra"])
    other.set_extra("D7", remove=True)
    # and a check of another board covers R1 and R2
    c = ck.Checks(b.data)
    c.start(ref_uid, "SN-C1")
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["R1"]["status"] == "ok" and "R2" in res and "D7" not in res


def test_a_check_picks_up_parts_put_in_the_check_after_it_was_done():
    from aoimap import check as ck
    b = reference_board()
    ref_uid = b.project["uid"]
    c = ck.Checks(b.data)
    c.start(ref_uid, "SN-L1")
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
    for r in c.cur["sides"]["front"]["results"]:
        if r["status"] != "ok":
            c.answer("front", r["ref"], "correct")
    uid, before = c.cur["uid"], c.cur["result"]
    assert before == "PASS" and "R2" not in {r["ref"] for r in c.cur["sides"]["front"]["results"]}
    c.close()
    b.teach(f"project/{ref_uid}/front", "R2", "good")
    c.open(uid)
    res = {r["ref"]: r for r in c.cur["sides"]["front"]["results"]}
    assert res["R2"]["since"] and res["R2"]["status"] == "ok", res["R2"]
    assert all(r.get("answer") == "correct" for r in c.cur["sides"]["front"]["results"] if r["status"] != "ok" and r["ref"] != "R2")
    assert "New in the check since" in c.cur["log"][-1]["what"] and ck.load(b.data, uid)["sides"]["front"]["results"][-1]["ref"] == "R2"


def test_bad_or_missing_on_the_reference_isnt_passed_for_matching_it():
    from aoimap import check as ck
    b = reference_board()
    ref_uid, board = b.project["uid"], b.project["board"]
    src = f"project/{ref_uid}/front"
    b.teach(src, "R1", "bad", "missing")       # by its look: R1 shows wrong on the reference
    assert ck.load_examples(b.data, board, "front")["reference_wrong"] == {ref_uid: ["R1"]}
    c = ck.Checks(b.data)
    shot = lambda: cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes()
    c.start(ref_uid, "SN-W1")
    c.photo("front", shot())
    r1 = next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "R1")
    assert r1["status"] != "ok", r1
    # GOOD on the reference again: its look is the standard
    ex = next(e for e in ck.load_examples(b.data, board, "front")["examples"] if e["ref"] == "R1")
    c.forget(board, "front", ex["id"])
    assert ck.load_examples(b.data, board, "front")["reference_wrong"] == {}
    b.teach(src, "R1", "good")
    c.start(ref_uid, "SN-W2")
    c.photo("front", shot())
    assert next(r for r in c.cur["sides"]["front"]["results"] if r["ref"] == "R1")["status"] == "ok"


def _built(data, cid):
    import time as _t
    from aoimap import refchange as rc
    for _ in range(600):
        if cid not in rc._BUILDING:
            return rc.view(data, cid)
        _t.sleep(0.1)
    raise AssertionError("the comparison didn't finish")


def test_changing_the_reference_asks_about_each_difference():
    from aoimap import check as ck, projects as pj, refchange as rc
    b = reference_board()
    old, board = b.project["uid"], b.project["board"]
    b.teach(f"project/{old}/front", "R1", "good")
    new = second_board(b, old, "C3")         # C3 is the wrong way round on the new one
    c = _built(b.data, rc.start(b.data, old, new)["id"])
    rows = {r["ref"]: r for r in c["rows"]}
    assert "C3" in rows and "turned" in rows["C3"]["kinds"], c["rows"]
    assert c["same"]["front"] and "C5" in c["same"]["front"] and c["status"] == "open"
    from aoimap import teach as tl
    assert tl.crop(b.data, f"project/{new}/front", "C3", "reference", old)[:2] == b"\xff\xd8"
    # the old reference stays in use until every difference has a decision
    assert [r["uid"] for r in ck.references(b.data)] == [old]
    try:
        rc.finish(b.data, c["id"]); raise AssertionError("finished with parts left")
    except ValueError as e:
        assert "still need a decision" in str(e)
    rc.approve_matching(b.data, c["id"])
    for r in c["rows"]:
        if r["key"] not in rc.load(b.data, c["id"])["decisions"]:
            rc.decide(b.data, c["id"], [r["key"]], "wrong" if r["ref"] == "C3" else "keep_old")
    v = rc.finish(b.data, c["id"])
    assert v["status"] == "done" and [r["uid"] for r in ck.references(b.data)] == [new]
    lx = ck.load_examples(b.data, board, "front")
    assert lx["reference_wrong"].get(new) == ["C3"] and any(e["ref"] == "C3" and e["label"] == "bad" for e in lx["examples"])
    assert "Reference for checks changed" in pj.load(b.data, new)["log"][-1]["what"]
    assert rc.changes(b.data, board)[0]["status"] == "done"
    # undo: the old reference is back, and what the decisions added is gone
    rc.undo(b.data, c["id"])
    assert [r["uid"] for r in ck.references(b.data)] == [old]
    lx = ck.load_examples(b.data, board, "front")
    assert not lx["reference_wrong"].get(new) and not any(e["ref"] == "C3" for e in lx["examples"])


def test_new_photo_of_the_reference_keeps_the_old_one_until_reviewed():
    from aoimap import check as ck, refchange as rc
    b = reference_board()
    uid = b.project["uid"]
    Hm = fx.camera("front", tilt_deg=2, ppi=275)
    b.set_photo("front", cv2.imencode(".png", fx.render("front", Hm))[1].tobytes())
    refs = ck.references(b.data)
    assert len(refs) == 1 and refs[0]["uid"] != uid and not b.project.get("reference")
    snap = refs[0]["uid"]
    b.align("front", click(Hm, b.design, ["FIDT1", "FIDT2", "FIDT3", "FIDT4"], "front"))
    assert [r["uid"] for r in ck.references(b.data)] == [snap]       # lining up again doesn't copy it twice
    c = _built(b.data, rc.start(b.data, snap, uid, "photo")["id"])
    for r in c["rows"]:
        rc.decide(b.data, c["id"], [r["key"]], "accept")
    rc.finish(b.data, c["id"])
    assert [r["uid"] for r in ck.references(b.data)] == [uid]


# ---- the board viewer on the Map tab (netlist.py, viewer.py) ---------------------------------

def test_nets_and_pins_read_from_the_odb():
    from aoimap import netlist as nl
    d = nl.load(ODB)
    assert d["nets"] == fx.NETS and d["units"] == "inch"
    parts = {c["ref"]: c for side in ("top", "bot") for c in d["parts"][side]}
    assert len(parts) == len(fx.PARTS)
    net = lambda ref, pin: next((d["nets"][q[3]] if q[3] >= 0 else None) for q in parts[ref]["pins"] if q[0] == pin)
    # top pins carry their net; the bottom's come from the SNT records; $NONE$ is no net
    assert net("U1", "1") == "VCC" and net("U1", "2") == "SDA" and net("U1", "5") == "GND"
    assert net("UB1", "2") == "SDA" and net("DB4", "2") == "GND" and net("RB1", "1") == "VCC"
    assert net("FIDT1", "1") is None and net("U2", "2") is None
    assert any("eda/data" in n for n in d["notes"])
    u2 = next(p for p in odblib.load(ODB)[0].parts if p.ref == "U2")
    assert {q[0]: q[1:3] for q in parts["U2"]["pins"]} == u2.pins       # the same positions as the parts map's
    pk = d["packages"][parts["C1"]["package"]]
    assert pk["name"] == "CAPT3528" and [round(v, 4) for v in pk["pins"][0][1:3]] == [0.02, 0.01] and not pk["pins"][0][3]
    assert d["packages"][parts["FIDT1"]["package"]]["pins"][0][3] is True     # a round pad
    assert d["outline"][:4] == [[0, 0], [6, 0], [6, 4], [0, 4]]


def _ptc_odb():
    for f in sorted((Path.home() / "aoimap-data" / "projects").glob("*-ptc4a_v1j/source/odb.*")):
        return f
    return None


def test_ptc_nets():
    """On the real PTC board, when its ODB++ is on this machine."""
    from aoimap import netlist as nl
    f = _ptc_odb()
    if not f:
        print("skip test_ptc_nets (no PTC ODB++ here)")
        return
    d = nl.load(f)
    count = {}
    for side in ("top", "bot"):
        for c in d["parts"][side]:
            for q in c["pins"]:
                if q[3] >= 0:
                    count[d["nets"][q[3]]] = count.get(d["nets"][q[3]], 0) + 1
    assert len(d["nets"]) > 400 and count["GND"] > 500 and count["VCC_3V3"] > 2 and count["VP48_IN"] > 2
    assert "$NONE$" not in count
    lt3 = next(c for c in d["parts"]["top"] if c["ref"] == "LT3")
    assert [d["nets"][q[3]] for q in lt3["pins"]][0] == "VP48_IN"
    assert len(d["outline"]) >= 4 and not d["notes"]


def test_viewer_api():
    from aoimap import photo as ph
    b = reference_board()
    app, srv, port = _serve(b.data)
    try:
        g = _api(port, "api/viewer/geometry")
        assert g["board"] == b.name and g["nets"] == fx.NETS and g["units"] == "inch"
        parts = {p["r"]: p for p in g["parts"]}
        assert len(parts) == len(fx.PARTS) and parts["FIDT1"]["f"] and parts["UB1"]["s"] == "bot"
        assert parts["C1"]["v"] == "T491B106K" and g["lines"][parts["C1"]["l"]]["qty"] == 8
        assert g["sides"]["front"]["photo"] and not g["sides"]["back"]["photo"]
        assert g["sides"]["front"]["orient"] == [1, -1] and g["sides"]["back"]["orient"] == [1, 1]
        pins = _api(port, "api/viewer/pins")["pins"]
        sda = g["nets"].index("SDA")
        assert [q[0] for q in pins["U1"] if q[3] == sda] == ["2"] and [q[0] for q in pins["UB1"] if q[3] == sda] == ["2"]
        assert pins["C1"][0][4:6] == [0.02, 0.01]
        assert (b.data / "boards" / b.name / "nets.json").is_file()       # read once per design
        st = _api(port, "api/viewer/status")["parts"]
        assert st["C1"]["st"] == "checked" and st["C1"]["kind"] == "tantalum"
        assert st["R1"]["st"] == "off" and st["UB1"]["st"] == "nophoto"
        b.teach(f"project/{b.project['uid']}/front", "C3", "bad", "reversed")
        b.set_ns(f"project/{b.project['uid']}/front", "C5")
        st = _api(port, "api/viewer/status")["parts"]
        assert st["C3"]["st"] == "bad" and st["C5"]["st"] == "ns"
        # the front photo, turned into the viewer's frame: the fiducials are where the design has them
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/viewer/photo/front.jpg?v=1", timeout=60) as r:
            img = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR)
        x0, y0, x1, y1 = g["sides"]["front"]["rect"]
        k = img.shape[1] / (x1 - x0)
        assert abs(img.shape[0] / (y1 - y0) - k) < 0.01 * k
        pads = ph.find_pads(img)
        for ref in ("FIDT1", "FIDT4"):
            want = np.float32([(parts[ref]["x"] - x0) * k, (-parts[ref]["y"] - y0) * k])
            assert np.linalg.norm(pads - want, axis=1).min() < 3, ref
        assert "error" in _api(port, "api/viewer/photo/back.jpg")
    finally:
        srv.shutdown()


# ---- the models trained on this machine (models.py) -------------------------------------------

def test_models_learn_polarity_from_the_reference_and_answers():
    from aoimap import models as md
    from aoimap.check import Checks
    b = reference_board()
    c = Checks(b.data)
    c.start(b.project["uid"], "SN-M")
    c.photo("front", cv2.imencode(".png", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280), reversed_refs=("C3",)))[1].tobytes())
    c.answer("front", "C3", "wrong", "", False, "reversed")
    items = md.collect(b.data, b.name)
    froms = {(e["ref"], e["from"]) for e in items}
    assert ("C1", "reference") in froms and ("C3", "taught") in froms
    c3 = next(e for e in items if e["ref"] == "C3" and e["from"] == "taught")
    assert c3["right"] is False and c3["turn"] == 2 and c3["img"].shape == (md.CROP, md.CROP, 3)
    met = md.train(b.data, b.name)
    pol, miss = met["jobs"]["polarity"], met["jobs"]["missing"]
    assert pol["enough"] and pol["accuracy"] is not None and pol["own"] >= 1 and pol["auto"]["turned copies"] == pol["examples"]
    assert pol["features"] == "simple measures"       # no image network in a fresh data folder
    assert not miss["enough"] and miss["short"]       # no MISSING or NS answers yet
    assert (md.folder(b.data, b.name) / "polarity.npz").is_file()
    st = md.status(b.data, b.name)
    assert [j["job"] for j in st["jobs"]] == list(md.JOBS) and st["jobs"][1]["metrics"]["examples"] == pol["examples"]
    # an opinion, once a model is trusted enough to give one
    old = md.USE_ACCURACY
    md.USE_ACCURACY, md._LOADED = 0.0, {}
    try:
        s = b.sides["front"]
        g = next(p for p in b.design["parts"] if p["ref"] == "C1")
        op = md.opinion(b.data, b.name, s["img"], s["mapping"], g, b.design["rot_sign"], "tantalum")
        assert 0 <= op["polarity"]["p"] <= 1 and "missing" not in op and md.summary(op)
    finally:
        md.USE_ACCURACY, md._LOADED = old, {}


def test_bare_pads_look_empty():
    from aoimap import models as md
    img = np.full((md.CROP, md.CROP, 3), (60, 140, 40), np.uint8)
    cv2.rectangle(img, (20, 40), (45, 72), (200, 200, 200), -1)
    cv2.rectangle(img, (67, 40), (92, 72), (200, 200, 200), -1)
    assert md.looks_empty(img)
    cv2.rectangle(img, (30, 42), (82, 70), (25, 25, 25), -1)        # a part's body over the pads
    assert not md.looks_empty(img)


def test_models_api_trains_in_the_background():
    import time as _t
    b = reference_board()
    app, srv, port = _serve(b.data)
    try:
        r = _api(port, "api/models")
        assert [x["board"] for x in r["boards"]] == [b.name] and r["boards"][0]["jobs"][0]["metrics"] is None
        assert "error" in _api(port, "api/models/train", {"board": "nope"})
        _api(port, "api/models/train", {"board": b.name, "jobs": ["polarity"]})
        for _ in range(240):
            p = _api(port, "api/models")["boards"][0]["progress"]
            if p["state"] != "running":
                break
            _t.sleep(0.5)
        assert p["state"] == "done", p
        jobs = {j["job"]: j for j in _api(port, "api/models")["boards"][0]["jobs"]}
        assert jobs["polarity"]["metrics"]["examples"] > 0 and jobs["missing"]["metrics"] is None
    finally:
        srv.shutdown()


def test_every_part_outline_holds_its_pins_on_the_ptc_odb():
    """On the real PTC ODB++ (when this machine has it), every part's outline holds the centre of its
    own pins, on both sides: the SFP cages JT3/JT4 (whose package origin is off the body) included."""
    src = Path.home() / "aoimap-data" / "uploads" / "odb.tgz"
    if not src.is_file():
        print("skip test_every_part_outline_holds_its_pins_on_the_ptc_odb (no PTC ODB++ here)")
        return
    odb, _ = odblib.load(src)
    tol = 1e-4
    off = []
    for c in odb.parts:
        if not c.pins:
            continue
        cx = sum(p[0] for p in c.pins.values()) / len(c.pins)
        cy = sum(p[1] for p in c.pins.values()) / len(c.pins)
        g = {"x": c.x, "y": c.y, "rot": c.rot, "mirror": c.mirror, "turn": c.turn, "bbox": c.bbox}
        if not designlib._inside((cx, cy), designlib.outline_corners(g), tol):
            off.append(c.ref)
    assert not off, off
    assert {c.side for c in odb.parts} == {"top", "bot"}


def test_ics_and_transistors_keep_pin_1_whatever_the_description_says():
    """A level translator called BIDIR(ECTIONAL) is not a bidirectional TVS diode: a U or Q part is
    always polarised, while a real TVS stays out."""
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "bom.csv"
        f.write_text("Reference,Value,Description\n"
                     "UT38,TXS0104EPWR,IC TRANSLATOR BIDIR 14TSSOP\n"
                     "UT29,TXS0102DCUR,IC TRNSLTR BIDIRECTIONAL 8VSSOP\n"
                     "QB1,BSS138,MOSFET N-CH 50V 220MA SOT-23-3\n"
                     "DT1,SMCJ12CA,TVS DIODE 12VWM 19.9VC SMC\n"
                     "R1,10k,RES 10K 1% 0603\n")
        b = bomlib.read_bom(f)
        kind = {ln["refs"][0]: ln["kind"] for ln in b["lines"]}
        assert kind == {"UT38": "ic", "UT29": "ic", "QB1": "ic", "DT1": None, "R1": None}, kind


def test_reline_up_follows_lens_bending_and_undoes():
    # a photo bent like a lens: true pixel = homography + a barrel term the 4 corner anchors can't show
    k, W, H = 120.0, 2400, 1600
    def true_px(pts):
        q = np.float64(pts) * k + [200, 200]
        r = (q - [W / 2, H / 2]) / (W / 2)
        return q + 18 * r * (r ** 2).sum(1, keepdims=True) - 18 * r * 0.75
    parts = [{"ref": f"C{i}", "x": 0.5 + (i % 14) * 1.2, "y": 0.5 + (i // 14) * 1.15, "rot": 90.0 * (i % 2), "turn": 1,
              "mirror": False, "bbox": [-0.12, -0.06, 0.12, 0.06], "side": "top"} for i in range(14 * 10)]
    img = np.full((H, W, 3), (40, 140, 40), np.uint8)
    for p in parts:
        cv2.fillPoly(img, [np.round(true_px(designlib.outline_corners(p)) * 4).astype(np.int32)], (200, 200, 200), cv2.LINE_AA, shift=2)
    corners = [[0, 0], [16.67, 0], [16.67, 10], [0, 10]]
    base = ph.fit(corners, true_px(corners).tolist(), (1, 1))
    truth = true_px([[p["x"], p["y"]] for p in parts])
    err = lambda m: np.linalg.norm(m.apply([[p["x"], p["y"]] for p in parts]) - truth, axis=1)
    assert np.median(err(base)) > 3
    m, stats = ph.refine_lineup(img, base, parts, 1, corners)
    assert np.median(err(m)) < 1.0 and np.percentile(err(m), 90) < 2.0, (np.median(err(m)), stats)
    assert stats["after"]["median"] < stats["before"]["median"]
    back = ph.Mapping.from_dict(json.loads(json.dumps(m.to_dict())))
    assert np.allclose(back.apply([[3, 4]]), m.apply([[3, 4]]), atol=1e-3)
    assert m.coarse().fine is None and np.allclose(m.coarse().apply([[3, 4]]), base.apply([[3, 4]]))
    # too few parts to go by: refused, nothing changed
    try:
        ph.refine_lineup(img, base, parts[:10], 1, corners)
        assert False
    except ValueError as e:
        assert "at least 30" in str(e)


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
