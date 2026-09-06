param([int]$TicketId = 2)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeployEnv = Join-Path $ProjectRoot "deploy\glpi\.env"

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Read-LocalEnv([string]$Path) {
    $result = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^([^#=]+)=(.*)$') {
            $result[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $result
}

function Get-Token([string]$Username, [string]$Password) {
    return Invoke-RestMethod -Method Post `
        -Uri "http://127.0.0.1:8090/realms/servicemind/protocol/openid-connect/token" `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{grant_type="password"; client_id="servicemind-api"; username=$Username; password=$Password}
}

function Get-StatusCode($ErrorRecord) {
    $status = $ErrorRecord.Exception.Response.StatusCode
    if ($status -is [int]) { return $status }
    return [int]$status.value__
}

$values = Read-LocalEnv $DeployEnv
$acmeAnalyst = Get-Token "acme-analyst" $values.ACME_ANALYST_PASSWORD
$acmeApprover = Get-Token "acme-approver" $values.ACME_APPROVER_PASSWORD
$globexAnalyst = Get-Token "globex-analyst" $values.GLOBEX_ANALYST_PASSWORD
$acmeHeaders = @{Authorization="Bearer $($acmeAnalyst.access_token)"}
$approverHeaders = @{Authorization="Bearer $($acmeApprover.access_token)"}
$globexHeaders = @{Authorization="Bearer $($globexAnalyst.access_token)"}

$serviceHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health"
Assert-True ($serviceHealth.status -eq "ok") "Service health failed"
$acmeHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/servicemind/glpi/health" -Headers $acmeHeaders
$globexHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/servicemind/glpi/health" -Headers $globexHeaders
Assert-True ($acmeHealth.authenticated -and $acmeHealth.entity_id -eq 1) "Acme GLPI scope failed"
Assert-True ($globexHealth.authenticated -and $globexHealth.entity_id -eq 2) "Globex GLPI scope failed"
Write-Host "PASS identity + tenant-aware GLPI health"

$run = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/runs" `
    -Headers $acmeHeaders -ContentType "application/json" `
    -Body (@{ticket_id=$TicketId; goal="Enterprise Phase 2 verification"; request_write=$true} | ConvertTo-Json)
Assert-True ($run.status -eq "waiting_approval") "Run did not stop for approval"
Assert-True ($run.action_intent.action_hash.Length -eq 64) "Action hash missing"

try {
    Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)" -Headers $globexHeaders | Out-Null
    throw "Cross-tenant run read unexpectedly succeeded"
} catch {
    if ($_.Exception.Message -eq "Cross-tenant run read unexpectedly succeeded") { throw }
    Assert-True ((Get-StatusCode $_) -eq 404) "Cross-tenant denial was not 404"
}
try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)/approval" `
        -Headers $acmeHeaders -ContentType "application/json" `
        -Body (@{decision="approved"; expected_action_hash=$run.action_intent.action_hash} | ConvertTo-Json) | Out-Null
    throw "Analyst approval unexpectedly succeeded"
} catch {
    if ($_.Exception.Message -eq "Analyst approval unexpectedly succeeded") { throw }
    Assert-True ((Get-StatusCode $_) -eq 403) "RBAC denial was not 403"
}
try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)/approval" `
        -Headers $approverHeaders -ContentType "application/json" `
        -Body (@{decision="approved"; expected_action_hash=("0" * 64)} | ConvertTo-Json) | Out-Null
    throw "Changed action approval unexpectedly succeeded"
} catch {
    if ($_.Exception.Message -eq "Changed action approval unexpectedly succeeded") { throw }
    Assert-True ((Get-StatusCode $_) -eq 409) "TOCTOU guard denial was not 409"
}

$approvalBody = @{decision="approved"; expected_action_hash=$run.action_intent.action_hash; comment="Phase 2 enterprise verification"} | ConvertTo-Json
$approved = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)/approval" `
    -Headers $approverHeaders -ContentType "application/json" -Body $approvalBody
$replayed = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)/approval" `
    -Headers $approverHeaders -ContentType "application/json" -Body $approvalBody
Assert-True ($approved.status -eq "succeeded") "Approved run failed"
Assert-True ($approved.result.execution.verified) "GLPI read-back verification failed"
Assert-True ($approved.result.execution.followup_id -eq $replayed.result.execution.followup_id) "Approval replay duplicated the write"
Write-Host "PASS approval gate + action hash + exactly-once verified GLPI write"

$events = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)/events" -Headers $acmeHeaders
Assert-True ($events.Content.Contains("approval.required")) "SSE approval event missing"
Assert-True ($events.Content.Contains("execution.verified")) "SSE execution event missing"
Assert-True ($events.Content.Contains("run.succeeded")) "SSE terminal event missing"
Write-Host "PASS durable SSE event replay"

$timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
$webhookBody = @{tenant_id="11111111-1111-4111-8111-111111111111"; event="updated"; item=@{id=$TicketId; entity=@{id=1}}} | ConvertTo-Json -Compress -Depth 5
$hmac = [System.Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($values.SERVICEMIND_ACME_WEBHOOK_SECRET))
try {
    $signature = [Convert]::ToHexString($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($webhookBody + $timestamp))).ToLowerInvariant()
} finally {
    $hmac.Dispose()
}
$webhookHeaders = @{"X-GLPI-signature"=$signature; "X-GLPI-timestamp"=$timestamp}
$first = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/webhooks/glpi" `
    -Headers $webhookHeaders -ContentType "application/json" -Body $webhookBody
$second = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/servicemind/webhooks/glpi" `
    -Headers $webhookHeaders -ContentType "application/json" -Body $webhookBody
Assert-True ($first.run_id -eq $second.run_id) "Webhook retry created a second run"
Assert-True ($second.duplicate) "Webhook retry was not marked duplicate"
Write-Host "PASS signed webhook + deterministic idempotent run"

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") (Join-Path $PSScriptRoot "verify_phase2_database.py")
if ($LASTEXITCODE -ne 0) { throw "Database controls verification failed" }

& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") (Join-Path $PSScriptRoot "verify_phase2_concurrency.py")
if ($LASTEXITCODE -ne 0) { throw "Concurrent exactly-once verification failed" }

Write-Host "PHASE 2 ENTERPRISE VERIFICATION PASSED"
Write-Host "Run ID: $($run.id)"
Write-Host "Verified GLPI followup ID: $($approved.result.execution.followup_id)"
Write-Host "Webhook run ID: $($first.run_id)"
