"""Drives the web page in headless Chromium on the made-up board: load files, line up the front with
4 fiducials, the back with 3 and a closeup, and save screenshots. Not part of the unit tests.

    python tests/e2e_page.py <out folder>"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import fixture as fx
from playwright.sync_api import sync_playwright

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="aoimap-e2e-"))
odb, bom = fx.make_odb(tmp), fx.make_bom(tmp)
Hf, Hb = fx.camera("front"), fx.camera("back", ppi=250)
cv2.imwrite(str(tmp / "front.jpg"), fx.render("front", Hf), [cv2.IMWRITE_JPEG_QUALITY, 95])
cv2.imwrite(str(tmp / "back.jpg"), fx.render("back", Hb), [cv2.IMWRITE_JPEG_QUALITY, 95])

port = 18093
srv = subprocess.Popen([sys.executable, "-m", "aoimap", "--port", str(port), "--data", str(tmp / "data"), "--no-browser",
                        *(["--db", os.environ["AOIMAP_TEST_DB"]] if os.environ.get("AOIMAP_TEST_DB") else ["--no-db"])],
                       cwd=HERE.parent)
time.sleep(1.5)
errors = []
# the page fills the window: the body never scrolls, and each tab has at most one box that scrolls
SCROLLERS = """() => {
  const res = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === 'hidden') continue;
    if ((/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 1) ||
        (/(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1)) res.push(el.id || el.className || el.tagName);
  }
  const d = document.scrollingElement;
  return { body: d.scrollHeight > d.clientHeight + 1 || d.scrollWidth > d.clientWidth + 1, boxes: res };
}"""


def one_scroller(pg, where):
    r = pg.evaluate(SCROLLERS)
    assert not r["body"], f"{where}: the page itself scrolls"
    assert len(r["boxes"]) <= 1, f"{where}: more than one scrollbar: {r['boxes']}"


try:
    with sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium"
        br = p.chromium.launch(**({"executable_path": exe} if Path(exe).exists() else {}))
        pg = br.new_page(viewport={"width": 1500, "height": 950})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(f"http://localhost:{port}/")
        pg.set_input_files("#file-odb", str(odb)); pg.wait_for_selector("#drop-odb.done")
        pg.set_input_files("#file-bom", str(bom)); pg.wait_for_selector("#design-box:not([hidden])")
        pg.screenshot(path=str(out / "1-files.png"))
        one_scroller(pg, "files")
        pg.click("button[data-tab=parts]"); pg.wait_for_selector("#lines tbody tr")
        pg.screenshot(path=str(out / "2-parts.png"))
        one_scroller(pg, "parts")

        def click_points(side, Hm, refs, design_pts):
            img = pg.locator("#photo")
            img.wait_for(state="visible")
            pg.wait_for_function("document.querySelector('#photo').complete && document.querySelector('#photo').naturalWidth > 0")
            box = img.bounding_box()
            nat = pg.evaluate("document.querySelector('#photo').naturalWidth")
            k = box["width"] / nat
            for r in refs:
                x, y = fx.to_px(Hm, [design_pts[r]])[0]
                pg.mouse.click(box["x"] + x * k, box["y"] + y * k)
                pg.wait_for_timeout(150)

        fids = {r: [x, y] for r, s, x, y, *_ in fx.PARTS if r.startswith("FID")}
        # front
        pg.click("button[data-tab=front]"); pg.wait_for_selector("#prompts > div", state="attached")
        pg.set_input_files("#photo-file", str(tmp / "front.jpg")); pg.wait_for_selector("#align-box:not([hidden])")
        click_points("front", Hf, ["FIDT1", "FIDT2", "FIDT4", "FIDT3"], fids)
        pg.click("#do-align"); pg.wait_for_selector("#parts-box:not([hidden])")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(out / "3-front.png"))
        one_scroller(pg, "front")
        # select a part: the panel shows it, GOOD marks it on the list and the photo, UNDO takes it back
        pg.click("#side-parts div[data-ref='C3']"); pg.wait_for_selector("#sel-part .ref")
        assert pg.inner_text("#sel-part .ref") == "C3"
        assert pg.eval_on_selector("#parts-svg polygon[data-ref='C3']", "p => p.classList.contains('sel')")
        pg.click("#sel-part [data-a=good]"); pg.wait_for_selector("#side-parts div[data-ref='C3'] .chip.st-good")
        assert "GOOD" in pg.inner_text("#sel-part .head")
        pg.click("#sel-part [data-a=undo]"); pg.wait_for_selector("#side-parts div[data-ref='C3'] .chip.st-in")
        pg.keyboard.press("ArrowDown"); pg.wait_for_timeout(300)
        assert pg.inner_text("#sel-part .ref") != "C3"
        one_scroller(pg, "front with a part selected")
        pg.keyboard.press("Escape"); pg.wait_for_timeout(200)
        print("front:", pg.inner_text("#align-result")[:300].replace("\n", " | "))
        print("rotation box shown:", pg.is_visible("#rot-box"))
        # back, then the closeup for DB1-3
        pg.click("button[data-tab=back]"); pg.wait_for_selector("#prompts > div", state="attached")
        pg.set_input_files("#photo-file", str(tmp / "back.jpg")); pg.wait_for_selector("#align-box:not([hidden])")
        click_points("back", Hb, ["FIDB2", "FIDB1", "FIDB3"], fids)
        pg.click("#do-align"); pg.wait_for_selector("#closeup-box:not([hidden])")
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / "4-back.png"))
        one_scroller(pg, "back")
        tiles = pg.evaluate("fetch('/api/state').then(r => r.json()).then(s => s.sides.back.tiles)")
        t = next(t for t in tiles if "DB1" in t["refs"])
        Hc = fx.camera("back", region=t["region"])
        cv2.imwrite(str(tmp / "closeup.jpg"), fx.render("back", Hc), [cv2.IMWRITE_JPEG_QUALITY, 95])
        pg.click(f"button[data-tile='{t['index']}']")
        pg.set_input_files("#photo-file", str(tmp / "closeup.jpg"))
        pg.wait_for_function("document.querySelector('#photo').src.includes('closeup')")
        centres = {r: [x, y] for r, s, x, y, *_ in fx.PARTS}
        click_points("back", Hc, [a["ref"] for a in t["anchors"]], {**centres, **fids})
        pg.click("#do-align"); pg.wait_for_timeout(900)
        pg.screenshot(path=str(out / "5-closeup.png"))
        pg.click("#back-full"); pg.wait_for_timeout(600)
        pg.screenshot(path=str(out / "6-back-after.png"))
        st = pg.evaluate("fetch('/api/state').then(r => r.json())")
        print("back summary:", st["sides"]["back"]["summary"])
        # tick the resistors' BOM line: in the check, with its status; expand it and look at a part
        pg.click("button[data-tab=parts]"); pg.wait_for_selector("#lines tbody tr")
        row = "#lines tr[data-key='10K']"
        assert not pg.is_checked(f"{row} [data-tick]") and "not ticked" in pg.inner_text(f"{row} .lstat")
        pg.check(f"{row} [data-tick]"); pg.wait_for_function("document.querySelector(\"#lines tr[data-key='10K'] .lstat\").innerText.includes('FRONT')")
        print("10k line:", pg.inner_text(f"{row} .lstat"))
        assert pg.eval_on_selector(f"{row} [data-kind]", "s => s.value") == "look"
        pg.click(f"{row} .lstat"); pg.wait_for_selector("#lines tr.sub [data-look='R1']")
        pg.screenshot(path=str(out / "6b-parts-ticked.png"))
        pg.click("#lines tr.sub [data-look='R1']"); pg.wait_for_selector("#tab-side:not([hidden])")
        pg.wait_for_timeout(600)
        assert pg.inner_text("#sel-part .ref") == "R1"
        pg.screenshot(path=str(out / "6c-look-R1.png"))
        pg.click("button[data-tab=map]"); pg.wait_for_timeout(1500)
        pg.screenshot(path=str(out / "7-map.png"))
        one_scroller(pg, "map")
        # the board viewer: find a net, click a part's pin, flip to the back
        pg.fill("#bv-q", "SDA"); pg.press("#bv-q", "Enter"); pg.wait_for_selector("#bv-info h3")
        assert "2 pins on 2 parts" in pg.inner_text("#bv-info"), pg.inner_text("#bv-info")
        pg.fill("#bv-q", "U1"); pg.press("#bv-q", "Enter"); pg.wait_for_timeout(500)
        assert "PINS (3)" in pg.inner_text("#bv-info") and "SDA" in pg.inner_text("#bv-info")
        pg.click("#bv-info [data-bvnet]"); pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / "7b-viewer-net.png"))
        pg.click("#tab-map h2"); pg.keyboard.press("f"); pg.wait_for_timeout(300)
        assert pg.inner_text("#bv-side") == "BACK"
        pg.screenshot(path=str(out / "7c-viewer-back.png"))
        # this board's serial, then the history
        pg.click("button[data-tab=files]")
        pg.fill("#proj-serial", "PTC-0042"); pg.press("#proj-serial", "Tab"); pg.wait_for_timeout(400)
        pg.click("button[data-tab=history]"); pg.wait_for_selector("#hist-detail .gallery img")
        pg.wait_for_function("[...document.querySelectorAll('#hist-detail .gallery img')].every(i => i.complete && i.naturalWidth > 0)")
        print("history:", pg.inner_text("#hist-status"), "|", pg.inner_text("#hist-list tbody")[:200].replace("\n", " | "))
        assert "PTC-0042" in pg.inner_text("#hist-detail h2")
        pg.screenshot(path=str(out / "8-history.png"))
        one_scroller(pg, "history")
        # another board of the same design, then back to the first through the history
        pg.click("#hist-detail [data-newfrom]"); pg.wait_for_selector("#tab-files:not([hidden]) #project-box:not([hidden])")
        pg.wait_for_timeout(500)
        assert pg.input_value("#proj-serial") == ""
        pg.screenshot(path=str(out / "9-new-board.png"))
        pg.click("button[data-tab=history]"); pg.wait_for_function("document.querySelectorAll('#hist-list tr[data-uid]').length >= 2")
        pg.locator("#hist-list tr[data-uid]").nth(1).click(); pg.wait_for_selector("#hist-detail [data-reopen]")
        pg.click("#hist-detail [data-reopen]"); pg.wait_for_timeout(800)
        st = pg.evaluate("fetch('/api/state').then(r => r.json())")
        assert st["project"]["serial"] == "PTC-0042" and st["sides"]["back"]["aligned"], st["project"]
        print("reopened:", st["project"]["uid"], st["sides"]["back"]["summary"])
        # make it the reference, then check another board with C3 and C7 the wrong way round
        cv2.imwrite(str(tmp / "other_front.jpg"), fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280), reversed_refs=("C3", "C7")),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        pg.click("button[data-tab=map]"); pg.click("#map-ref"); pg.wait_for_timeout(500)
        assert "✓" in pg.inner_text("#map-ref")
        pg.click("button[data-tab=check]"); pg.wait_for_selector("#ck-ref option", state="attached")
        pg.fill("#ck-serial", "REPAIR-17"); pg.click("#ck-go"); pg.wait_for_selector("#ck-run:not([hidden])")
        pg.set_input_files("#ck-file", str(tmp / "other_front.jpg"))
        pg.wait_for_selector("#ck-flagged .flag")
        pg.wait_for_function("[...document.querySelectorAll('#ck-flagged img, #ck-photo')].every(i => i.complete && i.naturalWidth > 0)")
        print("check:", pg.inner_text("#ck-sub"), "|", pg.inner_text("#ck-msg"))
        pg.screenshot(path=str(out / "10-check.png"))
        one_scroller(pg, "check")
        for ref in pg.eval_on_selector_all("#ck-flagged [data-ans=correct]", "bs => bs.map(b => b.dataset.ref)"):
            pg.click(f"#ck-flagged [data-ans={'wrong' if ref in ('C3', 'C7') else 'correct'}][data-ref='{ref}']")
            pg.wait_for_timeout(300)
        assert pg.inner_text("#ck-result") == "FAIL", pg.inner_text("#ck-result")
        print("check result:", pg.inner_text("#ck-result"), "|", pg.inner_text("#ck-sub"))
        pg.screenshot(path=str(out / "11-check-answered.png"))
        pg.click("#ck-run details.more summary"); pg.click("[data-goto=history][data-hist=checks]"); pg.wait_for_selector("#hist-detail .badge.FAIL")
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / "12-history-checks.png"))
        pg.click("button[data-tab=models]"); pg.wait_for_timeout(800)
        one_scroller(pg, "models")

        # ---- an answer on the reference is looked at on every board of the design --------------
        import json as _json, urllib.request as _rq

        def call(path, body=None, raw=None):
            data = raw if raw is not None else (_json.dumps(body).encode() if body is not None else None)
            req = _rq.Request(f"http://localhost:{port}/{path}", data=data, method="POST" if data is not None else "GET",
                              headers={} if raw is not None else {"Content-Type": "application/json"})
            with _rq.urlopen(req, timeout=120) as r:
                return _json.loads(r.read())

        ref_uid = call("api/check/state")["references"][0]["uid"]
        # another mapped board of the design (PTC 001, say), both sides lined up, set up through the API
        call(f"api/history/{ref_uid}/new", {})
        H2f, H2b = fx.camera("front", tilt_deg=-2, ppi=290), fx.camera("back", ppi=250, tilt_deg=1)
        for side, Hm, refs in (("front", H2f, ["FIDT1", "FIDT2", "FIDT4", "FIDT3"]), ("back", H2b, ["FIDB2", "FIDB1", "FIDB3"])):
            call(f"api/side/{side}/photo", raw=cv2.imencode(".jpg", fx.render(side, Hm, reversed_refs=("C5",)))[1].tobytes())
            pts = {r: [float(v) for v in fx.to_px(Hm, [fids[r]])[0]] for r in refs}
            st2 = call(f"api/side/{side}/align", {"points": pts})
        call("api/history/" + st2["project"]["uid"] + "/info", {"serial": "PTC 001"})
        other_uid = st2["project"]["uid"]
        call(f"api/history/{ref_uid}/open", {})
        pg.reload(); pg.wait_for_selector("#tab-files:not([hidden])")
        # the reference, FRONT: D7 (a TVS diode, not in the check) clicked on the photo and answered GOOD
        pg.click("button[data-tab=front]"); pg.wait_for_selector("#side-parts div[data-ref='D7']")
        pg.wait_for_timeout(500)
        pg.dispatch_event("#parts-svg polygon[data-ref='D7']", "click"); pg.wait_for_selector("#sel-part .ref")
        assert pg.inner_text("#sel-part .ref") == "D7" and "NOT CHECKED" in pg.inner_text("#sel-part .head")
        assert "checked on every" in pg.inner_text("#sel-part .carry"), pg.inner_text("#sel-part .carry")
        assert "NS ON EVERY BOARD" in pg.inner_text("#sel-part [data-a=nsall]")      # on the reference NS is for every board
        pg.click("#sel-part [data-a=good]"); pg.wait_for_selector("#side-parts div[data-ref='D7'] .chip.st-good")
        assert "now be checked on every" in pg.inner_text("#msg"), pg.inner_text("#msg")
        assert "This part is checked on every" in pg.inner_text("#sel-part .carry")
        pg.screenshot(path=str(out / "13-reference-answer.png"))
        # PTC 001, opened from Files: D7 is in its check, outlined in its status colour, new, and counted
        pg.click("button[data-tab=files]"); pg.wait_for_selector(f"#boards [data-reopen='{other_uid}']")
        pg.click(f"#boards [data-reopen='{other_uid}']"); pg.wait_for_function("document.querySelector('#msg').innerText.includes('New in the check')")
        pg.click("button[data-tab=front]"); pg.wait_for_selector("#side-parts div[data-ref='D7'] .newtag")
        assert pg.eval_on_selector("#side-parts div[data-ref='D7'] .chip", "c => c.classList.contains('st-in')")
        assert pg.eval_on_selector("#parts-svg polygon[data-ref='D7']", "p => p.classList.contains('st-in')")
        st = pg.evaluate("fetch('/api/viewer/status').then(r => r.json())")["parts"]
        assert st["D7"]["st"] == "checked" and st["D7"]["new"] and st["D7"]["verdict"] == "correct", st["D7"]
        n_in = int(pg.inner_text("#side-legend [data-stat=check] b"))
        assert n_in == sum(1 for r, s in st.items() if s["st"] != "off" and r in pg.eval_on_selector_all("#side-parts div[data-ref]", "ds => ds.map(d => d.dataset.ref)"))
        if not pg.is_visible("#sel-part .ref") or pg.inner_text("#sel-part .ref") != "D7":      # still chosen from the reference
            pg.dispatch_event("#parts-svg polygon[data-ref='D7']", "click")
        pg.wait_for_selector("#sel-part .newtag")
        pg.wait_for_function("document.querySelector('#sel-part .opinions').innerText.includes('LOOKS RIGHT')")
        pg.screenshot(path=str(out / "14-other-board-new-part.png"))
        one_scroller(pg, "front, new part")
        # and on Check a board: a new check of the front covers D7
        call("api/check/start", {"reference": ref_uid, "serial": "REPAIR-18"})
        res = call("api/check/side/front/photo", raw=cv2.imencode(".jpg", fx.render("front", fx.camera("front", tilt_deg=-3, ppi=280)))[1].tobytes())
        d7 = next(r for r in res["check"]["sides"]["front"]["results"] if r["ref"] == "D7")
        assert d7["status"] == "ok", d7

        # ---- COVERED: a heatsink on C8 of the reference only; every other board still checks C8 -------
        call(f"api/history/{ref_uid}/open", {})
        pg.reload(); pg.wait_for_selector("#tab-files:not([hidden])")
        pg.click("button[data-tab=front]"); pg.wait_for_selector("#side-parts div[data-ref='C8']")
        n_cant = int(pg.inner_text("#side-legend [data-stat=cant] b"))
        pg.click("#side-parts div[data-ref='C8']"); pg.wait_for_selector("#sel-part [data-a=covered]")
        pg.wait_for_function("document.querySelector('#sel-part .opinions').innerText.includes('REFERENCE')")
        pg.click("#sel-part [data-a=covered]"); pg.wait_for_selector("#side-parts div[data-ref='C8'] .chip.st-cant")
        assert "covered: heatsink" in pg.inner_text("#side-parts div[data-ref='C8']")
        pg.wait_for_selector("#sel-part [data-cover=label]")
        assert "COVERED: HEATSINK" in pg.inner_text("#sel-part [data-a=covered]")
        assert "other" in pg.inner_text("#sel-part .carry") and "still check" in pg.inner_text("#sel-part .carry"), pg.inner_text("#sel-part .carry")
        assert int(pg.inner_text("#side-legend [data-stat=cant] b")) == n_cant + 1
        assert pg.eval_on_selector("#parts-svg polygon[data-ref='C8']", "p => p.classList.contains('st-cant')")
        pg.click("#sel-part [data-cover=label]"); pg.wait_for_function("document.querySelector('#sel-part .head').innerText.includes('covered: label')")
        pg.screenshot(path=str(out / "14b-covered-on-reference.png"))
        one_scroller(pg, "front, covered part")
        pg.click("#sel-part [data-a=covered]"); pg.wait_for_selector("#side-parts div[data-ref='C8'] .chip.st-in")    # pressed again: undone
        pg.keyboard.press("c"); pg.wait_for_selector("#side-parts div[data-ref='C8'] .chip.st-cant")                   # key C
        info = call(f"api/part/info?src=project/{other_uid}/front&ref=C8")
        assert not info["covered"] and info["reference_covered"] == "heatsink" and info["verdict"] != "covered", info
        # Check a board: the open check compares C8 again; it looks like the covered look: one click confirms
        pg.click("button[data-tab=check]"); pg.wait_for_selector("#ck-flagged [data-cov][data-ref='C8']")
        assert "LOOKS COVERED" in pg.inner_text("#ck-flagged"), pg.inner_text("#ck-flagged")
        n_cant = int(pg.inner_text("#ck-legend .st-cant b"))
        pg.click("#ck-flagged button.hint[data-ref='C8']"); pg.wait_for_selector("#ck-ok [data-uncov='C8']")
        assert int(pg.inner_text("#ck-legend .st-cant b")) == n_cant + 1 and "covered: heatsink" in pg.inner_text("#ck-ok")
        pg.screenshot(path=str(out / "14c-covered-on-check.png"))
        one_scroller(pg, "check, covered part")
        pg.click("#ck-ok [data-uncov='C8']"); pg.wait_for_selector("#ck-flagged [data-cov][data-ref='C8']")
        pg.click("#ck-flagged button.a-covered:not(.hint)[data-ref='C8']"); pg.wait_for_selector("#ck-ok [data-uncov='C8']")
        chk = call("api/check/state")["check"]
        assert next(r for r in chk["sides"]["front"]["results"] if r["ref"] == "C8")["status"] == "covered"
        # History: the check lists C8 covered
        pg.click("#ck-run details.more summary"); pg.click("[data-goto=history][data-hist=checks]")
        pg.wait_for_selector(f"#hist-list tr[data-uid='{chk['uid']}']"); pg.click(f"#hist-list tr[data-uid='{chk['uid']}']")
        pg.wait_for_function("document.querySelector('#hist-detail').innerText.includes('COVERED ON THE FRONT')")
        call(f"api/history/{other_uid}/open", {})
        pg.reload(); pg.wait_for_selector("#tab-files:not([hidden])")

        # ---- making PTC 001 the reference: review each difference first ---------------------------
        pg.click("button[data-tab=map]"); pg.wait_for_selector("#map-ref")
        pg.click("#map-ref"); pg.wait_for_selector("#tab-review:not([hidden])")
        pg.wait_for_function("!document.querySelector('#rv-left').innerText.startsWith('COMPARING')", timeout=180000)
        print("review:", pg.inner_text("#rv-sub"))
        assert call("api/check/state")["references"][0]["uid"] == ref_uid      # the old one stays until finished
        pg.wait_for_function("[...document.querySelectorAll('#rv-rows img')].every(i => i.complete && i.naturalWidth > 0)")
        pg.screenshot(path=str(out / "15-review.png"))
        one_scroller(pg, "review")
        assert pg.is_visible("#rv-rows [data-rv=wrong][data-key='front/C5']"), pg.inner_text("#rv-rows")    # C5 is reversed on PTC 001
        pg.click("#rv-rows [data-rv=wrong][data-key='front/C5']"); pg.wait_for_timeout(400)
        if pg.is_enabled("#rv-approve"):
            pg.click("#rv-approve"); pg.wait_for_timeout(400)
        for _ in range(200):
            b = pg.query_selector("#rv-rows [data-rv=accept]:not(.on)")
            if not b:
                break
            b.click(); pg.wait_for_timeout(250)
        pg.wait_for_function("!document.querySelector('#rv-finish').disabled")
        pg.click("#rv-finish"); pg.wait_for_function("document.querySelector('#rv-left').innerText === 'DONE'")
        refs = call("api/check/state")["references"]
        assert [r["uid"] for r in refs] == [other_uid], refs
        pg.screenshot(path=str(out / "16-review-done.png"))
        pg.click("button[data-tab=history]"); pg.wait_for_selector("#hist-detail [data-review]")
        br.close()
finally:
    srv.terminate()
print("page errors:", errors or "none")
