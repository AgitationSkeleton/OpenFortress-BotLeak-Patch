# Patch an Open Fortress dedicated server's server.dll against the bot memory leak.
#   powershell -ExecutionPolicy Bypass -File apply.ps1 -Bin "C:\path\to\sdk2013\open_fortress\bin"
# Needs Python 3 with pefile (pip install pefile). Keeps the original as server.dll.orig-65a8180d.
# Windows allows renaming a loaded DLL, so this can run while the server is up; the patch takes
# effect when each srcds instance using that folder is next restarted.
param([Parameter(Mandatory)][string]$Bin, [string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$orig = '65A8180DE5480115B317DC1B30A8F29295A1B7A22D6B2E6D7AB42A18DBA041FA'
$dll = Join-Path $Bin 'server.dll'
$bak = Join-Path $Bin 'server.dll.orig-65a8180d'
if (Test-Path $bak) { throw "$bak already exists: already patched? Run rollback.ps1 first." }
if ((Get-FileHash $dll).Hash -ne $orig) { throw "$dll is not the supported build (sha256 65a8180d...). Not patching." }
$tmp = Join-Path $Bin 'server.dll.patching'
& $Python (Join-Path $PSScriptRoot 'patch_server_dll.py') $dll $tmp
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $tmp)) { throw 'patcher failed; nothing changed' }
Rename-Item $dll 'server.dll.orig-65a8180d'
Rename-Item $tmp 'server.dll'
Write-Host "Patched $dll (original kept as server.dll.orig-65a8180d). Restart each server that uses this folder."
