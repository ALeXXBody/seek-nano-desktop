#!/usr/bin/env python3
"""
Seek Nano thermal camera driver (vid=0x289D pid=0x11).

Implements the wire protocol reverse-engineered from a live session of Seek's
own Android app (see docs/protocol.md + artifacts/spytrace-session1.log):

    1. handshake   : W 0x54(0x00,0x00), W 0x3c(0x00,0x00), R 0x3d, W 0x3e(0x08,0x00)
    2. identity    : W 0x55(sel) + R 0x4e  (serial, part-number, fw info)
    3. ROM dump    : W 0x56(cursor: 0x20,00,page_hi,page_lo,0,0) + R 0x58 (64B page)
    4. streaming   : W 0x37(fc 00 04 00), W 0x3c(01 00), R 0x3d read-back
    5. frame pump  : W 0x53(58 5b 01 00) + R 0x35, repeat until EP1 has data
    6. frame read  : bulk EP 0x81 -> 177,840 B  (endpoint delivers 6,840-byte chunks)

Frames: header prefixed 0x79 0x05, then 88,920 uint16 samples (frame/2 bytes).
Exact sensor geometry TBD in calibration test run.
"""
import struct
import time

import usb.core
import usb.util

VID = 0x289D
PID = 0x11
REQ_OUT = 0x40          # bmRequestType: vendor, host->device
REQ_IN = 0xC0           # vendor, device->host

CTRL_IFACE = 0
BULK_EP = 0x81
FRAME_BYTES = 177_840
CHUNK = 6_840
FRAME_SAMPLES = FRAME_BYTES // 2     # 88,920


def u16le(b: bytes) -> int:
    return struct.unpack("<H", b[:2])[0]


def u32le(b: bytes) -> int:
    return struct.unpack("<I", b[:4])[0]


class SeekNano:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.dev = None
        self._claim()

    # ---------------- low-level helpers ----------------

    def control_out(self, request, payload: bytes):
        self.dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)

    def control_in(self, request, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))

    def set_page(self, mode: int, addr: int):
        self.control_out(0x56, bytes([mode, 0]) + struct.pack(
            "<H", addr) + b"\x00\x00")

    def read_page(self, length: int = 64) -> bytes:
        return self.control_in(0x58, length)

    # ---------------- open / close ----------------

    def _claim(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise RuntimeError(
                "Seek Nano not found (vid=0x289d pid=0x11). Is it plugged in "
                "on this machine?")
        if self.dev.is_kernel_driver_active(CTRL_IFACE):
            self.dev.detach_kernel_driver(CTRL_IFACE)
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, CTRL_IFACE)

    def close(self):
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    # ---------------- protocol phases ----------------

    def handshake(self):
        self.control_out(0x54, b"\x00\x00")
        self.control_out(0x3c, b"\x00\x00")
        st = self.control_in(0x3d, 2)
        self.control_out(0x3e, b"\x08\x00")
        if self.verbose:
            print(f"[handshake] status={st.hex()}")

    def chip_id(self) -> str:
        self.set_page(0x08, 0x0602)
        return self.read_page(16).rstrip(b"\x00").decode(errors="replace")

    def fw_info(self) -> bytes:
        return self.control_in(0x36, 12)

    def device_info(self, selector: int = 0x17) -> bytes:
        self.control_out(0x55, struct.pack("<H", selector))
        return self.control_in(0x4e, 64)

    def serial_number(self) -> bytes:
        return bytes(self.device_info(0x17)[:16]).rstrip(b"\x00")

    def part_number(self) -> bytes:
        return bytes(self.device_info(0x17)[16:32]).rstrip(b"\x00")

    def read_rom(self, start_page: int, page_count: int) -> bytes:
        blob = bytearray()
        for p in range(start_page, start_page + page_count):
            self.set_page(0x20, p * 64)
            blob += self.read_page(64)
        return blob

    def model_string(self) -> str:
        blob = self.read_page(0x07, 1)
        return blob.split(b"\x00")[0].decode(errors="replace")

    # ---------------- streaming ----------------

    def start_streaming(self):
        self.control_out(0x37, b"\xfc\x00\x04\x00")
        self.control_out(0x3c, b"\x01\x00")
        st = self.control_in(0x3d, 2)
        if self.verbose:
            print(f"[stream] state read-back={st.hex()} (expect 0100)")

    def pump_once(self, tries: int = 200) -> bool:
        """Kick one frame request + poll; returns True once a frame is ready."""
        for _ in range(tries):
            self.control_out(0x53, bytes([0x58, 0x5b, 0x01, 0x00]))
            rc = self.control_in(0x35, 4)
            if rc != b"\x00\x00\x00\x00":
                return True
        return False

    def read_frame(self) -> bytes:
        """Reads a full 177,840-byte frame from the bulk endpoint."""
        buf = bytearray()
        while len(buf) < FRAME_BYTES:
            chunk = self.dev.read(BULK_EP, CHUNK, 1250)
            buf.extend(chunk)
        return bytes(buf)

    def capture_frame(self, max_poll: int = 300) -> bytes:
        if not self.pump_once(max_poll):
            raise TimeoutError("no frame available after polling")
        return self.read_frame()

    # ---------------- one-shot run ----------------

    def run(self, n_frames: int = 1, dump_rom: int = 0):
        """Returns list of raw frame bytes after full init (like the app)."""
        self.handshake()
        if self.verbose:
            print(f"[chip]   {self.chip_id()}")
            print(f"[model]  {self.model_string()}")
            print(f"[serial] {self.serial_number()}")
            print(f"[fw]     {self.fw_info().hex()}")
        if dump_rom:
            open("rom_dump.bin", "wb").write(self.read_page(0, 64))
        self.start_streaming()
        frames = []
        for _ in range(n_frames):
            try:
                frames.append(self.read_frame())
            except usb.core.USBError:
                continue
        self.close()
        return frames


def strip_header(frame: bytes):
    """Frame = 2 B sync header 0x79 0x05 + frame index + sample rows.
    Returns (samples: list[int], meta). Geometry TBD after hardware run."""
    assert frame[:2] == bytes([0x79, 0x05]), "bad frame header"
    return struct.unpack(f"<{FRAME_SAMPLES}H", frame[2:None]), frame[2:]
