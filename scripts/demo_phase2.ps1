param(
    [int]$TicketId = 2,
    [switch]$Approve
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeployEnv = Join-Path $ProjectRoot "deploy\glpi\.env"

$values = @{}
foreach ($line in Get-Content -LiteralPath $DeployEnv) {
    if ($line -match '^([^#=]+)=(.*)$') {
        $values[$matches[1].Trim()] = $matches[2].Trim()
    }
}

$tokenUrl = "http://127.0.0.1:8090/realms/servicemind/protocol/openid-connect/token"
$analystToken = Invoke-RestMethod -Method Post -Uri $tokenUrl `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{
        grant_type = "password"
        client_id  = "servicemind-api"
        username   = "acme-analyst"
        password   = $values.ACME_ANALYST_PASSWORD
    }

$run = Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8080/v1/servicemind/runs" `
    -Headers @{Authorization = "Bearer $($analystToken.access_token)"} `
    -ContentType "application/json" `
    -Body (@{
        ticket_id    = $TicketId
        goal         = "Analyze the ticket and prepare a private GLPI followup."
        request_write = $true
    } | ConvertTo-Json)

Write-Host "Run: $($run.id)"
Write-Host "Status: $($run.status)"
Write-Host "Action: $($run.action_intent.action_type)"
Write-Host "Action hash: $($run.action_intent.action_hash)"

if (-not $Approve) {
    Write-Host "Run again with -Approve to execute the reviewed action."
    exit 0
}

$approverToken = Invoke-RestMethod -Method Post -Uri $tokenUrl `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{
        grant_type = "password"
        client_id  = "servicemind-api"
        username   = "acme-approver"
        password   = $values.ACME_APPROVER_PASSWORD
    }

$approved = Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8080/v1/servicemind/runs/$($run.id)/approval" `
    -Headers @{Authorization = "Bearer $($approverToken.access_token)"} `
    -ContentType "application/json" `
    -Body (@{
        decision             = "approved"
        expected_action_hash = $run.action_intent.action_hash
        comment              = "Approved by the Phase 2 demo script"
    } | ConvertTo-Json)

Write-Host "Final status: $($approved.status)"
Write-Host "Followup ID: $($approved.result.execution.followup_id)"
Write-Host "Read-back verified: $($approved.result.execution.verified)"
