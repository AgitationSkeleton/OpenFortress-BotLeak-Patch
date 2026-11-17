# Open Fortress bot memory leak patch

A small, server-side fix for the memory leak in Open Fortress's built-in bots. On an
unpatched dedicated server running bots around the clock (`tf_bot_quota` set,
`tf_allow_server_hibernation 0`, `tf_bot_join_after_player 0`), memory grows by about
**67 MB per hour per bot** until the 32-bit srcds process runs out of address space and
freezes or crashes. That is about 4.5 hours with 12 bots and 13 hours with 4.

The patch removes about 96% of that growth.

| server.dll | Memory growth, 12 bots, no humans |
|---|---|
| original | +801 MB/h |
| patched | +20 to +38 MB/h (about 2-3 MB/h per bot) |

It has run in production on four Open Fortress servers (FFA, Team DM, Infection,
All-Class DM) with 2-8 bots each.

## Is it safe?

- **Server only.** Only `open_fortress/bin/server.dll` on the server changes. Clients
  never load it, so players need nothing and join exactly as before. No networked data or
  SendTables are touched.
- **VAC.** VAC doesn't apply to the server's game DLL. Nothing on the client changes.
- **SourceMod.** All Windows byte signatures in SourceMod's Open Fortress gamedata still
  match at the same offsets.
- **Small.** 130 bytes of code change. The patcher checks the input file's SHA-256 and the
  exact bytes at every site, and refuses anything else.
- **Reversible.** The original DLL is kept beside the patched one, and `rollback.ps1`
  restores it.

## Supported build

The 32-bit Windows `server.dll` from the current Open Fortress release (Source SDK 2013
Multiplayer dedicated server, PE timestamp 2024-08-08 17:21:06 UTC, 9,293,824 bytes):

    sha256 65a8180de5480115b317dc1b30a8f29295a1b7a22d6b2e6d7ab42a18dba041fa

If your hash differs, the patcher refuses to run. A new Open Fortress build would need its
offsets worked out again, and may already fix the leak. The Linux `server.so` is not
covered.

## Install

You need Python 3 and `pefile`:

```
pip install pefile
```

Then point the script at your server's `open_fortress\bin` folder:

```powershell
powershell -ExecutionPolicy Bypass -File apply.ps1 -Bin "C:\path\to\sdk2013\open_fortress\bin"
```

Restart each srcds instance that uses that folder, ideally while it is empty. Windows lets
you rename a loaded DLL, so you can run the script while the servers are up.

To run the patcher by hand instead:

```
python patch_server_dll.py <original server.dll> <output server.dll>
```

It prints the output's SHA-256. The expected result is `fb78e874...81a644`.

## Roll back

```powershell
powershell -ExecutionPolicy Bypass -File rollback.ps1 -Bin "C:\path\to\sdk2013\open_fortress\bin"
```

Then restart the affected servers.

## What it fixes

See [TECHNICAL.md](TECHNICAL.md) for the full analysis. In short:

1. `CBot::Update` allocates a `new CUserCmd` for every bot on every tick, and nothing frees
   it. The patch frees the previous tick's command in `CBot::PlayerMove`.
2. `CBotMemory::GetDataMemory(name, true)` returns a `new CDataMemory` on every lookup miss.
   Its only callers are read-only getter macros, which never free it, and idle bots hit this
   on every AI tick. This is the larger leak, about 70% of the total. The patch returns one
   shared, never-modified default object instead.
3. The `CBot` constructor now initialises `m_cmd` / `m_lastCmd` to NULL, so fix 1 can never
   free garbage.

Small leaks remain: task objects, components dropped on respawn, and the controllers of
kicked bots. Fixing them properly needs a source change. Until then, recycle servers by
memory use, not just by a daily timer (see below).

## Servers stuck "down" after running out of memory (use the guard)

The patch slows the leak but does not stop it. In production, two patched servers with 4-8
bots still ran out of 32-bit address space (about 3.5 GB private) within 1-2 days. Growth
was well above the 2-3 MB/h per bot measured in the soak test, so a daily restart alone isn't
enough.

When that happens, the server can't be reached afterwards, even after a restart:

1. srcds runs out of memory and starts exiting.
2. While SourceMod unloads, it crashes.
3. SourceMod's crash reporter (the **Accelerator** extension) catches that crash and waits
   forever for its dump thread. Process exit has already killed that thread, so the wait
   never ends.
4. The process never finishes exiting. It keeps its UDP ports, and `taskkill /F` and
   `TerminateProcess` can't remove it (`taskkill` reports "no running instance").
5. A nightly restart starts a new instance, which quietly binds the *next* free port (for
   example 27016 instead of 27015). Players connecting to the usual port reach nothing.

Fixes, in order of importance:

- **Disable Accelerator** on Open Fortress servers. Rename
  `addons/sourcemod/extensions/accelerator.autoload`, then restart. You lose SourceMod crash
  reports, but this hang can't happen any more.
- **Run [`guard/of_guard.ps1`](guard/of_guard.ps1)** every few minutes from Task Scheduler,
  under the same Windows account as the servers. For each `srcds.exe -game open_fortress` it:
  - finds instances stuck as above and terminates their stuck thread, so the exit finishes
    and the ports and memory are freed (it falls back to closing the process's handles from
    outside);
  - terminates an instance once its private bytes pass `-SoftLimitMB` (default 2600) and A2S
    reports no humans, so your launch loop restarts it;
  - terminates it regardless above `-HardLimitMB` (default 3300), because it's about to fail.

  It uses `TerminateProcess`, which skips DLL unload and so can't hit the hang. It expects
  each server to run in a `:srcds ... goto srcds` loop that restarts it.

```powershell
$args = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\path\to\guard\of_guard.ps1" -QueryHost <server ip> -LogPath "C:\path\to\of_guard.csv"'
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument $args
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName OpenFortressGuard -Action $a -Trigger $t `
  -Settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4))
```

Run it once with `-WhatIf` first to see what it would do.

## For the Open Fortress developers

[`source_fix.diff`](source_fix.diff) is the same fix against the public Open Fortress
source, plus proper deletion in `ForgetData` / `ForgetAllData` / memory `Reset`. It
applies cleanly to the 2024 source, but it is **unbuilt and untested**. Treat it as a
starting point.

## License

GPL-3.0, the same as the Open Fortress source this patch modifies. See [LICENSE](LICENSE).
This repository contains no Open Fortress binaries. You patch your own copy.
Open Fortress is by the Open Fortress team; this is an unofficial community fix.
