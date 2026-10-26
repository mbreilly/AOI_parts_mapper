"""A Canon EOS camera (or another camera gphoto2 can drive) on USB, as a camera for the page.

The browser can't see a DSLR as a webcam at full resolution, so the server drives it with gphoto2:
the page shows live view frames from preview() (about 1024 px wide) to line the board up with
the ghost, and capture() takes the full-resolution photo and returns the JPEG straight from the
camera's memory, without saving it on the card. Live view stops and the camera is let go after
a while with no frames asked for.

Needs the python gphoto2 package (pip install gphoto2). Set the camera to manual (M) and JPEG; turn
off its auto power off. For testing without a camera, set CANON_FAKE_IMAGE to an image file.

This file is shared, unchanged, by the AOI parts mapper and the PTC QC station.
"""

import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

try:
    import gphoto2 as gp
except ImportError:   # the rest of the app works without it
    gp = None

IDLE_SECONDS = 30       # live view stops after this long with no frames asked for
PREVIEW_WIDTH = 1024


class CameraError(Exception):
    pass


class _Fake:
    """Stands in for a camera: live view and photos of one image file."""
    name = "Canon EOS (stand-in)"

    def __init__(self, path: str):
        self.img = cv2.imread(path)
        if self.img is None:
            raise CameraError(f"CANON_FAKE_IMAGE {path} can't be read.")

    def preview(self) -> bytes:
        k = PREVIEW_WIDTH / self.img.shape[1]
        return cv2.imencode(".jpg", cv2.resize(self.img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA))[1].tobytes()

    def capture(self) -> bytes:
        time.sleep(0.5)
        return cv2.imencode(".jpg", self.img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()

    def close(self):
        pass


class Canon:
    def __init__(self):
        self._lock = threading.Lock()
        self._cam = None
        self._name = ""
        self._used = 0.0
        self._seen = (0.0, None)
        fake = os.environ.get("CANON_FAKE_IMAGE")
        self._fake = _Fake(fake) if fake else None
        threading.Thread(target=self._idle, name="canon-idle", daemon=True).start()

    # ---- finding the camera -------------------------------------------------------------------
    def detect(self) -> dict:
        """{"available", "name", "why"}: whether a camera is plugged in and switched on."""
        if self._fake:
            return {"available": True, "name": self._fake.name, "why": ""}
        if gp is None:
            return {"available": False, "name": "", "why": "The gphoto2 Python package isn't installed (pip install gphoto2)."}
        if self._cam is not None:
            return {"available": True, "name": self._name, "why": ""}
        t, got = self._seen
        if time.monotonic() - t < 3 and got is not None:
            return got
        try:
            cams = list(gp.Camera.autodetect())
        except gp.GPhoto2Error as e:
            cams, why = [], str(e)
        else:
            why = "No camera found on USB. Plug it in and switch it on."
        got = {"available": bool(cams), "name": cams[0][0] if cams else "", "why": "" if cams else why}
        self._seen = (time.monotonic(), got)
        return got

    def _open(self):
        if self._fake:
            return self._fake
        if gp is None:
            raise CameraError("The gphoto2 Python package isn't installed (pip install gphoto2).")
        if self._cam is None:
            cam = gp.Camera()
            try:
                cam.init()
            except gp.GPhoto2Error as e:
                raise CameraError(f"The camera didn't answer ({e}). Check it's plugged in, switched on and not asleep.")
            self._cam = cam
            self._name = (self.detect_name(cam) or "Camera")
            self._setup(cam)
        return self._cam

    @staticmethod
    def detect_name(cam) -> str:
        try:
            return cam.get_summary().text.split("Model:", 1)[1].split("\n", 1)[0].strip()
        except Exception:   # noqa: BLE001 - only a label
            return ""

    @staticmethod
    def _setup(cam):
        """Photos to the camera's memory (not the card), as JPEG."""
        try:
            cfg = cam.get_config()
            changed = False
            ok, target = gp.gp_widget_get_child_by_name(cfg, "capturetarget")
            if ok >= gp.GP_OK:
                choices = [target.get_choice(i) for i in range(target.count_choices())]
                ram = next((c for c in choices if "ram" in c.lower()), None)
                if ram and target.get_value() != ram:
                    target.set_value(ram); changed = True
            ok, fmt = gp.gp_widget_get_child_by_name(cfg, "imageformat")
            if ok >= gp.GP_OK and "jpeg" not in fmt.get_value().lower():
                choices = [fmt.get_choice(i) for i in range(fmt.count_choices())]
                best = next((c for c in choices if c.lower().startswith("large fine jpeg")), None) or \
                    next((c for c in choices if "jpeg" in c.lower() and "raw" not in c.lower()), None)
                if best:
                    fmt.set_value(best); changed = True
            if changed:
                cam.set_config(cfg)
        except gp.GPhoto2Error:
            pass   # older cameras without these settings still take photos

    def _drop(self):
        if self._cam is not None and not self._fake:
            try:
                self._cam.exit()
            except Exception:   # noqa: BLE001
                pass
        self._cam = None

    def _idle(self):
        while True:
            time.sleep(5)
            if self._cam is not None and time.monotonic() - self._used > IDLE_SECONDS:
                with self._lock:
                    if time.monotonic() - self._used > IDLE_SECONDS:
                        self._drop()

    # ---- frames and photos ----------------------------------------------------------------------
    def preview(self) -> bytes:
        """One live view frame (JPEG)."""
        with self._lock:
            self._used = time.monotonic()
            cam = self._open()
            if self._fake:
                return cam.preview()
            try:
                f = cam.capture_preview()
                return bytes(memoryview(f.get_data_and_size()))
            except gp.GPhoto2Error as e:
                self._drop()
                raise CameraError(f"Live view stopped ({e}). Check the camera is on and in a shooting mode.")

    def capture(self) -> bytes:
        """A full-resolution photo (JPEG)."""
        with self._lock:
            self._used = time.monotonic()
            cam = self._open()
            if self._fake:
                return cam.capture()
            try:
                path = cam.capture(gp.GP_CAPTURE_IMAGE)
                f = cam.file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
                data = bytes(memoryview(f.get_data_and_size()))
                try:
                    cam.file_delete(path.folder, path.name)
                except gp.GPhoto2Error:
                    pass
            except gp.GPhoto2Error as e:
                self._drop()
                raise CameraError(f"The camera couldn't take the photo ({e}). Check it's on, in M mode, and can focus.")
            finally:
                self._used = time.monotonic()
            if not path.name.lower().endswith((".jpg", ".jpeg")) or np.frombuffer(data[:2], np.uint8).tolist() != [0xFF, 0xD8]:
                raise CameraError("The camera sent a RAW file. Set its image quality to JPEG (L, fine).")
            return data


_shared = None


def shared() -> Canon:
    global _shared
    if _shared is None:
        _shared = Canon()
    return _shared


if __name__ == "__main__":   # python -m <package>.canon out.jpg : checks the camera works
    import sys
    c = shared()
    print(c.detect())
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "canon-test.jpg")
    out.with_name(out.stem + "-preview.jpg").write_bytes(c.preview())
    out.write_bytes(c.capture())
    print("wrote", out, "and", out.with_name(out.stem + "-preview.jpg"))
