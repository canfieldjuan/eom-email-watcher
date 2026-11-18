using Microsoft.Diagnostics.Tracing;
using System.Text.Json;

var desktopPid = int.Parse(args[1], System.Globalization.CultureInfo.InvariantCulture);
using var source = new ETWTraceEventSource(args[0]);
var active = new Dictionary<int, EngineProcess>();
var processes = new List<EngineProcess>();
var io = new Dictionary<ulong, PendingIo>();
var traceCpuSamples = 0;
var desktopStarted = false;

source.Kernel.ProcessStart += data =>
{
    if (data.ProcessID == desktopPid && IsNamed(data.ProcessName, "eom-email-watcher-desktop"))
        desktopStarted = true;
    var direct = desktopStarted && data.ParentID == desktopPid;
    if (!IsNamed(data.ProcessName, "eom-mail-engine") || (!direct && !active.ContainsKey(data.ParentID)))
        return;

    var item = new EngineProcess(data.ProcessID, data.ParentID, direct, data.TimeStampRelativeMSec);
    active.Add(item.Pid, item);
    processes.Add(item);
};
source.Kernel.ProcessStop += data =>
{
    if (active.Remove(data.ProcessID, out var item))
        item.StopMs = data.TimeStampRelativeMSec;
    if (data.ProcessID == desktopPid)
        desktopStarted = false;
};
source.Kernel.ImageLoad += data =>
{
    if (active.TryGetValue(data.ProcessID, out var item)
        && IsNamed(Path.GetFileName(data.FileName), "python313.dll"))
        item.PythonImageMs ??= data.TimeStampRelativeMSec;
};
source.Kernel.PerfInfoSample += data =>
{
    traceCpuSamples++;
    if (active.TryGetValue(data.ProcessID, out var item))
        item.CpuSamples++;
};
source.Kernel.FileIOCreate += data => RecordIo(data.ProcessID, data.FileName, "open", data.IrpPtr, 0, data.TimeStampRelativeMSec);
source.Kernel.FileIORead += data => RecordIo(data.ProcessID, data.FileName, "read", data.IrpPtr, data.IoSize, data.TimeStampRelativeMSec);
source.Kernel.FileIOWrite += data => RecordIo(data.ProcessID, data.FileName, "write", data.IrpPtr, data.IoSize, data.TimeStampRelativeMSec);
source.Kernel.FileIOOperationEnd += data =>
{
    if (io.Remove(data.IrpPtr, out var pending))
    {
        pending.Metric.Completed++;
        pending.Metric.MaxCompletionMs = Math.Max(pending.Metric.MaxCompletionMs, data.TimeStampRelativeMSec - pending.StartMs);
    }
};

source.Process();
File.WriteAllText(args[2], JsonSerializer.Serialize(new
{
    DesktopPid = desktopPid,
    KernelEventsLost = source.EventsLost,
    TraceCpuSamples = traceCpuSamples,
    Processes = processes,
}, new JsonSerializerOptions { WriteIndented = true }));
if (!processes.Any(item => item.Direct))
    throw new InvalidDataException("The trace lacks desktop engine process starts");

void RecordIo(int pid, string path, string verb, ulong request, int bytes, double time)
{
    if (!active.TryGetValue(pid, out var item))
        return;
    var category = Classify(path);
    var key = $"{category}/{verb}";
    if (!item.FileIo.TryGetValue(key, out var metric))
    {
        metric = new IoMetric(time);
        item.FileIo.Add(key, metric);
    }
    metric.Count++;
    metric.RequestedBytes += bytes;
    metric.LastMs = time;
    if (request != 0)
        io[request] = new PendingIo(metric, time);
}

static bool IsNamed(string name, string expected) =>
    name.Equals(expected, StringComparison.OrdinalIgnoreCase)
    || name.Equals(expected + ".exe", StringComparison.OrdinalIgnoreCase);

static string Classify(string path)
{
    if (string.IsNullOrEmpty(path))
        return "unresolved";
    var filename = Path.GetFileName(path);
    if (IsNamed(filename, "eom-mail-engine"))
        return "engine_image";
    if (filename.Equals("config.toml", StringComparison.OrdinalIgnoreCase))
        return "fixture_config";
    if (filename.Equals("windows-source-cutover-v1.json", StringComparison.OrdinalIgnoreCase)
        || filename.Equals("windows-installed-program-catalog-v1.txt", StringComparison.OrdinalIgnoreCase))
        return "deployment_metadata";
    if (path.Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
        .Any(segment => segment.StartsWith("_MEI", StringComparison.OrdinalIgnoreCase)))
        return "pyinstaller_temp";
    return "other";
}

sealed record EngineProcess(int Pid, int ParentPid, bool Direct, double StartMs)
{
    public double? StopMs { get; set; }
    public double? PythonImageMs { get; set; }
    public int CpuSamples { get; set; }
    public Dictionary<string, IoMetric> FileIo { get; } = new();
}

sealed record IoMetric(double FirstMs)
{
    public double LastMs { get; set; }
    public int Count { get; set; }
    public long RequestedBytes { get; set; }
    public int Completed { get; set; }
    public double MaxCompletionMs { get; set; }
}

sealed record PendingIo(IoMetric Metric, double StartMs);
