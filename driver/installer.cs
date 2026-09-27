using System;
using System.Diagnostics;
using System.Reflection;
using System.IO;

class SeekNanoDriverInstaller
{
    static int RunTool(string exe, string args, bool hide)
    {
        var psi = new ProcessStartInfo(exe, args)
        { UseShellExecute = false, CreateNoWindow = true };
        using (var p = Process.Start(psi))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }

    static int Main()
    {
        try
        {
            string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";
            string inf = Path.Combine(here, "seeknano.inf");
            string cat = Path.Combine(here, "seeknano.cat");
            string cer = Path.Combine(here, "SeekNanoSigner.cer");
            if (!File.Exists(inf)) { Console.Error.WriteLine("seeknano.inf not next to installer."); return 2; }

            // 1) optional: trust our self-signed signer locally
            if (File.Exists(cat) && File.Exists(cer))
            {
                Console.WriteLine("Driver package is catalog-signed by 'SeekNano Project'.");
                Console.Write("Trust this signing certificate on this machine (root+publisher)? [y/N] ");
                var k = Console.ReadLine();
                if (k != null && k.Trim().Equals("y", StringComparison.OrdinalIgnoreCase))
                {
                    int r1 = RunTool("certutil.exe", "-addstore -f Root \"" + cer + "\"", false);
                    int r2 = RunTool("certutil.exe", "-addstore -f TrustedPublisher \"" + cer + "\"", false);
                    Console.WriteLine("cert store updates: root=" + r1 + " publisher=" + r2);
                    if (r1 != 0 || r2 != 0)
                        Console.WriteLine("Import failed - falling back to manual install.");
                }
                else
                {
                    Console.WriteLine("Skipping trust - Windows will prompt or refuse; use manual path below.");
                }
            }

            // 2) silent install of the INF (works when the catalog is trusted)
            Console.WriteLine("Installing Seeking Nano WinUSB binding (VID 289D PID 0011)...");
            int exit = RunTool(Environment.SystemDirectory + "\\pnputil.exe",
                               "/add-driver \"" + inf + "\" /install", false);
            Console.WriteLine("pnputil exit code: " + exit);
            if (exit == 0)
            {
                Console.WriteLine("SUCCESS - unplug and re-plug the camera now.");
                Console.WriteLine("USB\\VID_289D&PID_0011 now shows as 'WinUSB Device'.");
            }
            else
            {
                Console.WriteLine("pnputil refused the INF/package.");
                Console.WriteLine("Manual path - Device Manager:");
                Console.WriteLine("  1) Other devices -> Seek Thermal (or unknown)");
                Console.WriteLine("  2) Update driver -> Browse -> Let me pick");
                Console.WriteLine("  3) Have Disk -> " + inf);
                try { Process.Start(new ProcessStartInfo("https://zadig.akeo.ie") { UseShellExecute = true }); }
                catch { }
            }
            Console.Write("Press Enter to close... ");
            Console.ReadLine();
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("ERROR: " + e.Message);
            return 1;
        }
    }
}
