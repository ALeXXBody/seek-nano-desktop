#!/usr/bin/env python3
"""Render the real Viewer under Xvfb and save a screenshot.

Linux here gets the real [wx/numpy] stack via the system python, so I can
LOOK at the shipped GUI on my own instead of shipping designs unseen.
usb / PIL are the only hardware-ish imports: stubbed, camera not needed.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))

import stubs as W
fake_usb_core = types.ModuleType("usb.core")
fake_usb_core.USBError = W.USBError
fake_usb_core.find = lambda **kw: None
fake_usb = types.ModuleType("usb")
fake_usb.USBError = W.USBError
fake_usb.core = fake_usb_core
fake_usb.util = W.FakeUSBUtil()
sys.modules.update({"usb": fake_usb, "usb.core": fake_usb_core,
                    "usb.util": fake_usb.util})

import wx          # real wx (Xvfb)

APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
import viewer

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/shot.png"
STATE = sys.argv[2] if len(sys.argv) > 2 else "start"

app = wx.App(False)
v = viewer.Viewer()
v.Show()

# push a burst of synthetic frames so the AGC gate seeds and a frame shows
if STATE == "frame":
    def burst(n=20):
        for i in range(n):
            v.q.put(("frame", W.make_frame_body(seq=i + 2)))
            wx.CallLater(45, lambda i=i: v.q.put(
                ("frame", W.make_frame_body(seq=i + 2))))
    burst()

def capture():
    size = v.GetClientSize()
    bmp = wx.Bitmap(size.x, size.y)
    mdc = wx.MemoryDC()
    mdc.SelectObject(bmp)
    sdc = wx.ScreenDC()
    pos = v.ClientToScreen((0, 0))
    mdc.Blit(0, 0, size.x, size.y, sdc, pos.x, pos.y)
    mdc.SelectObject(wx.NullBitmap)
    img = bmp.ConvertToImage()
    img.SaveFile(OUT, wx.BITMAP_TYPE_PNG)
    v.Close()
    print("DEBUG shown=%d held=%d rej=%d frame_raw=%s gainhist=%d" % (
        v.shown_frames, v.held_gain, v.bad_frames,
        getattr(v, "frame_raw", None) is not None, len(v.gain_hist)))

if STATE == "frame":
    app.ProcessPendingEvents()
    wx.CallLater(400, v.Update)
    wx.CallLater(800, capture)
else:
    wx.CallLater(400, capture)
app.MainLoop()
print("saved", OUT)
