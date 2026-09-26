"""Seek Nano thermal viewer (Windows) - repo ALeXXBody/seek-nano-desktop.

Controls in the window:
    s  = start stream / toggle pause
    c  = next colormap
    p  = PNG snapshot -> next to the exe
    q  = quit
"""
import os
import sys
import threading
import time

import cv2
import numpy as np
import usb.core
import usb.util

try:
    import libusb_package
    _BACKEND = libusb_package.get_libusb1_backend()
except Exception:
    _BACKEND = None

app_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
os.chdir(app_dir)

VID = 0x289D
PID = 0x11
REQ_OUT = 0x40
REQ_IN = 0xC0
BULK_EP = 0x81
FRAME_BYTES = 177_840
CHUNK = 6_840
W, H = 320, 240

LUTS = None  # filled below

def ironbow(t):
    # t in [0,1] -> (b,g,r) uint8
    t64 = np.clip(t, 0, 1)
    r = np.clip(132.0 * (np.exp(-0.4 * t64) - 0.3) * 8.5 / 255.0 * 255.0, 0, 255)
    g = np.clip(140.0 * (np.exp(-0.53 * t64 + 0.16) - 0.25) * 1.45, 0, 255)
    b = np.clip(80.0 * (np.exp(-0.6 * t64 + 0.24) - 0.2) * 1.3, 0, 255)
    y = (t64 * 210).astype(np.uint8)
    r = np.round(255.0 * np.minimum(1.0, np.maximum(0.0, 1.4 * t64 - 0.2)))
    g = np.round(255.0 * np.clip(0.95 * np.tanh(2.4 * (t64 - 0.53)) + 0.62, 0, 1))
    b = np.clip(np.round(255.0 * (0.61 + 0.35 * np.sin(5.9 * t64 - 1.2))), 0, 255)
    b = np.where(t64 > 0.72, 0.35 * 255.0, b)
    return np.stack([b.astype(np.uint8), g.astype(np.uint8), r.astype(np.uint8)], axis=-1)

def hot_lut(t):
    r = np.clip(t * 2.4, 0, 1) * 255
    g = np.clip(t * 1.7 - 0.5, 0, 1) * 255
    b = np.clip(t - 0.65, 0, 1) * 255
    return np.stack([b, g, r], axis=-1).astype(np.uint8)

def gray_lut(t):
    v = (t * 255).astype(np.uint8)
    return np.stack([v, v, v], axis=-1)

class Stream(threading.Thread):
    def __init__(self, put):
        super().__init__(daemon=True)
        self.put = put
        self.stop_flag = threading.Event()
        self.daemon = True

    def ctrl_out(self, request, payload):
        self.dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)

    def ctrl_in(self, request, length):
        return bytes(self.dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))

    def set_page(self, mode, addr):
        self.ctrl_out(0x56, bytes([mode, 0]) + addr.to_bytes(2, "little") + b"\x00\x00")

    def run(self):
        try:
            dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_BACKEND)
            if dev is None:
                raise RuntimeError("FAILED TO FIND DEVICE: Seek Nano (vid 0x289d pid 0xd) 0x11). "
                    "If Windows Device Manager shows 'Seek Thermal' with an exclamation mark,\n"
                    "install the libusb-win32 driver ONCE using Zadig (README included).")
            dev.set_configuration()
            usb.util.claim_interface(dev, 0)
            self.dev = dev

            # replicate handshake from Seek's own Android app (Phase 1 + Phase 2)
            self.ctrl_out(0x54, b"\x00\x00")
            self.ctrl_out(0x3c, b"\x00\x00")
            self.ctrl_in(0x3d, 2)
            self.ctrl_out(0x3e, b"\x08\x00")
            self.put(("info", "connected - Start streaming (press s)"))
            # streaming Phase 4
            self.ctrl_out(0x37, b"\xfc\x00\x04\x00")
            self.ctrl_out(0x3c, b"\x01\x00")
            self.ctrl_in(0x3d, 2)

            while not self.stop_flag.is_set():
                self.ctrl_out(0x53, b"\x58\x5b\x01\x00")
                rc = self.ctrl_in(0x35, 4)
                if rc == b"\x00\x00\x00\x00":
                    time.sleep(0.001)
                    continue
                buf = bytearray()
                while len(buf) < FRAME_BYTES and not self.stop_flag.is_set():
                    buf.extend(dev.read(BULK_EP, CHUNK, 1250))
                if len(buf) == FRAME_BYTES:
                    self.put(("frame", bytes(buf)))
        except Exception as e:
            self.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass

class Viewer:
    COLORMAPS = [
        ("ironbow", ironbow),
        ("hot", hot_lut),
        ("grayscale", gray_lut),
    ]
    def __init__(self):
        self.q = []
        self.lut_i = 0
        self.stream = None
        self.paused = False
        self.frame = None
        self.msg = "connect Seek Nano, then press s"
        cv2.namedWindow("Seek Nano Viewer", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Seek Nano Viewer", 400, 380)

    def put(self, msg):
        if self.q:
            self.q.pop()
        self.q.append(msg)

    def toggle_stream(self):
        if self.stream and self.stream.is_alive():
            self.stream.stop_flag.set()
            self.stream = None
            self.msg = "stopped"
            return
        self.msg = "starting ..."
        self.stream = Stream(self.put)
        self.stream.start()

    def show(self):
        while self.q:
            kind, payload = self.q.pop(0)
            if kind == "frame":
                self.frame = payload
            elif kind == "info":
                self.msg = payload
            elif kind == "error":
                self.msg = f"ERROR: {payload}"
                self.stream = None
        if self.frame is None or self.paused:
            pass
        else:
            self.draw()

    def draw(self):
        arr = np.frombuffer(self.frame, dtype="<u2")
        samples = arr[3:]
        need = W * H
        img_arr = samples[:need].reshape(H, W)
        t = (img_arr.astype(np.float32) - img_arr.min()) / max(1, img_arr.max() - img_arr.min())
        rgb = self.COLORMAPS[self.lut_i][1](t)
        big = cv2.resize(rgb, (W * 2, H * 2), interpolation=cv2.INTER_NEAREST)
        img = big.copy()
        cv2.putText(img, self.msg, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 230, 255), 1)
        cv2.putText(img, f"{self.COLORMAPS[self.lut_i][0]}  (c)", (8, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 255, 200), 1)
        cv2.imshow("Seek Nano Viewer", img)

    def save_png(self):
        if self.frame is None:
            self.msg = "no frame yet"
            return
        arr = np.frombuffer(self.frame, dtype="<u2")
        samples = arr[3:]
        img_arr = samples[:W*H].reshape(H, W)
        t = (img_arr - img_arr.min()) / max(1, img_arr.max() - img_arr.min())
        rgb = self.COLORMAPS[self.lut_i][1](t)
        name = f"capture_{int(time.time())}.png"
        cv2.imwrite(name, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        self.msg = f"saved {name}"

    def run(self):
        while True:
            self.show()
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q') or key == 27:
                break
            elif key == ord('s'):
                self.toggle_stream()
            elif key == ord('c'):
                self.lut_i = (self.lut_i + 1) % len(self.COLORMAPS)
            elif key == ord('p'):
                self.save_png()
            elif key == ord(' '):
                self.paused = not self.paused
        if self.stream:
            self.stream.stop_flag.set()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    Viewer().run()
