<#
.SYNOPSIS
    从 QQ NT 的 wrapper.node 中定位 nt_sqlite3_key_v2，并通过调试器提取数据库密钥。

.DESCRIPTION
    两步走：

    1) 静态分析（纯 C# 实现，100 MB 的 wrapper.node 约 0.2 秒完成）
       * 解析 PE64 头 / 节表
       * 在 .rdata 中定位 "nt_sqlite3_key_v2: db=%p zDb=%s" 字符串的 RVA
       * 在 .text 中扫描 RIP-relative LEA 指令定位引用它的代码
       * 用异常表（.pdata）确定该指令所属函数的起始 RVA

    2) 动态调试
       * 未运行 QQ 时：以调试方式启动 QQ.exe，在 wrapper.node 加载后
         向函数入口写入 int3 断点，命中后从 R8/R9 读取密钥
       * QQ 已在运行时：自动改为附加模式（-Attach 可强制），不会杀掉你的 QQ

    与旧版的关键差异：
       * CONTEXT 结构按 x64 真实布局读写（旧版结构少 208 字节，GetThreadContext
         会越界写内存，可能导致崩溃）
       * 密钥长度取自 R9（int nKey 参数），不再用"字符串里第一个 0 在哪"猜测
       * 支持超时、附加模式、把密钥写入文件（避免 run_export 解析中文输出）

.PARAMETER WrapperNodePath
    wrapper.node 路径。省略则自动检测 QQ 安装位置。

.PARAMETER NoDebugForKey
    只做静态分析，不启动调试器（用于排查 QQ 版本变化）。

.PARAMETER OutFile
    把密钥（hex）写入指定文件，供 run_export.ps1 读取。

.PARAMETER Attach
    强制附加到已运行的 QQ 进程（默认在检测到 QQ 已运行时自动附加）。

.PARAMETER TimeoutSeconds
    等待断点命中的最长时间，默认 180 秒（附加模式默认 45 秒）。

.PARAMETER AsJson
    以 JSON 输出结果（便于脚本消费）。

.PARAMETER Quiet
    只输出密钥本身。

.PARAMETER QQExePath
    手动指定要启动的 QQ.exe（自动检测失败时使用）。

.PARAMETER ModuleName
    断点所在模块名，默认 wrapper.node（高级选项）。

.PARAMETER FunctionRva
    手动指定函数 RVA，跳过静态分析（高级选项，QQ 版本变化时使用）。

.EXAMPLE
    .\get_key_hex.ps1
    # 自动检测 QQ，提取密钥并打印

.EXAMPLE
    .\get_key_hex.ps1 -NoDebugForKey
    # 只做静态分析，验证当前 QQ 版本的 RVA

.EXAMPLE
    .\get_key_hex.ps1 -OutFile "$env:TEMP\key.txt" -Quiet
    # 把密钥写进文件，控制台只打印 hex
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$WrapperNodePath = '',

    [switch]$NoDebugForKey,

    [string]$OutFile = '',

    [switch]$Attach,

    [int]$TimeoutSeconds = 0,

    [switch]$AsJson,

    [switch]$Quiet,

    # 高级选项：QQ 版本变化导致自动分析失败时手动指定
    [string]$QQExePath = '',

    [string]$ModuleName = 'wrapper.node',

    [uint64]$FunctionRva = 0
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 引入共用函数库
# ---------------------------------------------------------------------------
$qqenvPath = Join-Path $PSScriptRoot 'qqenv.ps1'
if (-not (Test-Path -LiteralPath $qqenvPath)) {
    throw "缺少 qqenv.ps1（应与本脚本在同一目录）: $qqenvPath"
}
. $qqenvPath

# ---------------------------------------------------------------------------
# C# 实现：PE 分析 + 调试器
# ---------------------------------------------------------------------------
if (-not ('QQNTKey.PeAnalyzer' -as [type])) {
    $keySource = @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace QQNTKey
{
    // -----------------------------------------------------------------
    // 静态分析
    // -----------------------------------------------------------------
    public sealed class PeAnalysis
    {
        public ulong FunctionRva;
        public ulong LeaInstructionRva;
        public ulong StringRva;
        public int LeaCandidates;
        public string Sections = "";
        public string Note = "";
    }

    public static class PeAnalyzer
    {
        private sealed class SectionInfo
        {
            public string Name;
            public uint VirtualAddress;
            public uint VirtualSize;
            public uint PointerToRawData;
            public uint SizeOfRawData;
        }

        public static PeAnalysis Analyze(string path, string pattern)
        {
            byte[] data = System.IO.File.ReadAllBytes(path);
            PeAnalysis result = new PeAnalysis();

            if (data.Length < 0x200)
                throw new InvalidOperationException("文件太小，不是有效的 PE 文件");

            // ---- DOS header ----
            if (data[0] != 0x4D || data[1] != 0x5A)
                throw new InvalidOperationException("不是有效的 PE 文件（缺少 MZ 头）");
            int peOffset = BitConverter.ToInt32(data, 0x3C);
            if (peOffset <= 0 || peOffset + 24 >= data.Length)
                throw new InvalidOperationException("PE 头偏移非法");

            // ---- COFF header ----
            if (BitConverter.ToUInt32(data, peOffset) != 0x00004550)
                throw new InvalidOperationException("不是有效的 PE 文件（缺少 PE 签名）");
            int coffOffset = peOffset + 4;
            int sectionCount = BitConverter.ToUInt16(data, coffOffset + 2);
            int optionalSize = BitConverter.ToUInt16(data, coffOffset + 16);
            int optionalOffset = coffOffset + 20;
            ushort magic = BitConverter.ToUInt16(data, optionalOffset);
            if (magic != 0x20B)
                throw new InvalidOperationException("只支持 64 位 PE32+ 文件");

            // ---- data directories ----
            int dataDirOffset = optionalOffset + 112;
            uint exceptionRva = BitConverter.ToUInt32(data, dataDirOffset + 3 * 8);
            uint exceptionSize = BitConverter.ToUInt32(data, dataDirOffset + 3 * 8 + 4);

            // ---- sections ----
            int sectionOffset = optionalOffset + optionalSize;
            List<SectionInfo> sections = new List<SectionInfo>();
            StringBuilder names = new StringBuilder();
            for (int i = 0; i < sectionCount; i++)
            {
                int offset = sectionOffset + i * 40;
                if (offset + 40 > data.Length) break;
                SectionInfo section = new SectionInfo();
                section.Name = Encoding.ASCII.GetString(data, offset, 8).TrimEnd('\0');
                section.VirtualSize = BitConverter.ToUInt32(data, offset + 8);
                section.VirtualAddress = BitConverter.ToUInt32(data, offset + 12);
                section.SizeOfRawData = BitConverter.ToUInt32(data, offset + 16);
                section.PointerToRawData = BitConverter.ToUInt32(data, offset + 20);
                sections.Add(section);
                if (names.Length > 0) names.Append(' ');
                names.Append(section.Name);
            }
            result.Sections = names.ToString();

            SectionInfo rdata = FindSection(sections, ".rdata");
            SectionInfo text = FindSection(sections, ".text");
            if (text == null)
                throw new InvalidOperationException("找不到 .text 节");

            // ---- 1. 在 .rdata（必要时全文件）中查找日志字符串 ----
            byte[] needle = Encoding.ASCII.GetBytes(pattern);
            int patternOffset = -1;
            if (rdata != null)
            {
                patternOffset = IndexOf(data, needle,
                    (int)rdata.PointerToRawData,
                    (int)(rdata.PointerToRawData + rdata.SizeOfRawData));
            }
            if (patternOffset < 0)
            {
                patternOffset = IndexOf(data, needle, 0, data.Length);
                result.Note = "字符串不在 .rdata 中，已改为全文件搜索";
            }
            if (patternOffset < 0)
                throw new InvalidOperationException(
                    "未找到字符串 \"" + pattern + "\"，QQ 版本可能已变化");

            SectionInfo stringSection = FindSectionByOffset(sections, patternOffset);
            if (stringSection == null)
                throw new InvalidOperationException("无法定位字符串所在节");
            ulong stringRva = (ulong)stringSection.VirtualAddress +
                              (ulong)(patternOffset - (int)stringSection.PointerToRawData);
            result.StringRva = stringRva;

            // ---- 2. 在 .text 中查找引用它的 LEA 指令 ----
            List<ulong> candidates = new List<ulong>();
            int textStart = (int)text.PointerToRawData;
            int textEnd = (int)(text.PointerToRawData + text.SizeOfRawData);
            if (textEnd > data.Length) textEnd = data.Length;
            ulong textRva = text.VirtualAddress;

            for (int i = textStart + 1; i + 6 < textEnd; i++)
            {
                if (data[i] != 0x8D) continue;              // LEA
                byte rex = data[i - 1];
                if ((rex & 0xF8) != 0x48) continue;         // REX.W 前缀
                byte modrm = data[i + 1];
                if ((modrm & 0xC7) != 0x05) continue;       // mod=00, rm=101 -> RIP 相对
                int displacement = BitConverter.ToInt32(data, i + 2);
                ulong instructionRva = textRva + (ulong)(i - 1 - textStart);
                long target = (long)instructionRva + 7 + displacement;
                if (target < 0) continue;
                if ((ulong)target == stringRva)
                    candidates.Add(instructionRva);
            }
            result.LeaCandidates = candidates.Count;
            if (candidates.Count == 0)
                throw new InvalidOperationException(
                    "未找到引用该字符串的 LEA 指令（QQ 版本可能已变化）");

            // ---- 3. 用异常表定位函数边界 ----
            SectionInfo pdataSection = FindSectionByRva(sections, exceptionRva);
            List<ulong> functionStarts = new List<ulong>();
            if (pdataSection != null && exceptionSize > 0)
            {
                int entryOffsets = (int)pdataSection.PointerToRawData +
                                   (int)(exceptionRva - pdataSection.VirtualAddress);
                int entryCount = (int)(exceptionSize / 12);
                for (int i = 0; i < entryCount; i++)
                {
                    int offset = entryOffsets + i * 12;
                    if (offset + 12 > data.Length) break;
                    functionStarts.Add(BitConverter.ToUInt32(data, offset));
                }
            }
            functionStarts.Sort();

            ulong chosenLea = 0;
            ulong chosenFunction = 0;
            for (int i = 0; i < candidates.Count; i++)
            {
                ulong functionRva = FindFunction(functionStarts, candidates[i]);
                if (functionRva != 0)
                {
                    chosenLea = candidates[i];
                    chosenFunction = functionRva;
                    break;
                }
            }
            if (chosenFunction == 0)
            {
                chosenLea = candidates[0];
                ulong functionRva = FindFunction(functionStarts, chosenLea);
                if (functionRva != 0) chosenFunction = functionRva;
                else throw new InvalidOperationException(
                    "无法在异常表中定位 LEA 指令所属函数");
            }

            result.LeaInstructionRva = chosenLea;
            result.FunctionRva = chosenFunction;
            return result;
        }

        private static SectionInfo FindSection(List<SectionInfo> sections, string name)
        {
            foreach (SectionInfo section in sections)
            {
                if (string.Equals(section.Name, name, StringComparison.OrdinalIgnoreCase))
                    return section;
            }
            return null;
        }

        private static SectionInfo FindSectionByOffset(List<SectionInfo> sections, int offset)
        {
            foreach (SectionInfo section in sections)
            {
                long start = section.PointerToRawData;
                long end = start + section.SizeOfRawData;
                if (offset >= start && offset < end) return section;
            }
            return null;
        }

        private static SectionInfo FindSectionByRva(List<SectionInfo> sections, ulong rva)
        {
            foreach (SectionInfo section in sections)
            {
                ulong start = section.VirtualAddress;
                ulong end = start + Math.Max(section.VirtualSize, section.SizeOfRawData);
                if (rva >= start && rva < end) return section;
            }
            return null;
        }

        private static int IndexOf(byte[] data, byte[] needle, int start, int end)
        {
            if (start < 0) start = 0;
            if (end > data.Length) end = data.Length;
            int limit = end - needle.Length;
            byte first = needle[0];
            for (int i = start; i <= limit; i++)
            {
                if (data[i] != first) continue;
                int j = 1;
                while (j < needle.Length && data[i + j] == needle[j]) j++;
                if (j == needle.Length) return i;
            }
            return -1;
        }

        private static ulong FindFunction(List<ulong> sortedStarts, ulong rva)
        {
            // 找到 <= rva 的最后一个函数起始地址
            int low = 0;
            int high = sortedStarts.Count - 1;
            ulong best = 0;
            while (low <= high)
            {
                int middle = (low + high) / 2;
                if (sortedStarts[middle] <= rva)
                {
                    best = sortedStarts[middle];
                    low = middle + 1;
                }
                else
                {
                    high = middle - 1;
                }
            }
            return best;
        }
    }

    // -----------------------------------------------------------------
    // 调试器
    // -----------------------------------------------------------------
    internal static class Native
    {
        public const uint DEBUG_ONLY_THIS_PROCESS = 0x00000002;
        public const uint DEBUG_PROCESS = 0x00000001;
        public const uint TH32CS_SNAPMODULE = 0x00000008;
        public const uint TH32CS_SNAPMODULE32 = 0x00000010;
        public const uint THREAD_ALL_ACCESS = 0x1FFFFF;
        public const uint ERROR_SEM_TIMEOUT = 121;

        public const uint EXCEPTION_DEBUG_EVENT = 1;
        public const uint CREATE_THREAD_DEBUG_EVENT = 2;
        public const uint CREATE_PROCESS_DEBUG_EVENT = 3;
        public const uint EXIT_THREAD_DEBUG_EVENT = 4;
        public const uint EXIT_PROCESS_DEBUG_EVENT = 5;
        public const uint LOAD_DLL_DEBUG_EVENT = 6;
        public const uint UNLOAD_DLL_DEBUG_EVENT = 7;
        public const uint OUTPUT_DEBUG_STRING_EVENT = 8;
        public const uint RIP_EVENT = 9;

        public const uint EXCEPTION_BREAKPOINT = 0x80000003;
        public const uint EXCEPTION_SINGLE_STEP = 0x80000004;

        public const uint DBG_CONTINUE = 0x00010002;
        public const uint DBG_EXCEPTION_NOT_HANDLED = 0x80010001;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct STARTUPINFO
        {
            public int cb;
            public IntPtr lpReserved;
            public IntPtr lpDesktop;
            public IntPtr lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public int dwProcessId;
            public int dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct MODULEENTRY32W
        {
            public uint dwSize;
            public uint th32ModuleID;
            public uint th32ProcessID;
            public uint GlblcntUsage;
            public uint ProccntUsage;
            public IntPtr modBaseAddr;
            public uint modBaseSize;
            public IntPtr hModule;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
            public string szModule;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string szExePath;
        }

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool CreateProcess(
            string lpApplicationName,
            IntPtr lpCommandLine,
            IntPtr lpProcessAttributes,
            IntPtr lpThreadAttributes,
            bool bInheritHandles,
            uint dwCreationFlags,
            IntPtr lpEnvironment,
            string lpCurrentDirectory,
            ref STARTUPINFO lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation);

        [DllImport("kernel32.dll", EntryPoint = "WaitForDebugEvent", SetLastError = true)]
        public static extern bool WaitForDebugEventRaw(IntPtr lpDebugEvent, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool ContinueDebugEvent(uint dwProcessId, uint dwThreadId, uint dwContinueStatus);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool DebugActiveProcess(uint dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool DebugActiveProcessStop(uint dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool DebugSetProcessKillOnExit(bool killOnExit);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenThread(uint dwDesiredAccess, bool bInheritHandle, uint dwThreadId);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetThreadContext(IntPtr hThread, IntPtr lpContext);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetThreadContext(IntPtr hThread, IntPtr lpContext);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool ReadProcessMemory(
            IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, IntPtr nSize, out IntPtr lpNumberOfBytesRead);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool WriteProcessMemory(
            IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, IntPtr nSize, out IntPtr lpNumberOfBytesWritten);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool FlushInstructionCache(IntPtr hProcess, IntPtr lpBaseAddress, IntPtr dwSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr CreateToolhelp32Snapshot(uint dwFlags, uint th32ProcessID);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool Module32FirstW(IntPtr hSnapshot, ref MODULEENTRY32W lpme);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool Module32NextW(IntPtr hSnapshot, ref MODULEENTRY32W lpme);
    }

    /// <summary>
    /// x64 CONTEXT 以原始内存方式访问。
    /// 直接声明托管结构极易出错（旧版少算了 208 字节，GetThreadContext 会越界写入），
    /// 因此这里按官方布局的固定偏移读写（总长 1232 字节）。
    /// </summary>
    internal static class Context64
    {
        public const int Size = 1232;
        public const int ContextFlagsOffset = 0x30;   // 48
        public const int EFlagsOffset = 0x44;         // 68
        public const int R8Offset = 0xB8;             // 184
        public const int R9Offset = 0xC0;             // 192
        public const int RipOffset = 0xF8;            // 248
        public const uint CONTEXT_AMD64 = 0x00100000;
        public const uint CONTEXT_CONTROL = CONTEXT_AMD64 | 0x0001;
        public const uint CONTEXT_INTEGER = CONTEXT_AMD64 | 0x0002;
        public const uint CONTEXT_SEGMENTS = CONTEXT_AMD64 | 0x0004;
        public const uint CONTEXT_FLOATING_POINT = CONTEXT_AMD64 | 0x0008;
        public const uint CONTEXT_DEBUG_REGISTERS = CONTEXT_AMD64 | 0x0010;
        public const uint CONTEXT_ALL = CONTEXT_CONTROL | CONTEXT_INTEGER | CONTEXT_SEGMENTS |
                                        CONTEXT_FLOATING_POINT | CONTEXT_DEBUG_REGISTERS;
        public const uint TRAP_FLAG = 0x100;
    }

    public sealed class KeyExtractor : IDisposable
    {
        private readonly string _exePath;
        private readonly string _workingDirectory;
        private readonly string _moduleName;
        private readonly ulong _functionRva;
        private readonly Action<string> _log;
        private readonly Action<string> _verbose;
        private readonly int _timeoutSeconds;

        private IntPtr _hProcess = IntPtr.Zero;
        private Process _processRef;
        private IntPtr _hThread = IntPtr.Zero;
        private IntPtr _eventBuffer = IntPtr.Zero;
        private IntPtr _contextBuffer = IntPtr.Zero;
        private uint _processId;
        private ulong _moduleBase;
        private ulong _breakpointAddress;
        private byte _originalByte;
        private bool _breakpointInstalled;
        private bool _launched;
        private bool _attached;
        private bool _detachRequested;
        private bool _stopRequested;
        private bool _cancelled;
        private readonly HashSet<uint> _steppingThreads = new HashSet<uint>();

        public KeyExtractor(string exePath, string workingDirectory, string moduleName,
                            ulong functionRva, Action<string> log, Action<string> verbose,
                            int timeoutSeconds)
        {
            _exePath = exePath;
            _workingDirectory = workingDirectory;
            _moduleName = string.IsNullOrEmpty(moduleName) ? "wrapper.node" : moduleName;
            _functionRva = functionRva;
            _log = log ?? (s => { });
            _verbose = verbose ?? (s => { });
            _timeoutSeconds = timeoutSeconds > 0 ? timeoutSeconds : 180;
        }

        public string ExtractKey(uint existingProcessId)
        {
            _eventBuffer = Marshal.AllocHGlobal(176);
            _contextBuffer = Marshal.AllocHGlobal(Context64.Size);
            try
            {
                if (existingProcessId != 0)
                {
                    if (!AttachToProcess(existingProcessId))
                        throw new Exception("附加到 QQ 进程失败（错误码 " + Marshal.GetLastWin32Error() +
                                            "），请尝试以管理员身份运行");
                }
                else
                {
                    if (!StartProcess())
                        throw new Exception("启动 QQ 进程失败（错误码 " + Marshal.GetLastWin32Error() + "）");
                }
                return DebugLoop();
            }
            finally
            {
                Cleanup();
            }
        }

        public void Cancel()
        {
            _cancelled = true;
        }

        private bool StartProcess()
        {
            Native.STARTUPINFO startup = new Native.STARTUPINFO();
            startup.cb = Marshal.SizeOf(typeof(Native.STARTUPINFO));
            Native.PROCESS_INFORMATION info;
            if (!Native.CreateProcess(_exePath, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, false,
                                      Native.DEBUG_ONLY_THIS_PROCESS, IntPtr.Zero,
                                      _workingDirectory, ref startup, out info))
                return false;

            _hProcess = info.hProcess;
            _hThread = info.hThread;
            _processId = (uint)info.dwProcessId;
            _launched = true;
            _log("已以调试方式启动 QQ，PID = " + _processId);
            return true;
        }

        private bool AttachToProcess(uint processId)
        {
            if (!Native.DebugActiveProcess(processId))
                return false;
            Native.DebugSetProcessKillOnExit(false);
            _processId = processId;
            _attached = true;
            _log("已附加到正在运行的 QQ 进程，PID = " + processId);
            _hProcess = OpenProcessForRead(processId);
            return true;
        }

        private IntPtr OpenProcessForRead(uint processId)
        {
            try
            {
                // 必须保留 Process 对象的引用：它一旦被 GC 回收就会关闭句柄，
                // 之后 ReadProcessMemory / WriteProcessMemory 都会失败。
                _processRef = Process.GetProcessById((int)processId);
                return _processRef.Handle;
            }
            catch
            {
                return IntPtr.Zero;
            }
        }

        private string DebugLoop()
        {
            long deadlineTicks = DateTime.UtcNow.Ticks + TimeSpan.FromSeconds(_timeoutSeconds).Ticks;
            string foundKey = null;
            long exitDeadline = 0;

            while (true)
            {
                if (_cancelled)
                {
                    _log("已被取消。");
                    break;
                }
                if (exitDeadline != 0)
                {
                    // 已经拿到密钥，等待进程退出事件，最多再等 5 秒
                    if (DateTime.UtcNow.Ticks > exitDeadline) break;
                }
                else if (DateTime.UtcNow.Ticks > deadlineTicks)
                {
                    _log("等待断点超时（" + _timeoutSeconds + " 秒）。");
                    if (_launched && _hProcess != IntPtr.Zero)
                        Native.TerminateProcess(_hProcess, 0);
                    break;
                }

                if (!Native.WaitForDebugEventRaw(_eventBuffer, 500))
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error == (int)Native.ERROR_SEM_TIMEOUT) continue;
                    _verbose("WaitForDebugEvent 返回错误 " + error);
                    continue;
                }

                uint eventCode = (uint)Marshal.ReadInt32(_eventBuffer, 0);
                uint processId = (uint)Marshal.ReadInt32(_eventBuffer, 4);
                uint threadId = (uint)Marshal.ReadInt32(_eventBuffer, 8);
                IntPtr unionOffset = IntPtr.Add(_eventBuffer, 16);
                uint continueStatus = Native.DBG_CONTINUE;

                switch (eventCode)
                {
                    case Native.CREATE_PROCESS_DEBUG_EVENT:
                        CloseEventHandles(unionOffset, 3);
                        TryInstallBreakpoint();
                        break;

                    case Native.CREATE_THREAD_DEBUG_EVENT:
                        break;

                    case Native.EXIT_THREAD_DEBUG_EVENT:
                        break;

                    case Native.LOAD_DLL_DEBUG_EVENT:
                        CloseEventHandles(unionOffset, 1);
                        TryInstallBreakpoint();
                        break;

                    case Native.UNLOAD_DLL_DEBUG_EVENT:
                        break;

                    case Native.OUTPUT_DEBUG_STRING_EVENT:
                        break;

                    case Native.EXCEPTION_DEBUG_EVENT:
                        uint exceptionCode = (uint)Marshal.ReadInt32(unionOffset, 0);
                        ulong exceptionAddress = (ulong)Marshal.ReadInt64(unionOffset, 16);
                        if (exceptionCode == Native.EXCEPTION_BREAKPOINT)
                        {
                            if (_breakpointInstalled && exceptionAddress == _breakpointAddress)
                            {
                                string key = HandleBreakpoint(threadId);
                                if (key != null)
                                {
                                    foundKey = key;
                                    _log("已捕获密钥，正在结束调试会话 ...");
                                    if (_launched && _hProcess != IntPtr.Zero)
                                    {
                                        Native.TerminateProcess(_hProcess, 0);
                                        exitDeadline = DateTime.UtcNow.Ticks +
                                                       TimeSpan.FromSeconds(5).Ticks;
                                    }
                                    else
                                    {
                                        _detachRequested = true;
                                    }
                                }
                            }
                        }
                        else if (exceptionCode == Native.EXCEPTION_SINGLE_STEP)
                        {
                            if (_steppingThreads.Contains(threadId))
                            {
                                _steppingThreads.Remove(threadId);
                                ClearTrapFlag(threadId);
                                InstallBreakpoint();
                            }
                            else
                            {
                                continueStatus = Native.DBG_EXCEPTION_NOT_HANDLED;
                            }
                        }
                        else
                        {
                            continueStatus = Native.DBG_EXCEPTION_NOT_HANDLED;
                        }
                        break;

                    case Native.EXIT_PROCESS_DEBUG_EVENT:
                        _verbose("QQ 进程已退出。");
                        _stopRequested = true;
                        break;

                    case Native.RIP_EVENT:
                        break;
                }

                Native.ContinueDebugEvent(processId, threadId, continueStatus);

                if (_detachRequested)
                {
                    Native.DebugActiveProcessStop(_processId);
                    _attached = false;
                    _stopRequested = true;
                }
                if (_stopRequested) break;
            }

            if (foundKey != null) _log("调试结束，已获得密钥。");
            else _log("调试结束，未获得密钥。");
            return foundKey;
        }

        private static void CloseEventHandles(IntPtr info, int count)
        {
            for (int i = 0; i < count; i++)
            {
                IntPtr handle = Marshal.ReadIntPtr(info, i * IntPtr.Size);
                if (handle != IntPtr.Zero && handle != new IntPtr(-1))
                    Native.CloseHandle(handle);
            }
        }

        private void TryInstallBreakpoint()
        {
            if (_breakpointInstalled) return;
            if (_moduleBase == 0)
            {
                _moduleBase = GetModuleBase(_moduleName);
                if (_moduleBase != 0)
                {
                    _log(_moduleName + " 已加载，基址 0x" + _moduleBase.ToString("X"));
                }
            }
            if (_moduleBase == 0) return;
            InstallBreakpoint();
        }

        private ulong GetModuleBase(string moduleName)
        {
            IntPtr snapshot = Native.CreateToolhelp32Snapshot(
                Native.TH32CS_SNAPMODULE | Native.TH32CS_SNAPMODULE32, _processId);
            if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1)) return 0;
            try
            {
                Native.MODULEENTRY32W entry = new Native.MODULEENTRY32W();
                entry.dwSize = (uint)Marshal.SizeOf(typeof(Native.MODULEENTRY32W));
                if (Native.Module32FirstW(snapshot, ref entry))
                {
                    do
                    {
                        if (string.Equals(entry.szModule, moduleName, StringComparison.OrdinalIgnoreCase))
                            return (ulong)entry.modBaseAddr.ToInt64();
                    }
                    while (Native.Module32NextW(snapshot, ref entry));
                }
            }
            finally
            {
                Native.CloseHandle(snapshot);
            }
            return 0;
        }

        private void InstallBreakpoint()
        {
            if (_breakpointInstalled) return;
            if (_hProcess == IntPtr.Zero)
            {
                _log("无法访问目标进程（可能需要以管理员身份运行）。");
                return;
            }
            _breakpointAddress = _moduleBase + _functionRva;

            byte[] buffer = new byte[1];
            IntPtr read;
            if (!Native.ReadProcessMemory(_hProcess, new IntPtr((long)_breakpointAddress),
                                          buffer, new IntPtr(1), out read) ||
                read.ToInt64() != 1)
            {
                _log("读取函数首字节失败（错误码 " + Marshal.GetLastWin32Error() + "）");
                return;
            }
            _originalByte = buffer[0];
            if (_originalByte == 0xCC)
            {
                _verbose("该位置已有断点，跳过。");
                return;
            }
            if (!WriteByte(_breakpointAddress, 0xCC))
            {
                _log("写入断点失败（错误码 " + Marshal.GetLastWin32Error() + "）");
                return;
            }
            _breakpointInstalled = true;
            _log("已在 0x" + _breakpointAddress.ToString("X") + " 设置断点，等待 QQ 打开数据库 ...");
        }

        private bool WriteByte(ulong address, byte value)
        {
            byte[] buffer = new byte[] { value };
            IntPtr written;
            if (!Native.WriteProcessMemory(_hProcess, new IntPtr((long)address),
                                           buffer, new IntPtr(1), out written) ||
                written.ToInt64() != 1)
                return false;
            Native.FlushInstructionCache(_hProcess, new IntPtr((long)address), new IntPtr(1));
            return true;
        }

        private string HandleBreakpoint(uint threadId)
        {
            IntPtr hThread = Native.OpenThread(Native.THREAD_ALL_ACCESS, false, threadId);
            if (hThread == IntPtr.Zero)
            {
                _verbose("打开线程 " + threadId + " 失败");
                return null;
            }
            try
            {
                ZeroContext();
                Marshal.WriteInt32(_contextBuffer, Context64.ContextFlagsOffset,
                                   (int)Context64.CONTEXT_ALL);
                if (!Native.GetThreadContext(hThread, _contextBuffer))
                {
                    _verbose("GetThreadContext 失败（错误码 " + Marshal.GetLastWin32Error() + "）");
                    return null;
                }

                ulong rip = (ulong)Marshal.ReadInt64(_contextBuffer, Context64.RipOffset);
                ulong r8 = (ulong)Marshal.ReadInt64(_contextBuffer, Context64.R8Offset);
                ulong r9 = (ulong)Marshal.ReadInt64(_contextBuffer, Context64.R9Offset);
                int keyLength = (int)(uint)(r9 & 0xFFFFFFFF);
                _verbose("RIP=0x" + rip.ToString("X") + " R8=0x" + r8.ToString("X") +
                         " R9=" + keyLength);

                string key = null;
                if (r8 > 0x10000)
                {
                    if (keyLength > 0 && keyLength <= 64)
                    {
                        // 正常情况：r9 就是 int nKey 参数
                        byte[] keyBytes = new byte[keyLength];
                        IntPtr read;
                        if (Native.ReadProcessMemory(_hProcess, new IntPtr((long)r8), keyBytes,
                                                     new IntPtr(keyLength), out read) &&
                            read.ToInt64() == keyLength)
                        {
                            key = ToHex(keyBytes);
                        }
                    }
                    else
                    {
                        // 兜底：某些版本可能不以 nKey 传长度，退回到"读到第一个 0 为止"
                        byte[] probe = new byte[64];
                        IntPtr read;
                        if (Native.ReadProcessMemory(_hProcess, new IntPtr((long)r8), probe,
                                                     new IntPtr(probe.Length), out read) &&
                            read.ToInt64() > 0)
                        {
                            int length = Array.IndexOf(probe, (byte)0);
                            if (length < 0) length = (int)read.ToInt64();
                            if (length == 16 || length == 32)
                            {
                                byte[] keyBytes = new byte[length];
                                Array.Copy(probe, keyBytes, length);
                                key = ToHex(keyBytes);
                                _verbose("R9 不是有效长度，改用 0 终止符推断长度 " + length);
                            }
                        }
                    }
                }

                // 恢复原字节并把 RIP 指回被 int3 覆盖的指令
                if (_breakpointInstalled)
                {
                    WriteByte(_breakpointAddress, _originalByte);
                    _breakpointInstalled = false;
                }
                Marshal.WriteInt64(_contextBuffer, Context64.RipOffset, (long)(rip - 1));

                if (key != null)
                {
                    Native.SetThreadContext(hThread, _contextBuffer);
                    return key;
                }

                // 不是目标调用：单步执行一次后重新装回断点
                uint eflags = (uint)Marshal.ReadInt32(_contextBuffer, Context64.EFlagsOffset);
                Marshal.WriteInt32(_contextBuffer, Context64.EFlagsOffset,
                                   (int)(eflags | Context64.TRAP_FLAG));
                if (Native.SetThreadContext(hThread, _contextBuffer))
                    _steppingThreads.Add(threadId);
                return null;
            }
            finally
            {
                Native.CloseHandle(hThread);
            }
        }

        private void ClearTrapFlag(uint threadId)
        {
            IntPtr hThread = Native.OpenThread(Native.THREAD_ALL_ACCESS, false, threadId);
            if (hThread == IntPtr.Zero) return;
            try
            {
                ZeroContext();
                Marshal.WriteInt32(_contextBuffer, Context64.ContextFlagsOffset,
                                   (int)Context64.CONTEXT_ALL);
                if (Native.GetThreadContext(hThread, _contextBuffer))
                {
                    uint eflags = (uint)Marshal.ReadInt32(_contextBuffer, Context64.EFlagsOffset);
                    Marshal.WriteInt32(_contextBuffer, Context64.EFlagsOffset,
                                       (int)(eflags & ~Context64.TRAP_FLAG));
                    Native.SetThreadContext(hThread, _contextBuffer);
                }
            }
            finally
            {
                Native.CloseHandle(hThread);
            }
        }

        private static string ToHex(byte[] bytes)
        {
            StringBuilder builder = new StringBuilder(bytes.Length * 2);
            for (int i = 0; i < bytes.Length; i++)
                builder.Append(bytes[i].ToString("X2"));
            return builder.ToString();
        }

        private void ZeroContext()
        {
            for (int offset = 0; offset < Context64.Size; offset += 8)
                Marshal.WriteInt64(_contextBuffer, offset, 0);
        }

        private void Cleanup()
        {
            // 若还有线程停留在"单步"状态，必须先清掉 TF，否则脱离后该线程会
            // 触发无人处理的单步异常（表现为 QQ 崩溃）。
            if (_steppingThreads.Count > 0)
            {
                List<uint> pending = new List<uint>(_steppingThreads);
                _steppingThreads.Clear();
                foreach (uint threadId in pending)
                {
                    ClearTrapFlag(threadId);
                }
            }
            if (_breakpointInstalled && _hProcess != IntPtr.Zero)
            {
                WriteByte(_breakpointAddress, _originalByte);
                _breakpointInstalled = false;
            }
            if (_launched && _hProcess != IntPtr.Zero)
            {
                Native.TerminateProcess(_hProcess, 0);
            }
            if (_attached)
            {
                Native.DebugActiveProcessStop(_processId);
                _attached = false;
            }
            if (_hThread != IntPtr.Zero)
            {
                Native.CloseHandle(_hThread);
                _hThread = IntPtr.Zero;
            }
            if (_hProcess != IntPtr.Zero)
            {
                // 附加模式下 _hProcess 来自 Process.GetProcessById，不需要手动关闭
                if (_launched) Native.CloseHandle(_hProcess);
                _hProcess = IntPtr.Zero;
            }
            if (_eventBuffer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(_eventBuffer);
                _eventBuffer = IntPtr.Zero;
            }
            if (_contextBuffer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(_contextBuffer);
                _contextBuffer = IntPtr.Zero;
            }
        }

        public void Dispose()
        {
            Cleanup();
        }
    }
}
'@

    Add-Type -TypeDefinition $keySource -Language CSharp
}

# ---------------------------------------------------------------------------
# 定位 wrapper.node / QQ.exe
# ---------------------------------------------------------------------------
$qqInfo = $null
try {
    $qqInfo = Get-QQInstall -WrapperNodePath $WrapperNodePath
} catch {
    Write-QqFail $_.Exception.Message
    throw
}

if (-not $qqInfo -and $QQExePath) {
    if (-not (Test-Path -LiteralPath $QQExePath -PathType Leaf)) {
        throw "指定的 QQ.exe 不存在: $QQExePath"
    }
    $resolvedExe = (Resolve-Path -LiteralPath $QQExePath).Path
    $qqInfo = [pscustomobject]@{
        InstallDir      = (Split-Path -Parent $resolvedExe)
        Version         = ''
        QQExePath       = $resolvedExe
        WrapperNodePath = ''
        Source          = 'explicit'
    }
}

if (-not $qqInfo) {
    throw @'
未能自动检测到 QQ NT 安装目录。
请确认已安装 QQ NT（9.9.x），或用 -WrapperNodePath 手动指定 wrapper.node，例如：
  .\get_key_hex.ps1 -WrapperNodePath "D:\Program Files\Tencent\QQNT\versions\9.9.36-53644\resources\app\wrapper.node"
也可以用 -QQExePath 手动指定 QQ.exe。
'@
}

if ($QQExePath) { $qqInfo.QQExePath = $QQExePath }

if (-not $Quiet -and -not $AsJson) {
    Write-QqBanner 'QQ NT 密钥提取' '静态分析 + 动态调试'
    Write-Host ("  QQ 安装目录 : " + $qqInfo.InstallDir)
    $versionText = '未知'
    if ($qqInfo.Version) { $versionText = $qqInfo.Version }
    Write-Host ("  QQ 版本     : " + $versionText)
    if ($qqInfo.WrapperNodePath) {
        Write-Host ("  wrapper.node: " + $qqInfo.WrapperNodePath)
    }
    Write-Host ''
}

# ---------------------------------------------------------------------------
# 静态分析（或用 -FunctionRva 跳过）
# ---------------------------------------------------------------------------
$pattern = 'nt_sqlite3_key_v2: db=%p zDb=%s'
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

if ($FunctionRva -gt 0) {
    $analysis = [pscustomobject]@{
        FunctionRva       = $FunctionRva
        LeaInstructionRva = [uint64]0
        StringRva         = [uint64]0
        LeaCandidates     = 0
        Note              = '使用 -FunctionRva 手动指定，已跳过静态分析'
    }
} else {
    if (-not $qqInfo.WrapperNodePath) {
        throw '没有 wrapper.node 可供分析。请用 -WrapperNodePath 指定，或用 -FunctionRva 直接给出函数地址。'
    }
    if (-not $Quiet -and -not $AsJson) {
        Write-Host '正在分析 wrapper.node ...' -ForegroundColor Cyan
    }
    $analysis = [QQNTKey.PeAnalyzer]::Analyze($qqInfo.WrapperNodePath, $pattern)
}
$stopwatch.Stop()

$mode = 'debug'
if ($NoDebugForKey) { $mode = 'static' }

$result = [pscustomobject]@{
    FunctionRVA       = $analysis.FunctionRva
    LeaInstructionRVA = $analysis.LeaInstructionRva
    StringRVA         = $analysis.StringRva
    LeaCandidates     = $analysis.LeaCandidates
    WrapperNodePath   = $qqInfo.WrapperNodePath
    QQVersion         = $qqInfo.Version
    Key               = $null
    AnalysisMs        = $stopwatch.ElapsedMilliseconds
    Mode              = $mode
}

if (-not $Quiet -and -not $AsJson) {
    Write-Host ''
    Write-Host '=== 静态分析结果 ===' -ForegroundColor Yellow
    Write-Host ("  字符串 RVA    : 0x{0:X}" -f $analysis.StringRva) -ForegroundColor Green
    Write-Host ("  LEA 指令 RVA  : 0x{0:X}（共 {1} 处引用）" -f $analysis.LeaInstructionRva, $analysis.LeaCandidates) -ForegroundColor Green
    Write-Host ("  函数 RVA      : 0x{0:X}" -f $analysis.FunctionRva) -ForegroundColor Green
    $fileMb = (Get-Item -LiteralPath $qqInfo.WrapperNodePath).Length / 1MB
    Write-Host ("  耗时          : {0} ms（文件 {1:N1} MB）" -f $stopwatch.ElapsedMilliseconds, $fileMb) -ForegroundColor DarkGray
    if ($analysis.Note) { Write-QqWarn $analysis.Note }
}

if ($NoDebugForKey) {
    if ($AsJson) {
        $result | ConvertTo-Json -Depth 4
    } elseif ($Quiet) {
        Write-Output ('0x{0:X}' -f $result.FunctionRVA)
    } else {
        Write-Host ''
        Write-Host '已跳过动态调试（-NoDebugForKey）。' -ForegroundColor DarkGray
    }
    # 结果放进脚本作用域，避免 JSON 输出时又把对象写到管道里
    $script:qqKeyResult = $result
    return
}

# ---------------------------------------------------------------------------
# 动态调试
# ---------------------------------------------------------------------------
$running = @(Get-QQProcess)
$useAttach = [bool]$Attach
$targetPid = [uint32]0

if ($running.Count -gt 0) {
    if ($Attach) {
        $targetPid = [uint32]$running[0].Id
        Write-QqWarn "附加模式：目标 QQ PID $targetPid。"
    } else {
        throw @"
检测到 QQ 正在运行（PID $($running[0].Id)）。
抓取密钥需要由本脚本自己启动 QQ（QQ 会拒绝第三方调试器附加，通常返回错误 31）。
请先完全退出 QQ（右下角托盘图标右键退出），然后重新运行本脚本。

如果确实要尝试附加到正在运行的 QQ，请加 -Attach 参数。
"@
    }
}

if (-not $qqInfo.QQExePath -and -not $useAttach) {
    throw '找不到 QQ.exe，无法启动 QQ。请用 -QQExePath 指定，或先启动 QQ 后用 -Attach 重试。'
}

if ($TimeoutSeconds -le 0) {
    if ($useAttach) { $TimeoutSeconds = 45 } else { $TimeoutSeconds = 180 }
}

if (-not $Quiet -and -not $AsJson) {
    Write-Host ''
    Write-Host '=== 动态调试 ===' -ForegroundColor Yellow
    if ($useAttach) {
        Write-Host "  模式：附加到 QQ（PID $targetPid），最长等待 $TimeoutSeconds 秒" -ForegroundColor Cyan
    } else {
        Write-Host "  模式：启动 QQ 并附加调试器，最长等待 $TimeoutSeconds 秒" -ForegroundColor Cyan
        Write-Host '  请在弹出的 QQ 窗口中登录目标账号。' -ForegroundColor DarkGray
    }
}

$logAction = [Action[string]] {
    param($message)
    Write-Host ('  ' + $message) -ForegroundColor Cyan
}
$verboseAction = [Action[string]] {
    param($message)
    Write-Verbose ('  ' + $message)
}

$exePath = ''
if ($qqInfo.QQExePath) { $exePath = $qqInfo.QQExePath }

$extractor = New-Object QQNTKey.KeyExtractor(
    $exePath,
    $qqInfo.InstallDir,
    $ModuleName,
    [uint64]$analysis.FunctionRva,
    $logAction,
    $verboseAction,
    $TimeoutSeconds
)

$key = $null
try {
    $key = $extractor.ExtractKey([uint32]$targetPid)
} finally {
    $extractor.Dispose()
}

if (-not $key) {
    $message = '未能捕获密钥。'
    if ($useAttach) {
        $message += "`nQQ 可能已经打开了数据库。请退出 QQ 后重新运行本脚本（脚本会自动启动 QQ），"
        $message += "`n或在 QQ 登录界面重新登录时立刻运行: .\get_key_hex.ps1 -Attach"
    } else {
        $message += "`n请确认 QQ 能正常启动并登录，且版本为 9.9.x。可用 -NoDebugForKey 先验证静态分析。"
    }
    throw $message
}

$result.Key = $key

# 写入文件（run_export 通过文件读取，避免解析中文输出）
if ($OutFile) {
    $directory = Split-Path -Parent $OutFile
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    [System.IO.File]::WriteAllText($OutFile, $key, (New-Object System.Text.UTF8Encoding($false)))
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 4
} elseif ($Quiet) {
    Write-Output $key
} else {
    Write-Host ''
    Write-Host '=== 最终结果 ===' -ForegroundColor Yellow
    Write-Host ("  函数 RVA   : 0x{0:X}" -f $result.FunctionRVA) -ForegroundColor Green
    Write-Host ("  加密密钥   : $key") -ForegroundColor Green
    $keyLength = [int]($key.Length / 2)
    Write-Host ("  密钥长度   : $keyLength 字节") -ForegroundColor Green
    if ($OutFile) {
        Write-Host ("  已写入文件 : $OutFile") -ForegroundColor Green
    }
    Write-Host ''
    Write-Host '  提示：把密钥直接传给 decrypt_db.py 即可，不需要手动转换。' -ForegroundColor DarkGray
    Write-Host ("  例如：python decrypt_db.py <QQ号> $key") -ForegroundColor DarkGray
}

$script:qqKeyResult = $result
return
