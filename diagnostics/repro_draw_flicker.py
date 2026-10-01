"""Minimal repro: does wx's Image.Scale -> Bitmap -> DrawBitmap flicker?

The app draws a provably good array on every single paint - 713 of 713, rgb std
69-78, never flat - yet the screen shows a featureless field about 40% of the
time. So the corruption is in the draw path, not the data.

This paints a static thermal-looking bitmap in a bare wx frame using the SAME
sequence the app uses (wxImage from a raw buffer -> Scale -> wx.Bitmap ->
wx.PaintDC -> Clear -> DrawBitmap), then a second window using
wx.Bitmap.FromBuffer + dc.StretchBitmap instead. An external screen capture
measures both. If only the Scale path flickers, the fix is known.
"""
import sys

import numpy as np
import wx

MODE = sys.argv[1] if len(sys.argv) > 1 else "scale"
W, H = 320, 240

# a fixed, detailed image so any flatness on screen is unambiguous
yy, xx = np.mgrid[0:H, 0:W]
base = (120 + 60 * np.sin(xx / 9.0) * np.cos(yy / 7.0)
        + 90 * np.exp(-(((xx - 110) ** 2 + (yy - 130) ** 2) / 2600.0)))
rgb = np.zeros((H, W, 3), np.uint8)
t = np.clip((base - base.min()) / (base.max() - base.min()), 0, 1)
rgb[..., 0] = (255 * t).astype(np.uint8)
rgb[..., 1] = (60 + 120 * t).astype(np.uint8)
rgb[..., 2] = (255 * (1 - t)).astype(np.uint8)
BUF = np.ascontiguousarray(rgb).tobytes()


class Panel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=(640, 480))
        self.SetBackgroundColour(wx.BLACK)
        self.buf = BUF
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.n = 0

    def on_timer(self, ev):
        self.n += 1
        self.Refresh()

    def on_paint(self, ev):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.BLACK_BRUSH)
        dc.Clear()
        w, h = self.GetClientSize()
        if MODE == "scale":
            # exactly what the app does
            i2 = wx.Image(W, H, self.buf)
            bmp = wx.Bitmap(i2.Scale(int(w), int(h), wx.IMAGE_QUALITY_NEAREST))
            dc.DrawBitmap(bmp, 0, 0)
        else:
            bmp = wx.Bitmap.FromBuffer(W, H, self.buf)
            dc.StretchBitmap(bmp, 0, 0, int(w), int(h),
                             wx.IMAGE_QUALITY_NEAREST)


app = wx.App(False)
f = wx.Frame(None, title="ReproProbe", size=(660, 520))
p = Panel(f)
f.Show()
p.timer.Start(70)
print("mode=%s  running - capture the screen for ~20 s" % MODE, flush=True)
app.MainLoop()