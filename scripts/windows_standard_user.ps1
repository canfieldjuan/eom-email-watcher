# Qualification only. NULL environment delegates construction to the loaded user
# profile, instead of copying the administrator's ProcessStartInfo environment.
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class QualificationUser {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct Startup {
        public uint cb; public string reserved, desktop, title;
        public uint x,y,xSize,ySize,xChars,yChars,fill,flags;
        public ushort show,reservedSize;
        public IntPtr reservedData,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr process,thread; public uint pid,tid; }
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool CreateProcessWithLogonW(string user,string domain,string password,
        uint logon,string application,StringBuilder command,uint flags,IntPtr environment,
        string directory,ref Startup startup,out ProcessInfo process);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern uint WaitForSingleObject(IntPtr handle,uint timeout);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    public static int Run(string user,string domain,string password,string app,
                          string command,string directory) {
        var startup=new Startup(); startup.cb=(uint)Marshal.SizeOf(startup);
        ProcessInfo process;
        // LOGON_WITH_PROFILE, CREATE_NO_WINDOW, OS-built user environment.
        if (!CreateProcessWithLogonW(user,domain,password,1,app,new StringBuilder(command),
                                    0x08000000,IntPtr.Zero,directory,ref startup,out process))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            uint wait=WaitForSingleObject(process.process,600000);
            if (wait==258) {
                var kill=new System.Diagnostics.ProcessStartInfo("taskkill.exe",
                    "/PID "+process.pid+" /T /F");
                kill.UseShellExecute=false;
                using (var child=System.Diagnostics.Process.Start(kill)) {
                    if (!child.WaitForExit(10000)) throw new TimeoutException("Fixture tree cleanup");
                }
                throw new TimeoutException("Standard-user fixture timeout");
            }
            if (wait!=0) throw new Win32Exception(Marshal.GetLastWin32Error());
            uint code;
            if (!GetExitCodeProcess(process.process,out code))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return (int)code;
        } finally { CloseHandle(process.thread); CloseHandle(process.process); }
    }
}
'@
$code=[QualificationUser]::Run($env:EW_PROBE_USER,$env:COMPUTERNAME,$env:EW_PROBE_PASSWORD,
    $env:EW_PROBE_PYTHON,$env:EW_PROBE_ARGS,$env:EW_PROBE_OUTPUT)
if ($code -ne 0) {
    $log=Join-Path $env:EW_PROBE_OUTPUT 'standard-user.stderr.log'
    if (Test-Path -LiteralPath $log) { [Console]::Error.WriteLine([IO.File]::ReadAllText($log)) }
    throw "Standard-user fixture exit=$code"
}
