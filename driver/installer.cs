using System;
using System.Diagnostics;
using System.Reflection;
using System.IO;

class SeekNanoDriverInstaller
{
    static int Main()
    {
        try
        {
            string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";
            string inf = Path.Combine(here, "seeknano.inf");
            if (!File.Exists(inf))
            {
                Console.Error.WriteLine("seeknano.inf not found next to the installer.");
                return 2;
            }

            Console.WriteLine("Installing Seeking Nano WinUSB binding (VID 289D PID 0011)...");
            var psi = new ProcessStartInfo(
                Environment.SystemDirectory + "\\pnputil.exe",
                "/add-driver \"" + inf + "\" /install")
            { UseShellExecute = false, CreateNoWindow = true };
            int exit = 1;
            using (var p = Process.Start(psi))
            {
                p.WaitForExit();
                exit = p.ExitCode;
                Console.WriteLine("pnputil exit code: " + exit);
            }

            if (exit != 0)
            {
                // Unsigned INF refused by policy: route to the signed libusbK path via Zadig
                Console.WriteLine();
                Console.WriteLine("pnputil refused the unsigned INF (normal on Win 11).");
                Console.WriteLine("Opening Zadig instead - its catalog is properly signed.");
                Console.WriteLine();
                Console.WriteLine("IN ZADIG:");
                Console.WriteLine("  1) Options  -> List All Devices");
                Console.WriteLine("  2) Pick the Seek entry with ID USB\\VID_289D&PID_0011");
                Console.WriteLine("     (may appear as 'iAP' or 'com.thermal.pir324.3')");
                Console.WriteLine("  3) Driver:  libusbK   (or WinUSB - both work)");
                Console.WriteLine("  4) Press 'Install Driver' (5 seconds).");
                Console.WriteLine();
                Console.WriteLine("Then unplug and re-plug the camera and run SeekNano.exe.");
                try
                {
                    Process.Start(new ProcessStartInfo("https://zadig.akeo.ie") {
                        UseShellExecute = true });
                }
                catch { /* browser may fail on locked-down machines */ }
            }
            else
            {
                Console.WriteLine("Success - unplug and re-plug the camera now.");
                Console.WriteLine("USB\\VID_289D&PID_0011 should show as 'WinUSB Device'.");
            }
            Console.Write("Press Enter to close... ");
            Console.ReadLine();
            return exit == 0 ? 0 : 0;   // never force-exit non-zero for UX purposes
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("ERROR: " + e.Message);
            return 1;
        }
    }
}
