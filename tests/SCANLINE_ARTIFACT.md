Diagnostics for the scanline artifact: a full-width horizontal band of
broken colour confined to one or two rows, reported as appearing when hot-spot
detection is enabled.

Every cheap explanation has been eliminated by measurement, not by argument:

  probe_raw_scanline.py      60 frames pulled straight off USB with no viewer
                             running: no scanline artifact. Not the camera or
                             the transfer.
  probe_pipeline_scanline.py a real frame walked through _roi_u16, the 3x3
                             median, _nuc2d and _apply_ffc: clean at every
                             stage. Not the spatial pipeline.
  probe_cursor_artifact.py   cursor parked off the window vs moved onto the
                             canvas: identical. Not the line-profile overlay.
  probe_gain_seam.py         240 frames with 26 gain transitions (gains 1-28):
                             no seam, no dashes. Not a mid-frame gain switch.

The hot-spot overlay is excluded structurally rather than by testing: the
artifact is corrupted IMAGE DATA, and drawing code cannot corrupt pixels.

It has not reproduced on demand, so the viewer now watches for it. Every frame
the pipeline scores its own row profile and writes a compact SCAN line to
seeknano_verbose.log; if a row exceeds SCAN_FAULT_Z (8x the frame's robust
spread) the raw frame is dumped next to the exe as scanline_fault_<n>.raw with a
header naming the frame number, the threshold and the offending row, so the
fault can be studied offline instead of raced against.

Clean live frames sit at z 1.1; the band seen on screen sat far above the
threshold. See tests/test_scanline_watchdog.py for the calibration.

Two traps hit while doing this, recorded so they are not repeated:

  - Screen scraping is unreliable here. The scene changes between samples and
    the window moves, so an A/B captured minutes apart compares two different
    scenes. Everything that survives is measured off the wire or the pipeline.
  - Any artifact count taken while the stream is not running is meaningless:
    recover.py had taken the camera, the app was showing NO SIGNAL, and the
    detector was scoring the idle background's faint texture.