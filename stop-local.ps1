$ErrorActionPreference = "Stop"

$RuntimeDir = Join-Path $PSScriptRoot ".runtime"
foreach ($name in @("service", "streamlit")) {
    $pidFile = Join-Path $RuntimeDir "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }

    $processId = [int](Get-Content -Raw -LiteralPath $pidFile)
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $processId
        Write-Host "Stopped $name (PID $processId)"
    }
    Remove-Item -LiteralPath $pidFile -Force
}

