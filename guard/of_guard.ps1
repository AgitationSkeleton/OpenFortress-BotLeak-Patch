<#
.SYNOPSIS
  Watchdog for Open Fortress dedicated servers running bots around the clock (Windows).

.DESCRIPTION
  Run it every few minutes (e.g. from Task Scheduler). For every srcds.exe whose command line
  contains "-game <Game>" it does three things:

  1. Hung exit ("zombie") recovery. When a 32-bit srcds runs out of address space it exits, and
     during that exit SourceMod can crash while unloading; SourceMod's Accelerator crash handler
     then waits forever for its (already terminated) dump thread. The process never finishes
     exiting, keeps its UDP ports, and TerminateProcess/taskkill cannot remove it, so the next
     launch silently binds the next free port and players can't find the server. A process that
     has exited but still exists is detected here; its stuck thread is terminated (TerminateThread),
     which lets the exit finish and releases the ports and memory. If the process is still there a
     few seconds later, all of its handles are closed from outside (DuplicateHandle +
     DUPLICATE_CLOSE_SOURCE), which at least releases the ports.
  2. Memory recycling. If private bytes exceed -SoftLimitMB and the server reports 0 humans
     (A2S_INFO players - bots), the process is terminated so its launch loop restarts it.
  3. Hard limit. Above -HardLimitMB it is terminated even with humans on (it is about to die).

  Termination uses TerminateProcess, which skips DLL unload, so it cannot hit the hang above.
  The server's .bat launch loop (":srcds ... goto srcds") is expected to start it again.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File of_guard.ps1 -QueryHost 192.0.2.10 -LogPath C:\oflogs\guard.csv
  Add -WhatIf to only report.
#>
param(
    [string]$Game = "open_fortress",
    [Parameter(Mandatory = $true)][string]$QueryHost,   # address the servers answer A2S on (their +ip)
    [int]$SoftLimitMB = 2600,
    [int]$HardLimitMB = 3300,
    [string]$LogPath = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class OfGuardNative {
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool DuplicateHandle(IntPtr srcProc, IntPtr src, IntPtr dstProc, out IntPtr dst, uint access, bool inherit, uint options);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenThread(uint access, bool inherit, uint tid);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool TerminateThread(IntPtr h, uint code);
    // Terminate the remaining (stuck) threads of an already-exited process; the process then finishes exiting.
    public static int TerminateThreads(System.Diagnostics.Process p) {
        int n = 0;
        foreach (System.Diagnostics.ProcessThread t in p.Threads) {
            IntPtr h = OpenThread(0x1 /*THREAD_TERMINATE*/, false, (uint)t.Id);
            if (h == IntPtr.Zero) continue;
            if (TerminateThread(h, 1)) n++;
            CloseHandle(h);
        }
        return n;
    }
    // Close every handle of a process (only used on processes that have already exited).
    public static int CloseAllHandles(int pid) {
        IntPtr hp = OpenProcess(0x40 /*PROCESS_DUP_HANDLE*/, false, pid);
        if (hp == IntPtr.Zero) return -1;
        int n = 0; IntPtr me = GetCurrentProcess();
        for (int h = 4; h < 0x40000; h += 4) {
            IntPtr dup;
            if (DuplicateHandle(hp, new IntPtr(h), me, out dup, 0, false, 2 /*SAME_ACCESS*/)) {
                CloseHandle(dup);
                IntPtr none;
                if (DuplicateHandle(hp, new IntPtr(h), IntPtr.Zero, out none, 0, false, 1 /*CLOSE_SOURCE*/)) n++;
            }
        }
        CloseHandle(hp);
        return n;
    }
}
"@

function Write-Log([string]$msg) {
    $line = "{0},{1}" -f (Get-Date -Format s), $msg
    Write-Output $line
    if ($LogPath) { Add-Content -Path $LogPath -Value $line }
}

# A2S_INFO -> @{players; bots} or $null (handles the 2020+ challenge reply)
function Get-A2SInfo([string]$addr, [int]$port) {
    $udp = New-Object System.Net.Sockets.UdpClient
    try {
        $udp.Client.ReceiveTimeout = 2500
        $udp.Connect($addr, $port)
        $req = [byte[]](0xFF,0xFF,0xFF,0xFF,0x54) + [Text.Encoding]::ASCII.GetBytes("Source Engine Query") + [byte[]](0)
        [void]$udp.Send($req, $req.Length)
        $ep = New-Object System.Net.IPEndPoint([Net.IPAddress]::Any, 0)
        $r = $udp.Receive([ref]$ep)
        if ($r[4] -eq 0x41) {
            $req2 = $req + $r[5..8]
            [void]$udp.Send($req2, $req2.Length)
            $r = $udp.Receive([ref]$ep)
        }
        if ($r[4] -ne 0x49) { return $null }
        $i = 6
        for ($s = 0; $s -lt 4; $s++) { while ($r[$i] -ne 0) { $i++ }; $i++ }   # name, map, folder, game
        $i += 2                                                              # app id
        return @{ players = [int]$r[$i]; bots = [int]$r[$i + 2] }
    } catch {
        return $null
    } finally {
        $udp.Close()
    }
}

$procs = Get-CimInstance Win32_Process -Filter "Name='srcds.exe'" | Where-Object { $_.CommandLine -match ("-game\s+`"?" + [regex]::Escape($Game) + "\b") }
foreach ($cp in $procs) {
    $id = [int]$cp.ProcessId
    $port = if ($cp.CommandLine -match '\+port\s+(\d+)') { [int]$Matches[1] } else { 27015 }
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $p) { continue }

    $exited = $false
    try { $exited = $p.HasExited } catch { }
    if ($exited) {
        if ($WhatIf) { Write-Log "$id,$port,zombie,would terminate stuck thread"; continue }
        $t = [OfGuardNative]::TerminateThreads($p)
        Start-Sleep -Seconds 3
        if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
            $n = [OfGuardNative]::CloseAllHandles($id)
            Write-Log "$id,$port,zombie,terminated $t threads; still present; closed $n handles"
        } else {
            Write-Log "$id,$port,zombie,terminated $t threads; exited"
        }
        continue
    }

    $mb = [int]($p.PrivateMemorySize64 / 1MB)
    if ($mb -lt $SoftLimitMB) { continue }

    $info = Get-A2SInfo $QueryHost $port
    $humans = if ($info) { $info.players - $info.bots } else { -1 }
    $reason = $null
    if ($mb -ge $HardLimitMB) { $reason = "hard limit" }
    elseif ($humans -eq 0) { $reason = "soft limit, empty" }
    if (-not $reason) { Write-Log "$id,$port,$mb MB,humans=$humans,waiting for empty"; continue }
    if ($WhatIf) { Write-Log "$id,$port,$mb MB,humans=$humans,would restart ($reason)"; continue }
    Stop-Process -Id $id -Force
    Write-Log "$id,$port,$mb MB,humans=$humans,restarted ($reason)"
}
