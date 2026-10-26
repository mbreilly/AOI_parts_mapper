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
        pg.click("button[data-tab=parts]"); pg.wait_for_selector("#lines tbody tr")
        pg.screenshot(path=str(out / "2-parts.png"))

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
        print("front:", pg.inner_text("#align-result")[:300].replace("\n", " | "))
        print("rotation box shown:", pg.is_visible("#rot-box"))
        # back, then the closeup for DB1-3
        pg.click("button[data-tab=back]"); pg.wait_for_selector("#prompts > div", state="attached")
        pg.set_input_files("#photo-file", str(tmp / "back.jpg")); pg.wait_for_selector("#align-box:not([hidden])")
        click_points("back", Hb, ["FIDB2", "FIDB1", "FIDB3"], fids)
        pg.click("#do-align"); pg.wait_for_selector("#closeup-box:not([hidden])")
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / "4-back.png"))
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
        pg.click("button[data-tab=map]"); pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / "7-map.png"))
        # this board's serial, then the history
        pg.click("button[data-tab=files]")
        pg.fill("#proj-serial", "PTC-0042"); pg.press("#proj-serial", "Tab"); pg.wait_for_timeout(400)
        pg.click("button[data-tab=history]"); pg.wait_for_selector("#hist-detail .gallery img")
        pg.wait_for_function("[...document.querySelectorAll('#hist-detail .gallery img')].every(i => i.complete && i.naturalWidth > 0)")
        print("history:", pg.inner_text("#hist-status"), "|", pg.inner_text("#hist-list tbody")[:200].replace("\n", " | "))
        assert "PTC-0042" in pg.inner_text("#hist-detail h2")
        pg.screenshot(path=str(out / "8-history.png"), full_page=True)
        # another board of the same design, then back to the first through the history
        pg.click("#hist-detail [data-newfrom]"); pg.wait_for_selector("#tab-files:not([hidden]) #project-box:not([hidden])")
        pg.wait_for_timeout(500)
        assert pg.input_value("#proj-serial") == ""
        pg.screenshot(path=str(out / "9-new-board.png"))
        pg.click("button[data-tab=history]"); pg.wait_for_selector("#hist-list tr[data-uid]:nth-child(2)")
        pg.click("#hist-list tr[data-uid]:nth-child(2)"); pg.wait_for_selector("#hist-detail [data-reopen]")
        pg.click("#hist-detail [data-reopen]"); pg.wait_for_timeout(800)
        st = pg.evaluate("fetch('/api/state').then(r => r.json())")
        assert st["project"]["serial"] == "PTC-0042" and st["sides"]["back"]["aligned"], st["project"]
        print("reopened:", st["project"]["uid"], st["sides"]["back"]["summary"])
        br.close()
finally:
    srv.terminate()
print("page errors:", errors or "none")
