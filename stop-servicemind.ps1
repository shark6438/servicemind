$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
& (Join-Path $ProjectRoot "stop-local.ps1")

docker compose `
    --env-file (Join-Path $ProjectRoot "deploy\glpi\.env") `
    -f (Join-Path $ProjectRoot "deploy\glpi\compose.yaml") `
    stop

