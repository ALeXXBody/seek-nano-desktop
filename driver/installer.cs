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
            var psi = new ProcessStartInfo("pnputil.exe",
                "/add-driver \"" + inf + "\" /install")
            { UseShellExecute = false, CreateNoWindow = true };
            using (var p = Process.Start(psi))
            {
                p.WaitForExit();
                Console.WriteLine("pnputil exit code: " + p.ExitCode);
            }
            Console.WriteLine();
            Console.WriteLine("If Windows refused the unsigned INF:");
            Console.WriteLine("  1) Device Manager -> Other devices -> Seek Thermal (or unknown)");
            Console.WriteLine("  2) Update driver -> Browse my computer -> Let me pick");
            Console.WriteLine("  3) Have Disk -> select seeknano.inf");
            Console.WriteLine();
            Console.WriteLine("After install, USB\\VID_289D&PID_0011 shows as 'WinUSB Device'.");
            Console.WriteLine("Unplug and re-plug the camera so the driver binds.");
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
