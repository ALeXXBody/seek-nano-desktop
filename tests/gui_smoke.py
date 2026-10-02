"""CI smoke: build the real GUI once on the runner's real wxPython.

Catches toolkit-API drift (e.g. the 'Button' object has no attribute
'SetOwnBackColor' crash). Constructed headless - the window is never shown
- and any exception fails the build with a traceback.
"""
import os
import sys
import wx

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

app = wx.App(False)          # offscreen; no window is shown
import viewer

v = viewer.Viewer()          # full __init__: glass buttons, title bar, icon

# exercise every painted control so API drift shows here, not to a user
v.start_btn.SetLabel("Stop stream")
v.start_btn.SetLabel("Start stream")
v.cmap_btn.SetLabel("colormap: grayscale")
v.spot_btn.SetLabel("hot spots: mark")
v.header.set_state("connected - protocol handshake ok",
                   wx.Colour(46, 204, 113), fps="frame 39 ms")
v.push_status("streaming mode enabled - pumping frames")
v.header.Refresh()
v.Update()                   # run one paint pass synchronously
v._tick(None)                # drain an empty event queue

wx.CallAfter(v.Close)
app.MainLoop()
print("gui smoke: glass buttons relabelled, title bar painted, tick drained - ok")
