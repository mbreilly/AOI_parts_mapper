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
    assert c.learned_boards()[0]["front"] == {"good": 0, "bad": 0, "reference_wrong": 0}
    res = run("SN-5", "C3")
    assert res["D8"]["status"] == "check" and res["C3"]["status"] == "reversed" and res["C5"]["status"] == "ok"
    assert [x["board"] for x in c.learned_boards()] == [r["board"] for r in references(b.data)]


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
    assert re.search(r'<table id="lines"><thead></thead>', raw)
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
