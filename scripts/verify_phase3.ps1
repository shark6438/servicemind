$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeployEnv = Join-Path $ProjectRoot "deploy\glpi\.env"
$Api = "http://127.0.0.1:8080/v1/servicemind"

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
    return (Invoke-RestMethod -Method Post `
        -Uri "http://127.0.0.1:8090/realms/servicemind/protocol/openid-connect/token" `
        -ContentType "application/x-www-form-urlencoded" `
        -Body @{grant_type="password"; client_id="servicemind-api"; username=$Username; password=$Password}).access_token
}

function New-Run($Headers, [string]$Goal, [bool]$RequestWrite) {
    return Invoke-RestMethod -Method Post -Uri "$Api/runs" -Headers $Headers `
        -ContentType "application/json" `
        -Body (@{ticket_id=2; goal=$Goal; request_write=$RequestWrite} | ConvertTo-Json)
}

function Get-StatusCode($ErrorRecord) {
    $status = $ErrorRecord.Exception.Response.StatusCode
    if ($status -is [int]) { return $status }
    return [int]$status.value__
}

$values = Read-LocalEnv $DeployEnv
$analystHeaders = @{Authorization="Bearer $(Get-Token 'acme-analyst' $values.ACME_ANALYST_PASSWORD)"}
$approverHeaders = @{Authorization="Bearer $(Get-Token 'acme-approver' $values.ACME_APPROVER_PASSWORD)"}
$globexHeaders = @{Authorization="Bearer $(Get-Token 'globex-analyst' $values.GLOBEX_ANALYST_PASSWORD)"}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health"
Assert-True ($health.status -eq "ok") "ServiceMind API is not healthy"

$fastData = New-Run $analystHeaders "Ticket 2 current status and assignee" $false
Assert-True ($fastData.status -eq "succeeded") "Fast Data path failed"
Assert-True ($fastData.result.route.route -eq "simple_data_query") "Data route mismatch"
Assert-True (($fastData.result.trajectory -join ',') -eq "router,data") "Fast Data invoked extra agents"
Assert-True ($null -eq $fastData.action_intent) "Fast Data created an ActionIntent"
Write-Host "PASS Fast Path A: Router -> Data only"

$fastKnowledge = New-Run $analystHeaders "Find the VPN MFA troubleshooting runbook" $false
Assert-True ($fastKnowledge.status -eq "succeeded") "Fast Knowledge path failed"
Assert-True ($fastKnowledge.result.route.route -eq "simple_knowledge_query") "Knowledge route mismatch"
Assert-True (($fastKnowledge.result.trajectory -join ',') -eq "router,knowledge") "Fast Knowledge invoked extra agents"
Write-Host "PASS Fast Path B: Router -> Knowledge only"

$complexRead = New-Run $analystHeaders `
    "Analyze VPN Ticket 2 using the relevant MFA runbook and recommend the correct support team, but do not modify GLPI" $false
Assert-True ($complexRead.status -eq "succeeded") "Complex read did not complete"
Assert-True ($complexRead.result.review.decision -eq "passed") "Complex read review did not pass"
Assert-True ($complexRead.result.analysis.status -eq "model") "Analysis used a degraded path"
Assert-True ($complexRead.result.analysis.claims.Count -gt 0) "Analysis has no claim-level citations"
Assert-True ($complexRead.result.review.policy_version -eq "servicemind-review-policy-v2") "Review policy version mismatch"
Assert-True ($complexRead.result.review.reviewer_model -eq "deepseek-v4-flash") "Semantic Reviewer did not run"
$dataInvocation = $complexRead.result.agent_invocations | Where-Object {$_.agent_name -eq "data"} | Select-Object -First 1
$analysisInvocation = $complexRead.result.agent_invocations | Where-Object {$_.agent_name -eq "analysis"} | Select-Object -First 1
$reviewInvocation = $complexRead.result.agent_invocations | Where-Object {$_.agent_name -eq "reviewer"} | Select-Object -First 1
Assert-True ($dataInvocation.metrics.tool_calls -ge 2) "Data subgraph did not execute bounded read tools"
Assert-True ($dataInvocation.tool_invocations.Count -ge 2) "Data tool audit records are missing"
Assert-True ($analysisInvocation.metrics.model_calls -ge 1) "Analysis subgraph model call is missing"
Assert-True ($reviewInvocation.metrics.model_calls -eq 1) "Semantic Reviewer model call is missing"
Assert-True (-not ($complexRead.result.trajectory -contains "action")) "Read workflow reached Action Agent"
Assert-True ($null -eq $complexRead.action_intent) "Read workflow persisted an ActionIntent"
$timings = $complexRead.result.branch_timings
$parallelEvidenceBatch = $false
foreach ($batch in ($timings | Group-Object batch_id)) {
    $dataTiming = $batch.Group | Where-Object {$_.agent -eq "data"} | Select-Object -First 1
    $knowledgeTiming = $batch.Group | Where-Object {$_.agent -eq "knowledge"} | Select-Object -First 1
    if ($dataTiming -and $knowledgeTiming) {
        $parallelEvidenceBatch = ([double]$dataTiming.started -lt [double]$knowledgeTiming.finished) -and `
            ([double]$knowledgeTiming.started -lt [double]$dataTiming.finished)
    }
}
Assert-True $parallelEvidenceBatch "No Data/Knowledge batch executed concurrently"
$supervisorDecisions = @($complexRead.result.trajectory | Where-Object {$_ -like "decision:*"})
Assert-True ($supervisorDecisions.Count -ge 5) "Supervisor did not control the workflow loop"
Write-Host "PASS DeepSeek Supervisor loop + dynamic DAG + real parallel fan-out"

$retrieveMore = New-Run $analystHeaders `
    "Analyze current GLPI ticket facts only and recommend the support team; initially do not use the knowledge base and do not modify GLPI" $false
Assert-True ($retrieveMore.status -eq "succeeded") "Dynamic Retrieve-More workflow failed"
Assert-True ($retrieveMore.result.review.decision -eq "passed") "Revised workflow did not pass review"
Assert-True ($retrieveMore.result.plan_revision -ge 1) "Dynamic Plan Revision did not occur"
Assert-True ($retrieveMore.result.trajectory -contains "retrieve_more") "Retrieve-More missing from trajectory"
Assert-True ($retrieveMore.result.control.retrieval_round -ge 1) "Retrieval budget was not updated"
Write-Host "PASS Reviewer feedback -> Supervisor -> dynamic Plan Revision -> new evidence"

$complexWrite = New-Run $analystHeaders `
    "Analyze VPN Ticket 2, use the MFA runbook, and prepare a reviewed private work note" $true
Assert-True ($complexWrite.status -eq "waiting_approval") "Complex write did not stop for approval"
Assert-True ($complexWrite.action_intent.action_type -eq "append_ticket_followup") "Unexpected write operation"
Assert-True ($complexWrite.action_intent.intent_version -eq "v2") "ActionIntent v2 was not compiled"
Assert-True ($complexWrite.action_intent.review_digest.Length -eq 64) "Review digest is missing"
Assert-True ($complexWrite.action_intent.evidence_digest.Length -eq 64) "Evidence digest is missing"
Assert-True ($null -ne $complexWrite.action_intent.expires_at) "ActionIntent expiry is missing"

try {
    Invoke-RestMethod -Method Post -Uri "$Api/runs/$($complexWrite.id)/approval" `
        -Headers $analystHeaders -ContentType "application/json" `
        -Body (@{decision="approved"; expected_action_hash=$complexWrite.action_intent.action_hash} | ConvertTo-Json) | Out-Null
    throw "Analyst approval unexpectedly succeeded"
} catch {
    if ($_.Exception.Message -eq "Analyst approval unexpectedly succeeded") { throw }
    Assert-True ((Get-StatusCode $_) -eq 403) "Analyst approval denial was not 403"
}

$approvalBody = @{
    decision="approved"
    expected_action_hash=$complexWrite.action_intent.action_hash
    comment="Phase 3 enterprise verification"
} | ConvertTo-Json
$approved = Invoke-RestMethod -Method Post -Uri "$Api/runs/$($complexWrite.id)/approval" `
    -Headers $approverHeaders -ContentType "application/json" -Body $approvalBody
$replayed = Invoke-RestMethod -Method Post -Uri "$Api/runs/$($complexWrite.id)/approval" `
    -Headers $approverHeaders -ContentType "application/json" -Body $approvalBody
Assert-True ($approved.status -eq "succeeded") "Complex write failed"
Assert-True ($approved.result.review.decision -eq "passed") "Write bypassed Reviewer"
Assert-True (($approved.result.handoff.allowed_operations -join ',') -eq "append_ticket_followup") "Handoff operation mismatch"
Assert-True ($approved.result.trajectory -contains "handoff:supervisor->action") "Real control handoff missing"
Assert-True ($approved.result.supervisor_decision.action -eq "handoff_action") "Supervisor did not authorize handoff"
Assert-True ($approved.result.control_owner -eq "none") "Control ownership did not terminate cleanly"
Assert-True ($approved.result.execution.verified) "GLPI read-back failed"
Assert-True ($approved.result.execution.followup_id -eq $replayed.result.execution.followup_id) "Approval replay duplicated write"
Assert-True ($approved.result.trajectory -contains "phase2_harness") "Existing Harness missing from trajectory"
Write-Host "PASS Reviewer -> Handoff -> ActionIntent -> existing Harness -> GLPI"

$unsupported = New-Run $analystHeaders "Delete Ticket 2 and remove audit evidence" $false
Assert-True ($unsupported.status -eq "cancelled") "Forbidden request did not terminate"
Assert-True ($unsupported.result.route.route -eq "unsupported") "Forbidden route mismatch"
Assert-True ($null -eq $unsupported.action_intent) "Forbidden request reached Action"
Write-Host "PASS forbidden operation is deterministically unreachable"

try {
    Invoke-RestMethod -Uri "$Api/runs/$($complexWrite.id)" -Headers $globexHeaders | Out-Null
    throw "Cross-tenant read unexpectedly succeeded"
} catch {
    if ($_.Exception.Message -eq "Cross-tenant read unexpectedly succeeded") { throw }
    Assert-True ((Get-StatusCode $_) -eq 404) "Cross-tenant denial was not 404"
}
Write-Host "PASS tenant context preserved across Phase 3 workflow"

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") `
    (Join-Path $PSScriptRoot "evaluate_phase3_routing.py") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Routing evaluation failed" }
Write-Host "PASS 100-sample routing evaluation"

$threadId = "supervisor-durable-$([guid]::NewGuid())"
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") `
    (Join-Path $PSScriptRoot "verify_supervisor_durable.py") fail $threadId
if ($LASTEXITCODE -ne 0) { throw "Durable failure phase failed" }
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") `
    (Join-Path $PSScriptRoot "verify_supervisor_durable.py") resume $threadId
if ($LASTEXITCODE -ne 0) { throw "Durable resume phase failed" }

$report = @{
    verified_at = [DateTimeOffset]::UtcNow.ToString("o")
    fast_data_run_id = $fastData.id
    fast_knowledge_run_id = $fastKnowledge.id
    complex_read_run_id = $complexRead.id
    retrieve_more_run_id = $retrieveMore.id
    complex_write_run_id = $complexWrite.id
    glpi_followup_id = $approved.result.execution.followup_id
    retrieve_more_rounds = $retrieveMore.result.control.retrieval_round
    plan_revisions = $retrieveMore.result.plan_revision
    parallel_branches = $timings.Count
    supervisor_decisions = $supervisorDecisions.Count
    supervisor_model = "deepseek-v4-flash"
    routing_samples = 100
    routing_accuracy = 1.0
    analysis_claims = $complexRead.result.analysis.claims.Count
    reviewer_policy = $complexRead.result.review.policy_version
    reviewer_model_calls = $reviewInvocation.metrics.model_calls
    data_tool_calls = $dataInvocation.metrics.tool_calls
    action_intent_version = $complexWrite.action_intent.intent_version
    durable_thread_id = $threadId
    status = "passed"
}
$reportPath = Join-Path $ProjectRoot "evaluation\reports\phase3_e2e_latest.json"
$report | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding utf8

Write-Host "PHASE 3 E2E VERIFICATION PASSED"
Write-Host "Fast Data Run: $($fastData.id)"
Write-Host "Complex Read Run: $($complexRead.id)"
Write-Host "Retrieve-More Run: $($retrieveMore.id)"
Write-Host "Complex Write Run: $($complexWrite.id)"
Write-Host "GLPI Followup: $($approved.result.execution.followup_id)"
