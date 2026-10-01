using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;

class SeekNanoDriverInstaller
{
    static int Run(string exe, string args)
    {
        Console.WriteLine("> " + Path.GetFileName(exe) + " " + args);
        var psi = new ProcessStartInfo(exe, args)
        {
            UseShellExecute = false,
            WorkingDirectory = Path.GetDirectoryName(exe) ?? "."
        };
        using (var p = Process.Start(psi))
        {
            p.WaitForExit();
            Console.WriteLine("  exit " + p.ExitCode);
            return p.ExitCode;
        }
    }

    static int Main()
    {
        try
        {
            string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";
            string wdi = Path.Combine(here, "wdi-simple.exe");
            if (!File.Exists(wdi))
            {
                Console.Error.WriteLine("wdi-simple.exe missing next to the installer.");
                Console.Write("Press Enter to close... ");
                Console.ReadLine();
                return 2;
            }

            Console.WriteLine("Seek Nano driver install");
            Console.WriteLine("VID 289D  PID 0011  driver WinUSB");
            Console.WriteLine("This replaces any existing binding for this camera.");
            Console.WriteLine();

            // The two composite interfaces only. The parent node
            // USB\VID_289D&PID_0011 must stay unbound - per seeknano.inf it is
            // owned by another (USBIP/VBox) stack and claiming it fights that
            // filter driver, which left both MI_* children dead after the
            // 2026-09-29 bind run. Binding MI_00/MI_01 is what sncap.exe and
            // the transports actually use.
            string[] args = {
                "-n \"Seek Nano Thermal IF0\" -m \"Seek Thermal\" -v 0x289D -p 0x0011 -i 0 -t 0 -l 0",
                "-n \"Seek Nano Thermal IF1\" -m \"Seek Thermal\" -v 0x289D -p 0x0011 -i 1 -t 0 -l 0"
            };
            int worst = 0;
            foreach (var a in args)
            {
                int c = Run(wdi, a);
                if (c != 0 && worst == 0) worst = c;
            }

            Console.WriteLine();
            if (worst == 0)
                Console.WriteLine("Installed. Unplug the camera, plug it back in, then run SeekNano.exe.");
            else
                Console.WriteLine("One or more installs returned " + worst + ". Unplug/replug and try SeekNano.exe anyway.");
            Console.Write("Press Enter to close... ");
            Console.ReadLine();
            // propagate the worst child-install result so callers
            // (scripts, MSI) see a partial failure as a failure
            return worst;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("ERROR: " + e.Message);
            Console.Write("Press Enter to close... ");
            Console.ReadLine();
            return 1;
        }
    }
}
