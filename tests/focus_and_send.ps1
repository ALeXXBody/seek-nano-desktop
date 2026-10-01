# Bring the SeekNano window to the foreground and press keys at it.
# SetForegroundWindow alone fails from a console (Windows foreground lock),
# so this uses the AttachThreadInput trick. Reusable: each shell call is a
# fresh session, so the type has to be defined here rather than inline.
param([string]$Keys = "s", [int]$WaitMs = 1500)

Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FocusSeek {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr p);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool at);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
  public static bool Force(IntPtr t) {
    uint fg = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
    uint me = GetCurrentThreadId();
    AttachThreadInput(me, fg, true);
    ShowWindow(t, 9);
    BringWindowToTop(t);
    bool ok = SetForegroundWindow(t);
    SetFocus(t);
    AttachThreadInput(me, fg, false);
    return ok;
  }
}
"@

$w = Get-Process SeekNano -ErrorAction SilentlyContinue |
     Where-Object { $_.MainWindowTitle } | Select-Object -First 1
if (-not $w) { Write-Host "no SeekNano window found"; exit 1 }
Write-Host ("focus forced: " + [FocusSeek]::Force($w.MainWindowHandle))
Start-Sleep -Milliseconds $WaitMs
foreach ($k in $Keys.ToCharArray()) {
  [System.Windows.Forms.SendKeys]::SendWait($k.ToString())
  Start-Sleep -Milliseconds 400
}
Write-Host ("sent: " + $Keys)