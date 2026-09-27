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

# keep next to the .exe (not PyInstaller's temp _MEIxxxx dir, Windows deletes it)
if getattr(sys, "frozen", False):
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))
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

RAW_W, RAW_H = 342, 260
IMG_W, IMG_H = 320, 240
ROI_X, ROI_Y = 1, 4           # seek-like ROI (see libseek-thermal SeekThermalPro)
OFFSET_BIAS = 0x4000


def decode_frame(frame_raw):
    """SeekThermalPro-layout decoder: raw 342x260 -> image 320x240."""
    arr = np.frombuffer(frame_raw, dtype="<u2").astype(np.float32)
    raw = arr[:RAW_W * RAW_H].reshape(RAW_H, RAW_W)
    fid = int(raw[0, 2])
    return raw, fid


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

    def trace(self, line, to_ui=False):
        if self.logf:
            self.logf.write("%.3f %s\n" % (time.time(), line))
        if to_ui:
            self.q.put(("log", line))

    def ctrl_out(self, request, payload):
        self.trace("W req=0x%02x data=%s" % (request, payload.hex()))
        self.dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
        self.trace("W req=0x%02x OK" % request)

    def ctrl_in(self, request, length):
        r = bytes(self.dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))
        self.trace("R req=0x%02x len=%d -> %s" % (request, length, r.hex()))
        return r

    def put(self, item):
        self.q.put(item)

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
            eps = []
            try:
                for cfg in dev:
                    for i in cfg:
                        for ep in i:
                            self.trace("cfg=%d iface=%d ep=%#x attrs=0x%02x max=%d" % (
                                cfg.bConfigurationValue, i.bInterfaceNumber,
                                ep.bEndpointAddress, ep.bmAttributes,
                                ep.wMaxPacketSize))
                            if (ep.bEndpointAddress & 0x80) and (ep.bmAttributes & 3) == 2:
                                eps.append((i.bInterfaceNumber, ep.bEndpointAddress))
            except Exception as e:
                self.trace("iface walk skipped: %r" % e)
            self.put(("log", "bulk-in endpoints: " + str(eps)))
            self.q.put(("status", "device opened"))

            # Phase 1 - handshake (exact replay of phone session)
            self.ctrl_out(0x54, b"\x00\x00")
            self.ctrl_out(0x3c, b"\x00\x00")
            self.ctrl_in(0x3d, 2)
            self.ctrl_out(0x3e, b"\x08\x00")
            self.q.put(("status", "connected - protocol handshake ok"))

            # Phase 4 - enable streaming
            self.ctrl_out(0x37, b"\xfc\x00\x04\x00")
            self.ctrl_out(0x3c, b"\x01\x00")
            self.ctrl_in(0x3d, 2)
            self.q.put(("status", "streaming mode enabled - pumping frames"))

            if eps:
                self.ep_i = 0
                iface_n, ep_addr = eps[self.ep_i]
                self.put(("log", "using iface %d ep %#x" % (iface_n, ep_addr)))
            else:
                iface_n, ep_addr = 0, BULK_EP
            kicks = 0
            while not self.stop_flag.is_set():
                self.ctrl_out(0x53, b"\x58\x5b\x01\x00")
                kicks += 1
                try:
                    buf = bytearray()
                    n_chunks = 0
                    while len(buf) < FRAME_BYTES and not self.stop_flag.is_set():
                        chunk = dev.read(ep_addr, CHUNK, 500)
                        buf.extend(chunk)
                        n_chunks += 1
                        self.trace("kick %d chunk %d got %d B (total %d)" % (
                            kicks, n_chunks, len(chunk), len(buf)))
                except usb.core.USBError as ue:
                    self.put(("log", "kick %d: FAIL %s (chunks=%d total=%d)" % (
                        kicks, type(ue).__name__, n_chunks, len(buf))))
                    self.trace("BULK FAIL %s: %s" % (type(ue).__name__, ue))
                    continue
                if len(buf) == FRAME_BYTES:
                    self.put(("frame", bytes(buf)))
                    self.put(("info", f"frame {kicks} ok"))
                else:
                    self.put(("info", f"short frame {len(buf)} B"))
        except Exception:
            import traceback
            tb = traceback.format_exc()
            self.trace(tb)
            self.q.put(("error", tb.splitlines()[-1] if tb else "unknown"))
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
        self.W, self.H = IMG_W, IMG_H
        self.geom_done = True
        self.ffc = None            # flat-field reference frame (averaged)
        self.ffc_bank = []
        self.last_fid = None
        self.disp_prev = None      # temporal smoothing state

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
                               wx.TE_DONTWRAP)
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
        img_arr = arr[2:2 + self.W * self.H].reshape(self.H, self.W)
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
        raw, fid = decode_frame(self.frame_raw)
        self.last_fid = fid
        img_arr = raw[ROI_Y:ROI_Y + IMG_H, ROI_X:ROI_X + IMG_W].copy()
        if fid == 1:
            self.ffc_bank.append(img_arr)
            if len(self.ffc_bank) > 8:
                self.ffc_bank.pop(0)
            self.ffc = np.mean(np.stack(self.ffc_bank), axis=0)
            try:
                open("ffc_latest.raw", "wb").write(bytearray(np.stack(self.ffc_bank).mean(axis=0).astype("<u2").tobytes()))
                self.frame_ffc_saved = True
            except Exception:
                pass
            self.q.put(("log", "FFC frame bank=%d averaged" % len(self.ffc_bank)))
            return None                # don't render the shutter frame itself
        if self.ffc is not None:
            img_arr = img_arr + OFFSET_BIAS - self.ffc   # libseek retrieve()
            # median 3x3: kills dead pixels + salt noise
            p1 = np.pad(img_arr, 1, mode="edge")
            stack = np.stack([p1[dy:dy+IMG_H, dx:dx+IMG_W] for dy in range(3) for dx in range(3)])
            img_arr = np.median(stack, axis=0)
        # temporal smoothing
        if self.disp_prev is not None and self.ffc is not None:
            img_arr = 0.45 * self.disp_prev + 0.55 * img_arr
        self.disp_prev = img_arr.copy()
        vmin = float(np.percentile(img_arr, 2))
        vmax = float(np.percentile(img_arr, 98))
        vrange = max(1.0, vmax - vmin)
        t = np.clip((img_arr.astype(np.float32) - vmin) / vrange, 0, 1)
        rgb = COLORMAPS[self.lut_i][1](t).astype(np.uint8)
        img = wx.Image(IMG_W, IMG_H, rgb.tobytes())
        vw = min(2 * self.W, 820)
        vh = int(vw * self.H / self.W)
        return wx.Bitmap(img.Scale(vw, vh, wx.IMAGE_QUALITY_NEAREST))

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
