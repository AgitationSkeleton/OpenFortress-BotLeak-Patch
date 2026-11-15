# Technical notes

Source references are to the public Open Fortress source (2024), `src/game/server/bots/`.
The shipped `server.dll` (build 2024-08-08) was checked against it by disassembly: the
function bodies, struct offsets (`m_lastCmd` at +0x64, `m_cmd` at +0x68), allocation sizes
(0x44 for `CUserCmd`, 0x3C for `CDataMemory`) and string literals all match.

## The leaks

### 1. One `CUserCmd` per bot per tick

`SourceBot_RunAll()` (`bot_manager.cpp`) runs `CBot::Update()` for every bot on every tick,
alive or dead:

```cpp
m_cmd = new CUserCmd();               // bot.cpp, CBot::Update
...
PlayerMove( m_cmd );

void CBot::PlayerMove( CUserCmd *cmd )
{
    m_lastCmd = m_cmd;                // previous m_lastCmd is lost here
    ...
    GetHost()->ProcessUsercmds( cmd, 1, 1, 0, false );
}
```

`CBasePlayer::ProcessUsercmds` copies commands by value (`ctx->cmds.AddToTail( ... )`), so
it never takes ownership. Nothing ever deletes `m_cmd` or `m_lastCmd`. Valve's NextBot uses
a stack `CUserCmd` instead (`NextBotPlayer.h`).

At 66.7 ticks/s this is about 240,000 allocations per bot per hour, roughly 19 MB/h per bot
with allocator overhead.

### 2. One `CDataMemory` per lookup miss

```cpp
// bot_component_memory.cpp
CDataMemory *CBotMemory::GetDataMemory( const char *name, bool forceIfNotExists ) const
{
    ...
    if ( !m_DataMemory.IsValidIndex( index ) ) {
        if ( forceIfNotExists )
            return new CDataMemory();     // never stored, never freed
        return NULL;
    }
```

```cpp
// interfaces/ibotmemory.h
#define GetDataMemoryInt(name) GetMemory()->GetDataMemory(name, true)->GetInt()
// ... Float, Vector, String, Entity
```

Every schedule that hasn't started calls `GetDataMemoryInt("NextSchedule")` on every AI
tick (`bot_schedule.cpp`). `CBotVision::LookAround` calls it for `"BlockLookAround"`, and
the decision component calls it in several places. Measured, this is about 70% of the
growth.

### Smaller leaks (not patched)

- `CBot::Spawn` purges the component and schedule maps without deleting their objects,
  including their `CNavPath` / `CNavPathFollower`.
- `IBotSchedule::TaskComplete` removes tasks without deleting them (32 B each).
- `CBotMemory::ForgetData` / `ForgetAllData` and the memory `Reset()` use `Remove` /
  `Purge` instead of deleting.
- `~CTFPlayer` never deletes `m_pBotController`, so every kicked bot leaks its controller.

## Why it only shows with bots running unattended

`bot_manager.cpp` sets the desired bot count to 0 when no humans are present and
`tf_bot_join_after_player` is 1. With hibernation on, the server sleeps when empty. The leak
therefore only runs continuously with a quota, `tf_bot_join_after_player 0` and
`tf_allow_server_hibernation 0`. That is exactly the "bots keep an empty server alive"
setup.

## The binary patch

All injected code is position-independent: relative calls and PC-relative data, so no
relocations are needed and ASLR is unaffected.

- The code caves go in the zero padding at the end of `.text`'s raw data.
- The 4-byte cache slot goes in the zero-filled tail of `.data`'s last page.
- Both sections' VirtualSizes are extended to cover them, and the PE checksum is recomputed.
- The DLL has no CFG (`DllCharacteristics` 0x140).

| | Site | Change |
|---|---|---|
| A | `CBot::PlayerMove`, `mov eax,[ebx+68h]; mov [ebx+64h],eax` (6 bytes) | `call caveA; nop`. caveA: `old = m_lastCmd; m_lastCmd = m_cmd; if (old && old != m_cmd)` call `CUserCmd`'s scalar deleting destructor on `old` (frees through the game's own allocator). It returns with `eax = m_cmd`, as the original code left it. |
| B | `CBotMemory::GetDataMemory` force path, `push 3Ch; call operator new` (7 bytes) | `call caveB; jmp epilogue`. caveB builds one `CDataMemory` (same `operator new` and constructor) the first time, caches it, and returns it on every later call. |
| C | `CBot::CBot`, `mov dword [esi+18h],0` (7 bytes) | `call caveC; nop; nop`. caveC runs the displaced instruction and also zeroes `[esi+64h]` / `[esi+68h]`. |

### Safety checks

- **A:**
  - The instructions after the site reload `eax` (`mov eax,[ebx]`) and reassign `ecx` before using it, and no flags are live across the site, so clobbering `ecx`, `edx` and the flags is harmless.
  - The only other reader of `m_lastCmd` is `Update`'s "AI cannot run" branch, which runs before `PlayerMove` frees anything.
- **B:**
  - The original code also clobbered `ecx` and `edx` through its calls.
  - `ebx`, `esi` and `edi` are preserved.
  - Execution resumes at the function's normal epilogue with the result in `eax`.
  - Every caller that passes `forceIfNotExists = true` is a read-only getter macro, and the getters are pure, so a shared default object returns exactly what a fresh one would.
- **C:** `eax` (`this`, the constructor's return value) is preserved.

## Measurements

Test copies were LAN-only with no addons: 12 bots, `tf_allow_server_hibernation 0`,
`tf_bot_join_after_player 0`, `dm_gump`, no humans. The three copies ran side by side and
differed only in `server.dll`.

| server.dll | Private-bytes slope |
|---|---|
| original | +801 MB/h (r² = 1.000) |
| fix A + C only | +573 MB/h (r² = 1.000) |
| fix A + B + C | +36 MB/h over minutes 2-14, +20 MB/h over minutes 8-14 |

A further 60-minute soak of the patched DLL ran with 12 bots and a map change every 15
minutes: 306 MB to 382 MB, mostly in steps at map loads, with no crash. In production, with
2-8 bots per server, the patched servers stayed far below the unpatched rate. For example,
the 8-bot server grew 28 MB in its first 20 minutes, where the unpatched rate would be about
180 MB.
