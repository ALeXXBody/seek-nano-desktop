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
            string inf = Path.Combine(here, "seeknano.inf");
            if (!File.Exists(inf))
            {
                Console.Error.WriteLine("seeknano.inf missing next to the installer.");
                Console.Write("Press Enter to close... ");
                Console.ReadLine();
                return 2;
            }

            Console.WriteLine("Seek Nano driver install");
            Console.WriteLine("VID 289D  PID 0011  driver WinUSB (child interfaces only)");
            Console.WriteLine("This replaces any existing binding for this camera's two");
            Console.WriteLine("composite interfaces (MI_00 / MI_01). The parent USB node is");
            Console.WriteLine("deliberately NOT bound - see seeknano.inf.");
            Console.WriteLine();

            // pnputil stages the INF package (the CI-signed seeknano.cat is
            // matched by hash). This used to shell out to wdi-simple.exe,
            // which was never shipped and silently broke every install.
            int worst = Run("pnputil", "/add-driver \"" + inf + "\" /install");
            if (worst == 0)
                Run("pnputil", "/scan-devices");

            Console.WriteLine();
            if (worst == 0)
            {
                Console.WriteLine("Driver package installed. Unplug the camera, plug it back in,");
                Console.WriteLine("then run SeekNano.exe.");
            }
            else
            {
                Console.WriteLine("pnputil returned " + worst + ".");
                Console.WriteLine("If Windows refused to install an unsigned driver:");
                Console.WriteLine("  1. Device Manager -> the 'Seek' device with the yellow mark");
                Console.WriteLine("  2. Update driver -> Browse -> Let me pick -> Have disk            ");
                Console.WriteLine("     -> select seeknano.inf in this folder");
                Console.WriteLine("(that happens because this package is self-signed, not WHQL'd).");
            }
            Console.Write("Press Enter to close... ");
            Console.ReadLine();
            // propagate the result so callers (scripts, MSI) see failure
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
