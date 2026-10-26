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
    b2.open_board(b.name)
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
