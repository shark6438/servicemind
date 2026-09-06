$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Streamlit = Join-Path $ProjectRoot ".venv\Scripts\streamlit.exe"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Test-Endpoint([string]$Uri) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

if (-not (Test-Endpoint "http://127.0.0.1:8080/health")) {
    $service = Start-Process -FilePath $Python `
        -ArgumentList (Join-Path $ProjectRoot "src\run_service.py") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $RuntimeDir "service.out.log") `
        -RedirectStandardError (Join-Path $RuntimeDir "service.err.log") `
        -PassThru
    Set-Content -LiteralPath (Join-Path $RuntimeDir "service.pid") -Value $service.Id
}

$apiReady = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if (Test-Endpoint "http://127.0.0.1:8080/health") {
        $apiReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $apiReady) {
    throw "Agent API did not become healthy. See .runtime\service.err.log"
}

if (-not (Test-Endpoint "http://127.0.0.1:8501/_stcore/health")) {
    $ui = Start-Process -FilePath $Streamlit `
        -ArgumentList @(
            "run",
            (Join-Path $ProjectRoot "src\streamlit_app.py"),
            "--server.address=127.0.0.1",
            "--server.port=8501",
            "--browser.gatherUsageStats=false"
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $RuntimeDir "streamlit.out.log") `
        -RedirectStandardError (Join-Path $RuntimeDir "streamlit.err.log") `
        -PassThru
    Set-Content -LiteralPath (Join-Path $RuntimeDir "streamlit.pid") -Value $ui.Id
}

$uiReady = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if (Test-Endpoint "http://127.0.0.1:8501/_stcore/health") {
        $uiReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $uiReady) {
    throw "Streamlit UI did not become healthy. See .runtime\streamlit.err.log"
}

Write-Host "Agent API: http://127.0.0.1:8080/docs"
Write-Host "Streamlit: http://127.0.0.1:8501"
