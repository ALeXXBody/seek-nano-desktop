# Run as Administrator: seeds the WinUSB INF so Windows binds VID 289D PID 0011
# with WinUSB (selective suspend disabled), then re-enumerates.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
pnputil /add-driver "$here\seeknano.inf" /install
Write-Host "`nIf pnputil reported an unsigned-driver error, install via Device Manager:"
Write-Host "  Device Manager -> Seek Thermal (or unknown device) -> Update driver"
Write-Host "  -> Browse my computer -> Let me pick -> Have Disk -> seeknano.inf"
Write-Host "After install, USB\\VID_289D&PID_0011 should appear as 'WinUSB Device'."
Read-Host "Press Enter"
