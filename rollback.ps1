# Undo apply.ps1: put the original server.dll back.
#   powershell -ExecutionPolicy Bypass -File rollback.ps1 -Bin "C:\path\to\sdk2013\open_fortress\bin"
# Takes effect when each srcds instance using that folder is next restarted.
param([Parameter(Mandatory)][string]$Bin)
$ErrorActionPreference = 'Stop'
$orig = '65A8180DE5480115B317DC1B30A8F29295A1B7A22D6B2E6D7AB42A18DBA041FA'
$dll = Join-Path $Bin 'server.dll'
$bak = Join-Path $Bin 'server.dll.orig-65a8180d'
if (-not (Test-Path $bak)) { throw "no $bak; nothing to roll back" }
if ((Get-FileHash $bak).Hash -ne $orig) { throw "$bak is not the original build" }
if (Test-Path $dll) { Rename-Item $dll ('server.dll.patched-' + (Get-Date -Format 'yyyyMMddHHmmss')) }
Rename-Item $bak 'server.dll'
Write-Host "Original server.dll restored in $Bin. Restart each server that uses this folder."
