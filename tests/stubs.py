"""Windows-import stubs sufficient to exercise viewer.py's dev paths on Linux."""
import struct
import numpy as np


class USBError(RuntimeError):
    pass


def make_frame_body(seq=1):
    """Synthetic 177840-byte frame: real header + 342x260 thermal gradient."""
    raw = bytearray(177_840)
    raw[0:2] = b"\x79\x05"
    struct.pack_into("<H", raw, 2, seq)
    vals = np.zeros(342 * 260, dtype=np.uint16)
    yy, xx = np.mgrid[0:260, 0:342]
    prof = np.clip(0x4000 + 2000.0 * (yy / 259.0) + np.sin(np.arange(342) / 12.0) * 600,
                   0, 0xFFFF)
    vals[:] = prof.ravel().astype(np.uint16)
    payload = vals.tobytes()[:177840 - 16]   # header + payload == 177840
    raw[16:16 + len(payload)] = payload
    return bytes(raw)


class FakeEndpoint:
    def __init__(self, addr):
        self.bEndpointAddress = addr
        # pyusb exposes bmAttributes as an int; bulk = 2
        self.bmAttributes = 0x82 if (addr & 0x80) else 0x02
        self.wMaxPacketSize = 512


class FakeInterface(list):
    def __init__(self, number, eps):
        super().__init__(eps)
        self.bInterfaceNumber = number


class FakeConfig(list):
    def __init__(self, value, ifaces):
        super().__init__(ifaces)
        self.bConfigurationValue = value


class FakeDev:
    """Simulates a composite camera dev for the multi-endpoint probe loop."""

    def __init__(self, nframes=6, live_ep=0x81):
        self.cfg = FakeConfig(1, [
            FakeInterface(0, [FakeEndpoint(live_ep), FakeEndpoint(0x01)]),
            FakeInterface(1, [FakeEndpoint(0x82), FakeEndpoint(0x02)]),
        ])
        self.nframes = nframes
        self.seq = 0
        self.reads = []
        self.alt = {0: 0, 1: 0}
        self.claimed = []

    def set_configuration(self):
        pass

    def get_active_configuration(self):
        try:
            return self.cfg
        except Exception:
            raise USBError("no active configuration")

    def set_interface_altsetting(self, iface, alt):
        self.alt[iface] = alt

    def ctrl_transfer(self, reqtype, request, value, index, data, timeout):
        if reqtype == 0xC0:
            return bytes(data)
        return bytes(data)

    def read(self, ep, size, timeout=500):
        ep_addr = ep.bEndpointAddress if not isinstance(ep, int) else ep
        self.reads.append(ep_addr)
        if ep_addr != self._live:
            raise USBError("timed out (silent endpoint 0x%02x)" % ep_addr)
        self.seq += 1
        if self.seq > self.nframes:
            raise USBError("camera stopped (no more fake frames)")
        return make_frame_body(seq=self.seq)

    @property
    def _live(self):
        return 0x81


class FakeUSBCore:
    def __init__(self):
        self.dev = None

    def find(self, **kw):
        return self.dev

    def claim_interface(self, dev, iface):
        dev.claimed.append(iface)

    def dispose_resources(self, dev):
        pass


class FakeUSBUtil:
    @staticmethod
    def find_descriptor(itf, **kw):
        for ep in itf:
            if all(getattr(ep, k, None) == v for k, v in kw.items()):
                return ep
        return None


class FakeWxEvent:
    pass


class FakeWx:
    """sub-set of wx needed to import + build the GUI class (not show it)."""
    Frame = object
    wx_app = None

    def App(self, arg):
        return type("App", (), {"MainLoop": lambda s: None})()

    def __getattr__(self, name):
        return FakeWxEvent

