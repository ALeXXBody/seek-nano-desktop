# SeekNano.exe - Windows viewer

1. Plug the Seek Nano into a USB-C port (any of phone-suitable cables).
Manual driver option — Device Manager: "Update driver → Browse my computer → Let me pick → Have Disk → seeknano.inf".
   select your Seek device, choose **WinUSB / libusb-win32** -> "Install Driver"
   (once per machine).
3. Run `SeekNano.exe`. Hotkeys: `s` start stream, `c` colormap, `p` PNG snapshot,
   space pause, `q` quit.

Protocol implemented: reverse engineered from the Android app (see
`docs/protocol.md`). Built by the `build-release` workflow.
