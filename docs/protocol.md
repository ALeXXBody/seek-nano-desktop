# Seek Nano USB protocol (captured 2026-09-26 by `seekspy` APK)

Original device: **vid=0x289D pid=0x11**, 3 USB interfaces, interface 0 used.
All control ops go over EP0, `bmRequestType` 0x40 (vendor, host to device) or 0xC0
(vendor, device to host), value=0, index=0, 1250 ms default timeout.

## Phase 1 - handshake
```
W 0x54  payload 00 00
W 0x3c  payload 00 00
R 0x3d  -> status (2 B)   (00 00)
W 0x3e  payload 08 00     (frame paging config)
```

## Phase 2 - identity
Selectors written via `W 0x55`:
- `0x17` -> `R 0x4e` returns device-info block,
  ASCII `0xE91608E92116` + `1621A151994A` (serial + part number)
- `0x15` -> `R 0x4e` returns firmware version block
  (`00 00 04 00 44 01 00 01 00 08 00 00 56 01 04 01 02 00 0c 00 40 01 f0 00 0e 00 05 00 01 00 00 ...`)
- `R 0x36` -> 12 B firmware info: `16 00 21 00 e9 00 08 00 16 00 e9 00`
- `R 0x58` (after `W 0x56` mode=080002060000) -> `CQ-DBAX` chip ID string

## Phase 3 - ROM/config dump over 64-byte pages
`W 0x56` payload = 6-byte cursor `[0x20] [00] [page_hi] [page_lo] [00 00]`
then `R 0x58` returns 64 bytes of that page. Contents:
- page 0x0000: marker `31f7` and `1621A151994A` again
- page 0x0700: model string `Compact` + `J31`
- page 0x0800+ : calibration/AGC LUT tables (fp32 data)
- page 0x0902+: timestamp string "8/8/2024 12:23:35 PM"
- page 0x0900 fw: `0000040044010001 ...` (matches `R 0x4e` selector 0x15)

## Phase 4 - enable streaming
```
W 0x37 payload fc 00 04 00
W 0x3c payload 01 00
R 0x3d -> 01 00   (read-back; must reach 01)
```
Then the frame pump, repeated until a frame is internally queued:
```
W 0x53 payload 58 5b 01 00   (= 88856, half frame size, frame request kick)
R 0x35 -> 00 00 00 00        (status; 0 = nothing new yet)
```
and then EP1 (bulk-in) delivers 177,840 B = 26 chunks of 6,840 B.
88,920 = frame size / 2 sample count. Frame starts with header `79 05 ...`,
frame sequence number advances between frames.

## Raw-payload notes
- `R 0x4e` short reads appear periodically as "alive" pings (2C 1B 03 07).
- Full live-session wire trace: `artifacts/spytrace-session1.log`
  (483 CTRL ops + 257 BULK reads, ~10 raw frames).
