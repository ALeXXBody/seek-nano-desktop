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

# ============================================================================
# --- DEV: endpoint knowledge from the 2026-10-02 capture campaign -----------
#   USB\VID_289D&PID_0011\serial     parent      usbccgp
#   USB\VID_289D&PID_0011&MI_00\...  iAP Interface  (alt 0: 0x01 OUT / 0x81 IN)
#   USB\VID_289D&PID_0011&MI_01\...  com.thermal.pir324.3 (alt 0: empty,
#                                    alt 1: 0x02 OUT / 0x82 IN)
# Direct capture on the camera host showed the *thermal stream on MI_00/0x81*
# (~63 ms per frame). MI_01/0x82 is silent, MI_01 binds it at alt 1 because
# the composite parent gives interface 1 two bulk pipes that only appear
# there. For a dev build we therefore try every bulk-in endpoint we can see,
# in a deterministic order, and prefer whichever one actually produces data.
# ============================================================================
EP_CANDIDATES = (0x81, 0x82)   # iface 0 alt 0 first, iface 1 alt 1 second


# ----------------------------------------------------------------------------
# --- DEV: HTTP frame uploader (replaces loop.ps1 / sncap.exe) ----------------
class DevUploader:
    """PUTs captured frames to a dev host over plain HTTP.

    Keep this until the stream path is stable; it is a one-file replacement
    for the sncap.exe + loop.ps1 + loop.cmd + bind.ps1 experiment kit and is
    clearly segregated so it can be yanked for the stable release.
    """

    def __init__(self, host, port=8100):
        # host may be "192.168.50.200" or "192.168.50.200:8100"
        if ":" in host:
            parts = host.split(":")
            self.host, self.port = parts[0], int(parts[1])
        else:
            self.host, self.port = host, port
        self.seq = 0
        self.last_ok = False

    @property
    def base(self):
        return "http://{}:{}".format(self.host, self.port)

    def put_frame(self, blob, label=None):
        self.seq += 1
        name = label or "frame_{0:04d}.raw".format(self.seq)
        blob_to_send = blob
        return self._put(name, blob_to_send), name

    def put_logs(self, text):
        return self._put("logs.txt", text.encode("utf-8", "replace"))

    def put_bindinfo(self, text):
        return self._put("bindinfo.txt", text.encode("utf-8", "replace"))

    def _put(self, name, blob):
        import http.client
        try:
            c = http.client.HTTPConnection(self.host, self.port, timeout=15)
            c.request("PUT", "/" + name, blob,
                      {"Content-Type": "application/octet-stream"})
            r = c.getresponse()
            r.read()
            c.close()
            self.last_ok = (r.status == 200)
            return self.last_ok
        except Exception as e:
            print("dev upload failed: %r" % e)
            self.last_ok = False
            return False


# --- DEV: PnP bind-state probe (replaces diag.cmd) ---------------------------
def dev_bindinfo():
    """Return a text report of the PnP binding state of every Seek node.

    Wraps Get-PnpDevice so no helper .ps1/.cmd files are needed on the box.
    """
    import subprocess
    ps = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -match 'VID_289D' } | "
        "ForEach-Object { "
        "  $svc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName DEVPKEY_Device_Service).Data; "
        "  '{0,-30} {1,-26} svc={2}' -f $_.FriendlyName, $_.InstanceId, $svc "
        "}"
    )
    out = ["--- PnP binding state (Get-PnpDevice) ---"]
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30)
        out += (res.stdout or "").splitlines()
        if res.returncode != 0 and res.stderr:
            out += ["stderr: " + l for l in res.stderr.splitlines()[:4]]
    except Exception as e:
        out.append("bindinfo probe failed: %r" % e)
    out.append("--- dev: end ---")
    return "\n".join(l.rstrip() for l in out if l.strip())


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


# unified native transport loader (fresh-install friendly):
#   seeknanodirect.dll (libusb-1.0: works with libusbK AND WinUSB bindings)
#   seeknanousb.dll    (pure WinUSB API path)
import ctypes as _ct

class NativeTransport:
    def __init__(self):
        self.k = None
        self.name = None
        # candidates: also check PyInstaller's bundle dir; for a windowed
        # onefile build the dll is extracted to sys._MEIPASS, never the exe
        # folder, so plain ctypes.CDLL("name.dll") fails there.
        cand = ["seeknanodirect.dll", "seeknanousb.dll"]
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            cand = [os.path.join(sys._MEIPASS, n) for n in cand] + cand
        for path, pfx in zip(cand, ["SNLB_", "SNLB_", "SN_", "SN_"]):
            try:
                d = _ct.CDLL(path)
                getattr(d, pfx + "open").restype = _ct.c_int
                getattr(d, pfx + "stream_start").restype = _ct.c_int
                getattr(d, pfx + "get_frame").restype = _ct.c_int
                getattr(d, pfx + "get_frame").argtypes = [_ct.c_char_p]
                getattr(d, pfx + "stream_stop").restype = _ct.c_int
                getattr(d, pfx + "close").restype = None
                self.k = (name := os.path.basename(path), pfx, d)
                self.name = name
                break
            except Exception:
                continue
        if self.k is None:
            raise OSError("no native transport available")

_ctdll = None
try:
    _ctdll = NativeTransport()
except Exception:
    _ctdll = None


class NativeStream(threading.Thread):
    """driver-grade path: overlapped WinUSB reads in C, one call per frame."""
    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q
        self.stop_flag = threading.Event()
        self.buf = ctypes.create_string_buffer(FRAME_BYTES)
        self.dll = _ctdll
        self.dev = None

    def run(self):
        name, pfx, d = self.dll.k
        if getattr(d, pfx + "open")() != 0:
            self.q.put(("error", "native open failed - run SeekNanoDriverInstaller.exe "
                                 "(auto-picks Zadig if Win11 refuses the INF)"))
            Stream(self.q).run()
            return
        self.dev = True
        self.q.put(("log", "native transport: " + name +
                        (" (libusb-1.0, libusbK/WinUSB compatible)" if pfx == "SNLB_" else " (WinUSB API)")))
        rc = getattr(d, pfx + "stream_start")()
        if rc != 0:
            self.q.put(("log", "native handshake rc=%d, using working USB path" % rc))
            getattr(d, pfx + "close")()
            Stream(self.q).run()
            return
        self.q.put(("status", "native streaming - up to 25 fps"))
        self.q.put(("log", "handshake ok, reading frames"))
        fails = 0
        get_frame = getattr(d, pfx + "get_frame")
        try:
            while not self.stop_flag.is_set():
                t0 = time.time()
                n = get_frame(self.buf)
                dt = time.time() - t0
                if n == FRAME_BYTES:
                    fails = 0
                    self.q.put(("frame", bytes(self.buf.raw)))
                    self.q.put(("info", "frame %.0f ms" % (dt * 1000)))
                    wait = 0.04 - dt
                    if wait > 0 and not self.stop_flag.is_set():
                        time.sleep(wait)
                else:
                    fails += 1
                    self.q.put(("log", "skip bad frame %d (rc %s)" % (fails, n)))
                    if fails > 12:
                        self.q.put(("error", "native pump stalled - stop/start"))
                        break
                    time.sleep(0.2)
        except Exception as e:
            self.q.put(("error", "native read: %s" % e))
        getattr(d, pfx + "stream_stop")()
        getattr(d, pfx + "close")()



class Stream(threading.Thread):
    def __init__(self, q, status_cb=None, upload_target=None, upload_port=None):
        super().__init__(daemon=True)
        self.q = q
        self.status_cb = status_cb
        self.upload_target = upload_target
        self.upload_port = upload_port
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
                # --- DEV: walk the candidate endpoints and remember the
                # ones that actually carry data instead of assuming the
                # first one (MI_00/0x81 wins on this hardware, see header).
                order = [ep for (_i, ep) in eps
                         if ep in EP_CANDIDATES] or [ep for (_i, ep) in eps]
                self.ep_order = order
                self.put(("log", "bulk-in candidates: " +
                          ", ".join("0x%02x" % e for e in order)))
            else:
                self.ep_order = [BULK_EP]
            self.ep_i = 0
            iface_n, ep_addr = 0, self.ep_order[0]
            self.put(("log", "starting with ep 0x%02x" % ep_addr))
            dev_uploader = None
            if getattr(self, "upload_target", None):
                host = self.upload_target
                port = int(self.upload_port) if self.upload_port else 8100
                dev_uploader = DevUploader(host, port)
                self.put(("log", "dev upload -> %s:%d" % (host, port)))
            kicks = 0
            fails = 0
            exchanged = 0
            period = 0.040            # start optimistic: try the 25 fps spec
            MIN_PERIOD, MAX_PERIOD = 0.040, 0.115
            last_ok = time.time()
            while not self.stop_flag.is_set():
                wait = period - (time.time() - last_ok)
                if wait > 0:
                    time.sleep(wait)
                t0 = time.time()
                self.ctrl_out(0x53, b"\x58\x5b\x01\x00")
                kicks += 1
                try:
                    buf = bytearray()
                    while len(buf) < FRAME_BYTES and not self.stop_flag.is_set():
                        buf.extend(dev.read(ep_addr, CHUNK, 500))
                    last_ok = time.time()
                    dt = last_ok - t0
                    if fails == 0 and period > MIN_PERIOD:
                        period = max(MIN_PERIOD, period - 0.005)
                        if kicks % 12 == 0:
                            self.put(("log", "pacing %.0f ms (dt %.0f ms) ~ %.1fkicks/s" % (period*1000, dt*1000, 1.0/period)))
                    fails = 0
                    if dev_uploader:
                        try:
                            ok, name = dev_uploader.put_frame(bytes(buf))
                            exchanged += ok
                            self.put(("log", "dev upload %s -> %s" %
                                      (name, "ok" if ok else "FAILED")))
                        except Exception as ue:
                            self.put(("log", "dev upload error: %r" % ue))
                except usb.core.USBError as ue:
                    fails += 1
                    self.trace("BULK FAIL %s (fails=%d): %s" % (type(ue).__name__, fails, ue))
                    if fails == 2 and len(self.ep_order) > 1:
                        # advance to the next candidate - MI_01/0x82 is
                        # silent on this hardware so keep going until we
                        # find the live endpoint rather than waiting for
                        # a full reset round
                        self.ep_i = (self.ep_i + 1) % len(self.ep_order)
                        ep_addr = self.ep_order[self.ep_i]
                        fails = 0
                        self.put(("log", "dev: switching to endpoint 0x%02x" % ep_addr))
                    if fails == 3:
                        self.put(("log", "stalled - resetting mode, pacing %.0f ms" % (period*1000)))
                        try:
                            self.ctrl_out(0x3c, b"\x00\x00")
                            self.ctrl_out(0x3e, b"\x08\x00")
                            time.sleep(0.05)
                            self.ctrl_out(0x37, b"\xfc\x00\x04\x00")
                            self.ctrl_out(0x3c, b"\x01\x00")
                            self.ctrl_in(0x3d, 2)
                            time.sleep(0.2)
                        except Exception:
                            pass
                    if fails > 12:
                        self.put(("error", "no frames for 12 kicks - camera unresponsive; press Stop then Start"))
                        return
                    if fails <= 3:
                        time.sleep(0.1)
                        period = min(MAX_PERIOD, period * 1.3)
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
        self.nuc = None            # shutterless-NUC background reference
        self.lo_ema = None
        self.hi_ema = None

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

        # --------------------------------------------------------------------
        # --- DEV row: frame upload to a dev host + endpoint / bind probes --
        # Internal to the development flow, removed for the stable build.
        self.dev_host = wx.TextCtrl(panel, value="192.168.50.200:8100",
                                    style=wx.TE_PROCESS_ENTER)
        self.dev_upload = wx.CheckBox(panel, label="dev: upload frames")
        self.dev_probe = wx.Button(panel, label="dev: retry endpoints")
        self.dev_bind = wx.Button(panel, label="dev: bind info")
        self.dev_upload.SetValue(False)
        self.dev_probe.Bind(wx.EVT_BUTTON, self.on_dev_probe)
        self.dev_bind.Bind(wx.EVT_BUTTON, self.on_dev_bind)
        for b in (self.dev_host, self.dev_upload, self.dev_probe, self.dev_bind):
            top.Add(b, 0, wx.ALL, 3)
        # --------------------------------------------------------------------

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
        upload_target = None
        upload_port = None
        if self.dev_upload.GetValue():
            raw = self.dev_host.GetValue().strip()
            if ":" in raw:
                upload_target, upload_port = raw.rsplit(":", 1)
            else:
                upload_target = raw
            self.q.put(("log", "dev: frames will POST to %s:%s" %
                        (upload_target, upload_port)))
        # DEV: when frame upload is on, prefer the pyusb stream so the
        # endpoint auto-probe and uploader are in the same loop, even if
        # the native dlls are present
        if self.dev_upload.GetValue() and upload_target:
            self.stream_thread = Stream(self.q, upload_target=upload_target,
                                        upload_port=upload_port)
        elif _ctdll is not None:
            self.stream_thread = NativeStream(self.q)
        else:
            self.stream_thread = Stream(self.q, upload_target=upload_target,
                                        upload_port=upload_port)
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

    # ------------------------------------------------------------------
    # --- DEV handlers --------------------------------------------------
    def on_dev_probe(self, ev):
        """force the next Start to walk the endpoint candidates again"""
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.stop_flag.set()
            self.stream_thread = None
        self.start_btn.SetLabel("Start stream")
        self.push_status("dev: stopped, will auto-probe endpoints on Start")
        self.q.put(("log", "dev: restart the stream to re-probe endpoints"))

    def on_dev_bind(self, ev):
        text = dev_bindinfo()
        self.q.put(("log", text))
        name = "bindinfo.txt"
        open(name, "w", encoding="utf-8").write(text)
        self.q.put(("log", "saved " + name))

    # ------------------------------------------------------------------

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
        cached = getattr(self, "_bmp_cache", None)
        if cached and cached[0] == self.frame_raw:
            return cached[1]
        if self.frame_raw is None:
            return None
        raw = np.frombuffer(self.frame_raw, dtype="<u2").astype(np.float32)
        raw = raw[:RAW_W * RAW_H].reshape(RAW_H, RAW_W)
        img = raw[ROI_Y:ROI_Y + IMG_H, ROI_X:ROI_X + IMG_W]
        fstd = float(img.std())
        fmean = float(img.mean())
        # a flat frame is the shutter reference (its pixels carry no scene)
        if fstd < 25.0:
            self.ffc = img.copy()
            self._bmp_cache = (self.frame_raw, None)
            return None
        if self.ffc is not None:
            img = img - self.ffc + 0x4000
        p1 = np.pad(img, 1, mode="edge")
        stack = np.stack([p1[dy:dy+IMG_H, dx:dx+IMG_W] for dy in range(3) for dx in range(3)])
        img = np.median(stack, axis=0)
        lo = float(np.percentile(img, 2))
        hi = float(np.percentile(img, 98))
        t = np.clip((img - lo) / max(1.0, hi - lo), 0, 1)
        rgb = COLORMAPS[self.lut_i][1](t).astype(np.uint8)
        i2 = wx.Image(IMG_W, IMG_H, rgb.tobytes())
        vw = min(2 * self.W, 820)
        vh = int(vw * self.H / self.W)
        bmp = wx.Bitmap(i2.Scale(vw, vh, wx.IMAGE_QUALITY_NEAREST))
        self._bmp_cache = (self.frame_raw, bmp)
        return bmp

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



def serve_mode(port=5005):
    """Headless camera ferry: streams every raw frame as UDP packets to the LAN.
    Usage:  SeekNano.exe --serve [port]
    Sends:  header: magic 'SNFR', frame seq (u32), offset (u32), total (u32), Frags (u16)
            payload: raw 177840 bytes, chunked into 1400-byte pieces.
    Also dumps every control transfer to seeknano_verbose.log."""
    import socket, struct
    import usb.core, usb.util

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

    target = "255.255.255.255"  # broadcast on the LAN; server sniffs it

    # find the device the same way the viewer does (usb / libusb_package)
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except Exception:
        backend = None
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        print("camera not found")
        return
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    print("camera open, streaming to UDP broadcast")

    def ctrl_out(request, payload, retry=True):
        try:
            dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
        except usb.core.USBError as e:
            # EPIPE (stalled control pipe) or busy device: reset once and retry
            if retry and (getattr(e, "errno", None) == 32 or "Pipe" in str(e)):
                print(f"pipe stall on ctrl req 0x{request:02x}: {e}; resetting camera")
                try:
                    dev.reset()
                    time.sleep(1)
                    dev.set_configuration()
                    usb.util.claim_interface(dev, 0)
                    dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
                    return
                except Exception as e2:
                    print(f"reset failed: {e2}. unplug and replug the camera, then rerun.")
                    sys.exit(3)
            raise
    def ctrl_in(request, length):
        return bytes(dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))

    # exact 483-op replay of the phone session
    ctrl_out(0x54, b"\x00\x00")
    ctrl_out(0x3C, b"\x00\x00")
    ctrl_in(0x3D, 2)
    ctrl_out(0x3E, b"\x08\x00")
    # identity
    ctrl_out(0x56, bytes([0x08, 0x00, 0x02, 0x06, 0x00, 0x00]))
    ctrl_in(0x58, 16)
    ctrl_in(0x4E, 4)
    ctrl_out(0x55, b"\x17\x00")
    ctrl_in(0x4E, 64)
    ctrl_in(0x4E, 4)
    ctrl_in(0x36, 12)
    ctrl_out(0x55, b"\x15\x00")
    ctrl_in(0x4E, 64)
    ctrl_in(0x4E, 4)
    ctrl_in(0x36, 12)
    for page in range(11):
        addr = page * 64
        ctrl_out(0x56, bytes([0x20, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, 0, 0]))
        ctrl_in(0x58, 64)
    ctrl_out(0x55, b"\x15\x00")
    ctrl_in(0x4E, 64)
    ctrl_in(0x35, 4)
    ctrl_out(0x37, b"\xfc\x00\x04\x00")
    ctrl_out(0x3C, b"\x01\x00")
    ctrl_in(0x3D, 2)
    print("handshake complete, streaming raw frames over UDP")

    seq = 1
    FrmPer = 1342  # 177840 / 1400 pieces
    while True:
        ctrl_out(0x53, b"\x58\x5b\x01\x00")
        buf = bytearray()
        while len(buf) < FRAME_BYTES:
            chunk = dev.read(BULK_EP, CHUNK, 400)
            buf.extend(chunk)
        if len(buf) != FRAME_BYTES:
            continue
        frame = bytes(buf)
        # 11 fragments of payload 1400 B each + last remainder
        offs = 0
        while offs < FRAME_BYTES:
            chunk = frame[offs:offs + 1400]
            pkt = b"SNFR" + struct.pack("<II I", seq, offs, FRAME_BYTES) + chunk
            sock.sendto(pkt, (target, port))
            offs += len(chunk)
        seq += 1
        print(f"frame {seq} sent", end="\r")

def _crashlog(fn, *a):
    try:
        fn(*a)
    except Exception:
        import traceback, sys as _s
        with open("crash.txt", "a") as f:
            traceback.print_exc(file=f)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "crash.txt written next to the exe", "SeekNano", 0x10)


def parse_target(s, default_port=8100):
    "192.168.50.200[:8100] -> (ip, port)"
    if ":" in s:
        host, port = s.rsplit(":", 1)
        return host, int(port)
    return s, default_port


# ---------------------------------------------------------------------------
# --- DEV: headless frame collector, replaces loop.ps1 entirely --------------
def dev_serve(target_url="192.168.50.200:8100", frames_per_batch=3):
    """Dev headless capture: stream raw frames to a collector over HTTP PUT.

    Usage:  SeekNano.exe --dev-serve [host[:port]]

    Uses the native WinUSB transport dll so the run does not depend on libusb
    being able to open the composite *parent* node (pyusb fails with
    "[Errno 13] Access denied" there, because usbccgp owns the parent).
    Frames are PUT to http://<host>:<port>/frame_<seq>.raw.
    Log: devserve.log next to the exe (printf-safe, windowed build).
    """
    import ctypes
    _box = ctypes.windll.user32.MessageBoxW
    logf = open("devserve.log", "a", buffering=1)

    def say(msg, fatal=False):
        print(msg)
        logf.write("%.3f %s\n" % (time.time(), msg) if not fatal else msg)
        try:
            logf.write("%.3f %s\n" % (time.time(), msg))
        except Exception:
            pass
        if fatal:
            _box(None, msg, "SeekNano dev", 0x10)

    host, port = parse_target(target_url)
    uploader = DevUploader(host, port)
    say("dev-serve target: %s:%d" % (host, port))

    # ---- native transport: WinUSB API on the MI_00 child (live stream) ----
    # prefer seeknanousb.dll (pure WinUSB API, GUID {1C2BD42A...} = MI_00,
    # bulk ep 0x81): that is the transport that captured 177840-byte frames
    # directly on this hardware.  seeknanodirect.dll (libusb) enumerates the
    # composite and lands on the *silent* MI_01 half (stream_start rc=-30).
    name = pfx = d = None
    frozen_cand = []
    base_dir = (sys._MEIPASS if getattr(sys, "frozen", False) and
                hasattr(sys, "_MEIPASS") else
                os.path.dirname(os.path.abspath(sys.executable)) if
                getattr(sys, "frozen", False) else
                os.path.dirname(os.path.abspath(__file__)))
    for fn, pfx_ in (("seeknanousb.dll", "SN_"), ("seeknanodirect.dll", "SNLB_")):
        for path in (os.path.join(base_dir, fn), fn):
            try:
                d_ = _ct.CDLL(path)
                getattr(d_, pfx_ + "open").restype = _ct.c_int
                getattr(d_, pfx_ + "stream_start").restype = _ct.c_int
                getattr(d_, pfx_ + "get_frame").restype = _ct.c_int
                getattr(d_, pfx_ + "get_frame").argtypes = [_ct.c_char_p]
                getattr(d_, pfx_ + "stream_stop").restype = _ct.c_int
                getattr(d_, pfx_ + "close").restype = None
                name, pfx, d = fn, pfx_, d_
                break
            except Exception:
                continue
        if name is not None:
            break
    if name is None:
        say("dev: no native transport dll found next to SeekNano.exe ({})\n"
            "Download SeekNano-devbundle.zip from the release and extract "
            "it next to the exe (seeknanousb.dll + seeknanodirect.dll)."
            .format(base_dir), True)
        return
    say("dev transport: " + name)

    if getattr(d, pfx + "open")() != 0:
        say("dev: native open failed.\n\n"
            "Make sure both child interfaces are bound to WinUSB "
            "(run bind.ps1 once, or SeekNanoDriverInstaller).\n\n"
            "Details: devserve.log, bindinfo.txt.", True)
        try:
            open("bindinfo.txt", "w", encoding="utf-8").write(dev_bindinfo())
        except Exception:
            pass
        return

    rc = getattr(d, pfx + "stream_start")()
    say("stream_start rc=%d" % rc)

    buf = ctypes.create_string_buffer(FRAME_BYTES)
    seq = 0
    batch = 0
    fails_total = 0
    last_ok = time.time()
    get_frame = getattr(d, pfx + "get_frame")
    stream_stop = getattr(d, pfx + "stream_stop")
    say("streaming; frames PUT to %s:%d" % (host, port))
    while True:
        n = get_frame(buf)
        if n != FRAME_BYTES:
            fails_total += 1
            say("frame attempt got %s bytes (fails=%d)" % (n, fails_total))
            if fails_total > 12:
                say("dev: native pump stalled - rerun, or unplug/replug.")
                stream_stop()
                return
            time.sleep(0.2)
            continue
        seq += 1
        batch += 1
        ok, _ = uploader.put_frame(bytes(buf.raw))
        dt = (time.time() - last_ok) * 1000.0
        say("frame %d ok  %4.0f ms  upload=%s" %
            (seq, dt, "ok" if ok else "FAILED"))
        fails_total = 0
        last_ok = time.time()
        if batch >= frames_per_batch:
            batch = 0
            time.sleep(0.5)


def dev_bind():
    """Print the PnP binding state (replaces diag.cmd binds)."""
    print(dev_bindinfo())
    open("bindinfo.txt", "w", encoding="utf-8").write(dev_bindinfo())
    print("saved bindinfo.txt")


def selftest():
    results = []
    for modstr in ("wx", "numpy", "queue", "PIL"):
        try:
            __import__(modstr)
            results.append((modstr, "ok"))
        except Exception as e:
            results.append((modstr, "FAIL " + repr(e)))
    for dname in ("seeknanodirect.dll", "seeknanousb.dll"):
        try:
            ctypes.CDLL(dname)
            results.append((dname, "ok"))
        except Exception as e:
            results.append((dname, "missing (install SeekNanoDriverInstaller first)"))
    try:
        import libusb_package
        libusb_package.get_libusb1_backend()
        results.append(("libusb-1.0 backend", "ok"))
    except Exception as e:
        results.append(("libusb-1.0 backend", "FAIL " + repr(e)))
    print("\n".join("%-22s %s" % (a, b) for a, b in results))
    # exit 1 if anything critical failed
    crit = [r for r in results if r[0] in ("wx", "numpy")]
    for r in crit:
        if "FAIL" in r[1]:
            sys.exit(1)
    sys.exit(0)

def _run_gui():
    app = wx.App(False)
    v = Viewer()
    v.Show(True)
    app.MainLoop()

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--dev-serve" in sys.argv:
        # dev: python viewer.py --dev-serve 192.168.50.200:8100 [frames_per_batch]
        target = "192.168.50.200:8100"
        fps = 3
        argv = sys.argv[sys.argv.index("--dev-serve") + 1:]
        if argv:
            target = argv[0]
        if len(argv) > 1 and argv[1].isdigit():
            fps = int(argv[1])
        _crashlog(dev_serve, target, fps)
    elif "--dev-bind" in sys.argv:
        dev_bind()
    elif "--serve" in sys.argv:
        port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 5005
        _crashlog(serve_mode, port)
    else:
        _crashlog(_run_gui)

