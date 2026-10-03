<#
.SYNOPSIS
  QExec local build pipeline. Free, offline-capable, no cloud services.

.DESCRIPTION
  Stages (each must pass; the script stops at the first failure):
    1. lock     - uv.lock is consistent with pyproject.toml (no silent dependency drift)
    2. format   - ruff format --check
    3. lint     - ruff check
    4. types    - mypy --strict on python/qexec
    5. tests    - pytest (synthetic fixtures only; no credentials, no network)
    6. smoke    - end-to-end synthetic CLI run, if the CLI exists

.PARAMETER Offline
  Require dependency installation from the local cache; no dependency network access.

.PARAMETER Fast
  Skip the smoke stage (used by the pre-commit hook).
#>
param([switch]$Fast, [switch]$Offline)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Make uv discoverable in fresh shells after a winget install.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + $env:Path
# Note: `uv sync` downloads the pinned, free packages from PyPI on the FIRST install only (to
# populate the local cache). Use -Offline to require the cache on later runs. The test suite
# itself blocks outbound network connections via tests/conftest.py, so no test can reach the
# network regardless.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if ($Offline) { $env:UV_OFFLINE = '1' }
Remove-Item Env:DATABENTO_API_KEY -ErrorAction SilentlyContinue

function Invoke-Stage([string]$Name, [scriptblock]$Body) {
    Write-Host "==> [$Name]" -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host ("    ok ({0:N1}s)" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

Invoke-Stage 'lock'   { uv lock --check --quiet }
Invoke-Stage 'sync'   { uv sync --locked --quiet }
Invoke-Stage 'format' { uv run --locked ruff format --check python tests }
Invoke-Stage 'lint'   { uv run --locked ruff check python tests }
Invoke-Stage 'types'  { uv run --locked mypy }
if ($Fast) {
    Invoke-Stage 'tests (fast: excludes @slow)' { uv run --locked pytest -m "not slow" }
} else {
    Invoke-Stage 'tests'  { uv run --locked pytest }
}

if (-not $Fast) {
    $cli = Join-Path $root 'python/qexec/cli/main.py'
    if (Test-Path $cli) {
        Invoke-Stage 'smoke' {
            $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
            $out = [IO.Path]::GetFullPath((Join-Path $tempRoot ("qexec-smoke-" + [guid]::NewGuid())))
            try {
                uv run --locked qexec demo --out $out --sessions 2 --quick
            } finally {
                if (Test-Path -LiteralPath $out) {
                    $resolved = (Resolve-Path -LiteralPath $out).Path
                    $expectedParent = $tempRoot.TrimEnd([char[]]@('\', '/'))
                    if ((Split-Path -Parent $resolved) -ne $expectedParent -or
                        (Split-Path -Leaf $resolved) -notlike 'qexec-smoke-*' -or
                        $resolved -ne $out) {
                        throw "Refusing cleanup outside the verified smoke directory: $resolved"
                    }
                    Remove-Item -LiteralPath $resolved -Recurse -Force
                }
            }
        }
    } else {
        Write-Host '==> [smoke] skipped (CLI not present yet)' -ForegroundColor Yellow
    }
}

Write-Host 'PIPELINE PASSED' -ForegroundColor Green
exit 0
