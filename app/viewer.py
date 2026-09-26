"""Seek Nano thermal viewer (Windows) - proper GUI.

Layout:
  top     : buttons  (Start/Stop, Colormap, Save PNG, Dump raw frame, Copy log, Clear log)
  left    : video panel (2x zoom, status text bottom)
  right   : read-only, selectable log (Ctrl+C works) ; also duplicated in seeknano_verbose.log
"""
import os
import queue
import sys
import threading
import time

import numpy as np
import wx

app_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
os.chdir(app_dir)

import usb.core
import usb.util

try:
    import libusb_package
    _BACKEND = libusb_package.get_libusb1_backend()
except Exception:
    _BACKEND = None

VID = 0x289D
PID = 0x11
REQ_OUT = 0x40
REQ_IN = 0xC0
BULK_EP = 0x81
FRAME_BYTES = 177_840
CHUNK = 6_840
W, H = 320, 240
VW, VH = W * 2, H * 2


def ironbow(t):
    r = np.clip(255.0 * (1.4 * t - 0.2), 0, 255)
    g = np.clip(255.0 * (0.62 + 2.4 * (t - 0.53)), 0, 255)
    g = np.where(t > 0.72, 255 * np.clip(1.35 - 0.9 * (t - 0.72) / 0.28, 0.35, 1.0), g)
    b = np.clip(255.0 * (0.61 + 0.35 * np.sin(5.9 * t - 1.2)), 0, 255)
    b = np.where(t > 0.72, 0.35 * 255.0, b)
    return np.stack([np.clip(b, 0, 255), np.clip(g, 0, 255), np.clip(r, 0, 255)],
                    axis=-1).astype(np.uint8)


def hot_lut(t):
    r = np.clip(t * 2.4, 0, 1) * 255
    g = np.clip(t * 1.7 - 0.5, 0, 1) * 255
    b = np.clip(t - 0.65, 0, 1) * 255
    return np.stack([b, g, r], axis=-1).astype(np.uint8)


def gray_lut(t):
    v = (t * 255).astype(np.uint8)
    return np.stack([v, v, v], axis=-1).astype(np.uint8)


COLORMAPS = [("ironbow", ironbow), ("hot", hot_lut), ("grayscale", gray_lut)]


class Stream(threading.Thread):
    def __init__(self, q, status_cb):
        super().__init__(daemon=True)
        self.q = q
        self.status_cb = status_cb
        self.stop_flag = threading.Event()
        try:
            self.logf = open("seeknano_verbose.log", "a", buffering=1)
        except Exception:
            self.logf = None

    def trace(self, line):
        if self.logf:
            self.logf.write("%.3f %s\n" % (time.time(), line))
        self.q.put(("log", line))

    def ctrl_out(self, request, payload):
        self.trace("W req=0x%02x data=%s" % (request, payload.hex()))
        self.dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
        self.trace("W req=0x%02x OK" % request)

    def ctrl_in(self, request, length):
        r = bytes(self.dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))
        self.trace("R req=0x%02x len=%d -> %s" % (request, length, r.hex()))
        return r

    def status(self, text):
        self.q.put(("status", text))

    def run(self):
        try:
            dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_BACKEND)
            if dev is None:
                raise RuntimeError(
                    "Seek Nano (vid 0x289d pid 0x11) not found.\n"
                    "If Windows Device Manager shows 'Seek Thermal' with an exclamation\n"
                    "mark, install the libusbK driver once with Zadig (see README).")
            dev.set_configuration()
            usb.util.claim_interface(dev, 0)
            self.dev = dev
            for i in dev:
                self.trace("iface %s eps=%s" % (
                    i,
                    [(hex(e.bEndpointAddress), e.type, e.maxPacketSize) for e in i]))
            self.status_cb("device opened")

            # Phase 1 - handshake (exact replay of phone session)
            self.ctrl_out(0x54, b"\x00\x00")
            self.ctrl_out(0x3c, b"\x00\x00")
            self.ctrl_in(0x3d, 2)
            self.ctrl_out(0x3e, b"\x08\x00")
            self.status("connected - protocol handshake ok")

            # Phase 4 - enable streaming
            self.ctrl_out(0x37, b"\xfc\x00\x04\x00")
            self.ctrl_out(0x3c, b"\x01\x00")
            self.ctrl_in(0x3d, 2)
            self.status("streaming mode enabled - pumping frames")

            kicks = 0
            while not self.stop_flag.is_set():
                self.ctrl_out(0x53, b"\x58\x5b\x01\x00")
                rc = self.ctrl_in(0x35, 4)
                kicks += 1
                if rc != b"\x00\x00\x00\x00":
                    self.trace("kick %d status %s" % (kicks, rc.hex()))
                if kicks < 70:
                    time.sleep(0.002)
                    continue
                try:
                    buf = bytearray()
                    while len(buf) < FRAME_BYTES and not self.stop_flag.is_set():
                        buf.extend(dev.read(BULK_EP, CHUNK, 1250))
                except usb.core.USBError as ue:
                    self.trace("BULK FAIL %s: %s" % (type(ue).__name__, ue))
                    continue
                if len(buf) == FRAME_BYTES:
                    self.q.put(("frame", bytes(buf)))
                else:
                    self.trace("short frame %d B" % len(buf))
        except Exception as e:
            self.trace("THREAD ERR %s: %s" % (type(e).__name__, e))
            self.q.put(("error", "%s: %s" % (type(e).__name__, e)))
        finally:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass


class Viewer(wx.Frame):
    def __init__(self):
        wx.Frame.__init__(self, None, title="Seek Nano Viewer", size=(1000, 660))
        self.q = queue.Queue()
        self.stream_thread = None
        self.frame_raw = None
        self.lut_i = 0
        self.paused = False

        panel = wx.Panel(self)
        top = wx.BoxSizer(wx.HORIZONTAL)

        self.start_btn = wx.Button(panel, label="Start stream")
        self.start_btn.Bind(wx.EVT_BUTTON, self.on_toggle)
        self.cmap_btn = wx.Button(panel, label="colormap: ironbow")
        self.cmap_btn.Bind(wx.EVT_BUTTON, self.on_cmap)
        snap_btn = wx.Button(panel, label="Save PNG")
        snap_btn.Bind(wx.EVT_BUTTON, self.on_snapshot)
        raw_btn = wx.Button(panel, label="Dump raw frame")
        raw_btn.Bind(wx.EVT_BUTTON, self.on_raw)
        copy_btn = wx.Button(panel, label="Copy log")
        copy_btn.Bind(wx.EVT_BUTTON, self.on_copy)
        clear_btn = wx.Button(panel, label="Clear log")
        clear_btn.Bind(wx.EVT_BUTTON, self.on_clear)
        for b in (self.start_btn, self.cmap_btn, snap_btn, raw_btn, copy_btn, clear_btn):
            top.Add(b, 0, wx.ALL, 3)

        split = wx.SplitterWindow(panel, style=wx.SP_LIVE_UPDATE)
        self.video = wx.Panel(split, style=wx.BORDER_SUNKEN)
        self.video.SetBackgroundColour(wx.BLACK)
        self.video.Bind(wx.EVT_PAINT, self.on_paint)
        self.log = wx.TextCtrl(split, style=wx.TE_MULTILINE | wx.TE_READONLY |
                               wx.TE_DONTWRAP | wx.TE_DONTEPLETE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND)
        sizer.Add(split, 1, wx.EXPAND)
        panel.SetSizer(sizer)
        split.SplitVertically(self.video, self.log, 420)
        split.SetSashPosition(420)

        self.SetStatusBar(wx.StatusBar(self))
        self.push_status("plug the Nano in, then press Start stream")

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self.timer.Start(70)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def push_status(self, text):
        self.GetStatusBar().SetStatusText(text)

    # ---- stream control ----
    def on_toggle(self, ev):
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.stop_flag.set()
            self.stream_thread = None
            self.start_btn.SetLabel("Start stream")
            self.push_status("stopped")
            return
        self.frame_raw = None
        self.push_status("starting ...")
        self.stream_thread = Stream(self.q, self)
        self.stream_thread.start()
        self.start_btn.SetLabel("Stop stream")

    def on_cmap(self, ev):
        self.lut_i = (self.lut_i + 1) % len(COLORMAPS)
        self.cmap_btn.SetLabel("colormap: " + COLORMAPS[self.lut_i][0])
        self.video.Refresh()

    def on_snapshot(self, ev):
        if self.frame_raw:
            self._save_png("capture_%d.png")
        else:
            self.push_status("no frame yet")

    def on_raw(self, ev):
        if self.frame_raw:
            name = "frame_%d.raw" % int(time.time())
            open(name, "wb").write(self.frame_raw)
            self.q.put(("log", "saved " + name))
        else:
            self.push_status("no frame yet")

    def on_copy(self, ev):
        val = self.log.GetValue()
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(val))
            wx.TheClipboard.Close()
            self.q.put(("log", "-> copied to clipboard"))
        else:
            self.q.put(("log", "-> clipboard unavaiable"))

    def on_clear(self, ev):
        self.log.Clear()

    def _save_png(self, fmt):
        arr = np.frombuffer(self.frame_raw, dtype="<u2")
        samples = arr[3:]
        img_arr = samples[:W * H].reshape(H, W)
        vmin, vrange = img_arr.min(), max(1, img_arr.max() - img_arr.min())
        t = (img_arr.astype(np.float32) - vmin) / vrange
        rgb = COLORMAPS[self.lut_i][1](t)
        if hasattr(rgb, "astype"):
            rgb = rgb.astype(np.uint8)
        try:
            from PIL import Image
            Image.fromarray(rgb).save(fmt % int(time.time()))
            self.q.put(("log", "saved " + fmt % int(time.time())))
        except ImportError:
            self.q.put(("log", "PIL missing, raw not saved"))

    # ---- painting ----
    def on_paint(self, ev):
        dc = wx.PaintDC(self.video)
        dc.SetBackground(wx.BLACK_BRUSH)
        dc.Clear()
        self._draw_bitmap(dc)
        return

    def _bmp(self):
        if self.frame_raw is None:
            return None
        arr = np.frombuffer(self.frame_raw, dtype="<u2")
        samples = arr[3:]
        img_arr = samples[:W * H].reshape(H, W)
        vmin, vrange = img_arr.min(), max(1, img_arr.max() - img_arr.min())
        t = (img_arr.astype(np.float32) - vmin) / vrange
        rgb = COLORMAPS[self.lut_i][1](t).astype(np.uint8)
        img = wx.Image(W, H, rgb.tobytes())
        return wx.Bitmap(img.Scale(VW, VH, wx.IMAGE_QUALITY_NEAREST))

    def _draw_bitmap(self, dc):
        bmp = self._bmp()
        if bmp is None:
            dc.SetPen(wx.Pen((30, 230, 255)) if hasattr(dc, "SetPen") else None)
            return
        dc.DrawBitmap(bmp, 0, 0)

    # ---- tick ----
    def tick(self, ev):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "frame":
                self.frame_raw = payload
                self.video.Refresh()
            elif kind == "status":
                self.push_status(payload)
            elif kind == "error":
                self.log.AppendText("ERROR: %s\n" % payload)
                self.push_status("ERROR: " + payload[:80])
                self.stream_thread = None
                self.start_btn.SetLabel("Start stream")
            elif kind == "log":
                self.log.AppendText("%s\n" % payload)
                if self.log.GetLastPosition() > 4000:
                    self.log.Remove(0, 2000)
        if self.frame_raw and not self.paused:
            self.video.Refresh()

    def on_close(self, ev):
        if self.stream_thread:
            self.stream_thread.stop_flag.set()
        ev.Skip()


if __name__ == "__main__":
    app = wx.App(False)
    v = Viewer()
    v.Show(True)
    app.MainLoop()
