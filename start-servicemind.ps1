$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$DockerLocalData = "D:\FastAPI\DockerLocalAppData"
$DockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$GlpiCompose = Join-Path $ProjectRoot "deploy\glpi\compose.yaml"
$GlpiEnv = Join-Path $ProjectRoot "deploy\glpi\.env"

# Docker Desktop 4.59 cannot access a stale socket in the original LocalAppData
# directory on this machine. This process-scoped override preserves the old data
# and uses a clean runtime directory instead.
$env:LOCALAPPDATA = $DockerLocalData
$env:DOCKER_CLIENT_TIMEOUT = "5"
New-Item -ItemType Directory -Force -Path $DockerLocalData | Out-Null

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Start-Process -FilePath $DockerDesktop -WindowStyle Hidden
    $dockerReady = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            $dockerReady = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $dockerReady) {
        throw "Docker Desktop did not become ready."
    }
}

docker compose --env-file $GlpiEnv -f $GlpiCompose up -d

Push-Location $ProjectRoot
try {
    & (Join-Path $ProjectRoot ".venv\Scripts\alembic.exe") upgrade head
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    & (Join-Path $ProjectRoot ".venv\Scripts\python.exe") `
        (Join-Path $ProjectRoot "scripts\init_langgraph_postgres.py")
    & (Join-Path $ProjectRoot ".venv\Scripts\python.exe") `
        (Join-Path $ProjectRoot "scripts\seed_phase2.py")
}
finally {
    Pop-Location
}
& (Join-Path $ProjectRoot "start-local.ps1")

Write-Host "GLPI: http://127.0.0.1:8088"
Write-Host "ServiceMind UI: http://127.0.0.1:8501"
Write-Host "ServiceMind API: http://127.0.0.1:8080/docs"
