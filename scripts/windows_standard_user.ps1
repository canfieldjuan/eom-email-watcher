# Qualification only: loaded user profile, suspended launch, owned descendant job.
$ErrorActionPreference = 'Stop'
$receipt=Join-Path $env:EW_PROBE_OUTPUT 'native-process-state.json'
[IO.File]::WriteAllText($receipt, '{"account":"'+$env:EW_PROBE_USER+'","phase":"not-started","quiesced":true}')
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
    [StructLayout(LayoutKind.Sequential)]
    struct Accounting {
        public long user,kernel,periodUser,periodKernel;
        public uint faults,total,active,terminated;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long processTime,jobTime; public uint flags;
        public UIntPtr minWorking,maxWorking; public uint activeLimit;
        public UIntPtr affinity; public uint priority,scheduling;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct JobLimits {
        public BasicLimits basic;
        public ulong readOps,writeOps,otherOps,readBytes,writeBytes,otherBytes;
        public UIntPtr processMemory,jobMemory,peakProcessMemory,peakJobMemory;
    }
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool CreateProcessWithLogonW(string user,string domain,string password,
        uint logon,string application,StringBuilder command,uint flags,IntPtr environment,
        string directory,ref Startup startup,out ProcessInfo process);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern uint WaitForSingleObject(IntPtr handle,uint timeout);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job,int kind,ref JobLimits limits,uint size);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job,int kind,out Accounting info,uint size,IntPtr length);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    static uint Active(IntPtr job) {
        Accounting info;
        if (!QueryInformationJobObject(job,1,out info,(uint)Marshal.SizeOf(typeof(Accounting)),IntPtr.Zero))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return info.active;
    }
    static uint Drain(IntPtr job) {
        var deadline=DateTime.UtcNow.AddSeconds(10);
        uint active;
        do { active=Active(job); if (active==0) return 0; System.Threading.Thread.Sleep(20); }
        while (DateTime.UtcNow<deadline);
        return active;
    }
    static void Report(string path,string user,string phase,uint pid,bool quiet,bool signalled,int kill,uint active,uint before=0) {
        System.IO.File.WriteAllText(path,"{\"account\":\""+user+"\",\"phase\":\""+phase+
            "\",\"pid\":"+pid+",\"quiesced\":"+(quiet?"true":"false")+
            ",\"process_signalled\":"+(signalled?"true":"false")+",\"kill_exit\":"+kill+
            ",\"active_processes\":"+active+",\"active_before_cleanup\":"+before+"}");
    }
    public static bool TimeoutQuiesced(int kill,bool signalled) { return kill==0 && signalled; }
    public static bool WholeTreeQuiesced(int kill,bool signalled,uint active) {
        return TimeoutQuiesced(kill,signalled) && active==0;
    }
    public static int Run(string user,string domain,string password,string app,
                          string command,string directory,string receipt,uint timeout) {
        var startup=new Startup(); startup.cb=(uint)Marshal.SizeOf(startup);
        ProcessInfo process;
        Report(receipt,user,"creating",0,false,false,-1,0);
        // No breakaway: membership is established before user code can spawn.
        if (!CreateProcessWithLogonW(user,domain,password,1,app,new StringBuilder(command),
                                    0x08000004,IntPtr.Zero,directory,ref startup,out process)) {
            int error=Marshal.GetLastWin32Error();
            Report(receipt,user,"not-started",0,true,false,-1,0);
            throw new Win32Exception(error);
        }
        IntPtr job=IntPtr.Zero;
        bool assigned=false;
        try {
            job=CreateJobObject(IntPtr.Zero,null);
            if (job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            var limits=new JobLimits(); limits.basic.flags=0x2000; // KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(limits)))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (!AssignProcessToJobObject(job,process.process))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            assigned=true;
            if (ResumeThread(process.thread)==UInt32.MaxValue)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            Report(receipt,user,"active",process.pid,false,false,-1,Active(job));
            uint wait=WaitForSingleObject(process.process,timeout);
            if (wait!=0 && wait!=258) throw new Win32Exception(Marshal.GetLastWin32Error());
            uint code=0;
            if (wait==0 && !GetExitCodeProcess(process.process,out code))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            int kill=0;
            uint before=Active(job);
            if (before!=0 && !TerminateJobObject(job,123)) kill=Marshal.GetLastWin32Error();
            bool signalled=WaitForSingleObject(process.process,10000)==0;
            uint active=Drain(job);
            // This proves descendants only. The scheduled-scope receipt is joined below.
            bool tree=WholeTreeQuiesced(kill,signalled,active);
            Report(receipt,user,wait==258?"timeout-tree":"exited-tree",process.pid,false,signalled,kill,active,before);
            if (!tree) throw new InvalidOperationException("Fixture descendant tree remains live");
            return wait==258?-2:(int)code;
        } finally {
            if (!assigned) TerminateProcess(process.process,123); // Still suspended; no user code ran.
            if (job!=IntPtr.Zero) CloseHandle(job);
            CloseHandle(process.thread); CloseHandle(process.process);
        }
    }
}
'@
function Join-Quiescence($native,$case,$account) {
    return ($native.account -ceq $account -and
        $native.phase -in @('exited-tree','timeout-tree') -and
        $native.process_signalled -is [bool] -and $native.process_signalled -eq $true -and
        $null -ne $native.kill_exit -and $null -ne $native.active_processes -and
        $case.account -ceq $account -and $case.case -ceq 'old-then-new' -and
        $case.phase -ceq 'complete' -and $case.quiesced -is [bool] -and $case.quiesced -eq $true -and
        [QualificationUser]::WholeTreeQuiesced($native.kill_exit,$native.process_signalled,$native.active_processes))
}
$code=[QualificationUser]::Run($env:EW_PROBE_USER,$env:COMPUTERNAME,$env:EW_PROBE_PASSWORD,
    $env:EW_PROBE_PYTHON,$env:EW_PROBE_ARGS,$env:EW_PROBE_OUTPUT,$receipt,[uint32]$env:EW_PROBE_WAIT_MS)
$state=Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
$casePath=Join-Path $env:EW_PROBE_OUTPUT 'old-then-new/case-cleanup.json'
$case=$null
if (Test-Path -LiteralPath $casePath) { $case=Get-Content -LiteralPath $casePath -Raw | ConvertFrom-Json }
$state.quiesced=Join-Quiescence $state $case $env:EW_PROBE_USER
$state.phase=if ($code -eq -2) {if ($state.quiesced) {'timeout-quiesced'} else {'timeout-live'}} else {
    if ($state.quiesced) {'exited'} else {'exited-unproven'}
}
$state | ConvertTo-Json -Compress | Set-Content -LiteralPath $receipt -Encoding UTF8
if ($code -eq -2) { throw 'Standard-user fixture timeout' }
if ($code -ne 0) {
    $log=Join-Path $env:EW_PROBE_OUTPUT 'standard-user.stderr.log'
    if (Test-Path -LiteralPath $log) { [Console]::Error.WriteLine([IO.File]::ReadAllText($log)) }
    throw "Standard-user fixture exit=$code"
}
if (-not $state.quiesced) { throw 'Fixture quiescence unproven' }
