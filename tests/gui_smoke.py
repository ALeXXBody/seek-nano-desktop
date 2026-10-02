"""CI smoke: build the real GUI once on the runner's real wxPython.

Catches toolkit-API drift (e.g. the 'Button' object has no attribute
'SetOwnBackColor' crash that shipped in v0.6.0-hud: tests never built the
GUI class, only imported the module). Constructed headless - the window is
never shown - and any exception fails the build with a traceback.
"""
import os
import sys
import wx

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

app = wx.App(False)          # offscreen; no window is shown
import viewer

v = viewer.Viewer()          # full __init__: theme, header, sizers, icon
v.push_status("connected - protocol handshake ok")
v.header.set_state("connected", wx.Colour(0, 255, 0), fps="frame 39 ms")
v._tick(None)                # one drain of an empty event queue

import wx.lib.sized_controls as sc					 # noqa: F401 (import sanity)
wx.CallAfter(v.Close)
app.MainLoop()
print("gui smoke: Viewer() built, status painted, tick drained - ok")
