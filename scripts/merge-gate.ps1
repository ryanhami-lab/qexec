<#
.SYNOPSIS
  Merge gate: run the full pipeline inside a feature worktree, then merge into main.

.EXAMPLE
  scripts/merge-gate.ps1 -Branch wp1-book
#>
param([Parameter(Mandatory)][string]$Branch)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$wt = Join-Path (Split-Path -Parent $root) ("qexec-wt\" + $Branch)
if (-not (Test-Path $wt)) { throw "worktree not found: $wt" }

Write-Host "==> pipeline in $wt" -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $wt 'scripts\ci.ps1')
if ($LASTEXITCODE -ne 0) { throw "pipeline failed on $Branch; not merging" }

Set-Location $root
git merge --no-ff $Branch -m "Merge $Branch (pipeline passed)"
if ($LASTEXITCODE -ne 0) { throw "merge conflict on $Branch; resolve manually" }

Write-Host "==> pipeline on merged main" -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\ci.ps1')
if ($LASTEXITCODE -ne 0) { throw "main is red after merging $Branch" }
Write-Host "MERGED $Branch" -ForegroundColor Green
