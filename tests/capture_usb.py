"""Capture every USB transfer the phone app makes, while it streams.

This is the payload of the whole arm64 detour. The SDK hooks into
UsbDeviceConnection.controlTransfer and bulkTransfer, so logging those gives the
real request codes and payloads the app sends on a working camera - which is
exactly what the static analysis could not isolate.

Run against the embedded Frida gadget:

    python3 capture_usb.py <seconds>

Output is one line per transfer, so the startup sequence and the steady-state
stream can be told apart by eye.
"""
import sys
import time

import frida

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 25

HOOK = r"""
Java.perform(function () {
  var out = [];
  function hex(p, n) {
    var s = '';
    for (var i = 0; i < n; i++) s += ('0' + (p[i] & 0xff).toString(16)).slice(-2);
    return s;
  }
  function stamp() { return (Date.now() % 1000000); }

  function hookClass(name) {
    try {
      var c = Java.use(name);
      c.controlTransfer.overloads.forEach(function (ov) {
        ov.implementation = function (req, val, idx, buf, len, t) {
          var d = '';
          try { if (buf) d = hex(buf, Math.min(len, 16)); } catch (e) {}
          send({ k: 'CTRL-W', c: name, req: req & 0xff, len: len, data: d, t: stamp() });
          var r = ov.apply(this, arguments);
          send({ k: 'CTRL-R', req: req & 0xff, ret: r, t: stamp() });
          return r;
        };
      });
      c.bulkTransfer.overloads.forEach(function (ov) {
        ov.implementation = function (ep, buf, len, t) {
          var r = ov.apply(this, arguments);
          send({ k: 'BULK', ep: ep, len: len, got: r, t: stamp() });
          return r;
        };
      });
      out.push('hooked ' + name);
    } catch (e) {
      out.push('skip ' + name + ': ' + e);
    }
  }

  ['android.hardware.usb.UsbDeviceConnection'].forEach(hookClass);
  send({ k: 'READY', info: out.join(' | ') });
});
"""

dev = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
procs = {p.pid: p for p in dev.enumerate_processes()}
target = None
for pid, p in procs.items():
    # An embedded gadget registers itself as a process literally named
    # "Gadget" rather than under the host app's name, so matching on the
    # package name finds nothing and the run aborts.
    if "seek" in p.name.lower() or "thermal" in p.name.lower() \
            or p.name.lower() == "gadget":
        target = p
        break
if target is None:
    print("app process not found. processes seen:")
    for pid, p in list(procs.items())[:20]:
        print("   %6d %s" % (pid, p.name))
    raise SystemExit(1)

print("attaching to pid %d (%s)" % (target.pid, target.name))
session = dev.attach(target.pid)
script = session.create_script(HOOK)

ctrl_w = {}
lines = []
bulk = [0]


def on_message(msg, data):
    if msg.get("type") != "send":
        return
    m = msg["payload"]
    if m["k"] == "READY":
        print("ready:", m["info"])
    elif m["k"] == "CTRL-W":
        ctrl_w[m["req"]] = ctrl_w.get(m["req"], 0) + 1
        if ctrl_w[m["req"]] <= 3:          # first few of each kind only
            lines.append("  W 0x%02X len=%-3d data=%s" % (m["req"], m["len"], m["data"]))
    elif m["k"] == "BULK":
        bulk[0] += 1


script.on("message", on_message)
script.load()
print("capturing %d s -- start or use the app now" % SECONDS)
time.sleep(SECONDS)

print()
print("=== control transfers seen (first 3 of each request code) ===")
for l in lines:
    print(l)
print()
print("=== summary ===")
print("%-8s %s" % ("request", "count"))
for r in sorted(ctrl_w):
    tag = ""
    if r in (0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58):
        tag = "  <-- already known to this project"
    print("  0x%02X  %5d%s" % (r, ctrl_w[r], tag))
print()
print("bulk transfers: %d" % bulk[0])