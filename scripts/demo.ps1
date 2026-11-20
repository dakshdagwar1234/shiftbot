# Why this exists:
# End-to-end interactive demonstration script for Shiftbot.
# Demonstrates clarifying questions, cut-off rejection, escalation, isolation, container restart survival, and manager approval.
# Usage:
#   .\scripts\demo.ps1 [-Fresh]
# Note: Without -Fresh, it assumes a fresh stack with FIXED_NOW=2026-11-20T17:00:00+05:30 is already running.

[CmdletBinding()]
param(
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
$BaseUrl = "http://localhost:8000"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "     SHIFT-CHANGE REQUEST AGENT - LIVE DEMO (STEP 9.1)    " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# -------------------------------------------------------------------------
# Setup / Fresh Stack Handling
# -------------------------------------------------------------------------
if ($Fresh) {
    Write-Host "`n[-Fresh] Resetting stack to fresh state with FIXED_NOW=2026-11-20T17:00:00+05:30..." -ForegroundColor Yellow
    docker compose down -v
    $env:FIXED_NOW = "2026-11-20T17:00:00+05:30"
    docker compose up -d
    Write-Host "Waiting for services to become healthy..." -ForegroundColor Gray
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $h = Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET -TimeoutSec 2
            if ($h.status -eq "ok" -and $h.db -eq "ok") {
                $ready = $true
                break
            }
        } catch {}
    }
    if (-not $ready) {
        Write-Host "FAILED: Stack failed to initialize or report healthy within 30s." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Stack initialized and healthy." -ForegroundColor Green
} else {
    # Ensure service is healthy and tables are initialized before checking freshness
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET -TimeoutSec 2
            if ($h.status -eq "ok" -and $h.db -eq "ok") {
                $ready = $true
                break
            }
        } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        Write-Host "Could not check the database: service /health not ok within 30s." -ForegroundColor Red
        exit 1
    }

    try {
        $rawOut = & docker compose exec -T db psql -U shift -d shift_db -t -A -c "SELECT COUNT(*) FROM shift_requests;" 2>&1
        if ($LASTEXITCODE -ne 0) {
            $errReason = ($rawOut | Out-String).Trim()
            Write-Host "Could not check the database: $errReason" -ForegroundColor Red
            exit 1
        }
        $numericLines = @($rawOut) | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d+$' }
        if (-not $numericLines -or $numericLines.Count -eq 0) {
            $errReason = ($rawOut | Out-String).Trim()
            Write-Host "Could not check the database: no numeric count returned ($errReason)" -ForegroundColor Red
            exit 1
        }
        $lastNumeric = [string]($numericLines[-1])
        $reqCount = [int]$lastNumeric
        if ($reqCount -gt 0) {
            Write-Host "Database is not fresh. Run with -Fresh or reset it first." -ForegroundColor Red
            exit 1
        }
    } catch {
        $shortReason = $_.Exception.Message.Trim()
        Write-Host "Could not check the database: $shortReason" -ForegroundColor Red
        exit 1
    }
}

# -------------------------------------------------------------------------
# Token Generator & Helper Functions
# -------------------------------------------------------------------------
# Read the real secret once directly from the running container environment
$RawSecret = (docker compose exec -T app printenv JWT_SECRET).Trim()
if (-not $RawSecret) {
    Write-Host "FAILED: JWT_SECRET could not be retrieved from container." -ForegroundColor Red
    exit 1
}

function New-DemoToken([string]$companyId, [string]$employeeId, [string]$role) {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $exp = $now + 86400
    $headerJson = '{"alg":"HS256","typ":"JWT"}'
    $payloadJson = "{`"company_id`":`"$companyId`",`"employee_id`":`"$employeeId`",`"role`":`"$role`",`"iat`":$now,`"exp`":$exp}"

    function To-B64Url([byte[]]$b) {
        return [Convert]::ToBase64String($b).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    }

    $h = To-B64Url ([System.Text.Encoding]::UTF8.GetBytes($headerJson))
    $p = To-B64Url ([System.Text.Encoding]::UTF8.GetBytes($payloadJson))
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($RawSecret)
    $sig = To-B64Url ($hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$h.$p")))
    return "$h.$p.$sig"
}

function Format-RedactedToken([string]$tok) {
    if ($tok.Length -gt 12) {
        return "$($tok.Substring(0, 12))..."
    }
    return "$tok..."
}

# Verification tracker
$Checks = [ordered]@{}

# -------------------------------------------------------------------------
# Step 1: GET /health
# -------------------------------------------------------------------------
Write-Host "`n[Step 1] Server Health: Verify FastAPI and PostgreSQL connectivity" -ForegroundColor Yellow
try {
    $h = Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET -TimeoutSec 5
    Write-Host "GET /health -> status: $($h.status), db: $($h.db)" -ForegroundColor Gray
    $step1Pass = ($h.status -eq "ok" -and $h.db -eq "ok")
} catch {
    $step1Pass = $false
    Write-Host "Error checking /health: $_" -ForegroundColor Red
}
$Checks["Step 1 (Health OK)"] = $step1Pass

# -------------------------------------------------------------------------
# Step 2: comp_alpha emp_005 (19:00 drop, 120m away)
# -------------------------------------------------------------------------
Write-Host "`n[Step 2] Clarification & Multi-Turn Booking: emp_005 requests late drop -> clarifies 9 PM -> applied" -ForegroundColor Yellow
$emp005Token = New-DemoToken "comp_alpha" "emp_005" "employee"
Write-Host "emp_005 Token: $(Format-RedactedToken $emp005Token)" -ForegroundColor DarkGray
$emp005Headers = @{
    "Authorization" = "Bearer $emp005Token"
    "Content-Type"  = "application/json"
}
$convId2 = "demo_emp005_" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))

# Turn 1: "can I go a bit late?"
$body2a = @{ conversation_id = $convId2; message = "can I go a bit late?" } | ConvertTo-Json
$r2a = Invoke-WebRequest -Uri "$BaseUrl/chat" -Method POST -Headers $emp005Headers -Body $body2a -UseBasicParsing
$json2a = $r2a.Content | ConvertFrom-Json
Write-Host "Turn 1 Assistant: $($json2a.reply)" -ForegroundColor Cyan
Write-Host "Turn 1 State: awaiting=$($json2a.awaiting)" -ForegroundColor DarkGray
$step2aPass = ($json2a.awaiting -eq "time")

# Turn 2: "9 PM"
$body2b = @{ conversation_id = $convId2; message = "9 PM" } | ConvertTo-Json
$r2b = Invoke-WebRequest -Uri "$BaseUrl/chat" -Method POST -Headers $emp005Headers -Body $body2b -UseBasicParsing
$json2b = $r2b.Content | ConvertFrom-Json
$calls2b = $r2b.Headers["X-LLM-Calls"]
Write-Host "Turn 2 Assistant: $($json2b.reply)" -ForegroundColor Cyan
Write-Host "Turn 2 Headers: X-LLM-Calls = $calls2b" -ForegroundColor DarkGray
$step2bPass = ($json2b.reply -match "confirmed" -and $json2b.reply -match "9:00 PM" -and $calls2b -ne $null)
$Checks["Step 2 (Clarification & 9 PM Booking)"] = ($step2aPass -and $step2bPass)

# -------------------------------------------------------------------------
# Step 3: comp_alpha emp_001 (18:00 drop, 60m away, inside 2h cut-off)
# -------------------------------------------------------------------------
Write-Host "`n[Step 3] Cut-off Policy Enforcement: emp_001 requests 8 PM on 18:00 drop -> rejected, offers manager route only" -ForegroundColor Yellow
$emp001Token = New-DemoToken "comp_alpha" "emp_001" "employee"
Write-Host "emp001 Token: $(Format-RedactedToken $emp001Token)" -ForegroundColor DarkGray
$emp001Headers = @{
    "Authorization" = "Bearer $emp001Token"
    "Content-Type"  = "application/json"
}
$convId3 = "demo_emp001_" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))

$body3 = @{ conversation_id = $convId3; message = "move my drop to 8 PM" } | ConvertTo-Json
$r3 = Invoke-WebRequest -Uri "$BaseUrl/chat" -Method POST -Headers $emp001Headers -Body $body3 -UseBasicParsing
$json3 = $r3.Content | ConvertFrom-Json
Write-Host "Assistant: $($json3.reply)" -ForegroundColor Cyan
Write-Host "State: awaiting=$($json3.awaiting)" -ForegroundColor DarkGray
$step3Pass = ($json3.reply -match "within the 2 hours cut-off" -and $json3.reply -match "manager" -and $json3.awaiting -eq "confirm_offer")
$Checks["Step 3 (Cut-off Rejection)"] = $step3Pass

# -------------------------------------------------------------------------
# Step 4: Same conversation (emp_001 insists -> manager escalation)
# -------------------------------------------------------------------------
Write-Host "`n[Step 4] Human-in-the-Loop Escalation: emp_001 asks to send to manager -> PENDING_APPROVAL and workflow pause" -ForegroundColor Yellow
$body4 = @{ conversation_id = $convId3; message = "send it to my manager" } | ConvertTo-Json
$r4 = Invoke-WebRequest -Uri "$BaseUrl/chat" -Method POST -Headers $emp001Headers -Body $body4 -UseBasicParsing
$json4 = $r4.Content | ConvertFrom-Json
Write-Host "Assistant: $($json4.reply)" -ForegroundColor Cyan
Write-Host "State: awaiting=$($json4.awaiting)" -ForegroundColor DarkGray
$step4Pass = ($json4.reply -match "submitted for manager approval")
$Checks["Step 4 (Escalation to Manager)"] = $step4Pass

# -------------------------------------------------------------------------
# Step 5: Same employee, same conversation ("any update?" on paused thread)
# -------------------------------------------------------------------------
Write-Host "`n[Step 5] Paused Thread Query: emp_001 asks 'any update?' -> returns pending notice with exactly 0 LLM calls" -ForegroundColor Yellow
$body5 = @{ conversation_id = $convId3; message = "any update?" } | ConvertTo-Json
$r5 = Invoke-WebRequest -Uri "$BaseUrl/chat" -Method POST -Headers $emp001Headers -Body $body5 -UseBasicParsing
$json5 = $r5.Content | ConvertFrom-Json
$calls5 = $r5.Headers["X-LLM-Calls"]
Write-Host "Assistant: $($json5.reply)" -ForegroundColor Cyan
Write-Host "Headers: X-LLM-Calls = $calls5" -ForegroundColor DarkGray
$step5Pass = ($json5.reply -match "submitted for manager approval|pending manager approval" -and $calls5 -eq "0")
$Checks["Step 5 (Paused Query 0 LLM Calls)"] = $step5Pass

# -------------------------------------------------------------------------
# Step 6: Isolation (comp_beta manager mgr_beta)
# -------------------------------------------------------------------------
Write-Host "`n[Step 6] Cross-Tenant Isolation: mgr_beta sees empty queue and receives 404 attempting to decide Alpha request" -ForegroundColor Yellow
$mgrBetaToken = New-DemoToken "comp_beta" "mgr_beta" "manager"
Write-Host "mgr_beta Token: $(Format-RedactedToken $mgrBetaToken)" -ForegroundColor DarkGray
$mgrBetaHeaders = @{
    "Authorization" = "Bearer $mgrBetaToken"
    "Content-Type"  = "application/json"
}

# 6a: mgr_beta checks requests
$r6a = Invoke-RestMethod -Uri "$BaseUrl/manager/requests" -Method GET -Headers $mgrBetaHeaders
$betaList = @($r6a.requests)
Write-Host "mgr_beta GET /manager/requests -> Count: $($betaList.Count)" -ForegroundColor Gray
$step6aPass = ($betaList.Count -eq 0)

# Get the alpha request ID from DB
$rawId = (docker compose exec -T db psql -U shift -d shift_db -t -A -c "SELECT request_id FROM shift_requests WHERE company_id = 'comp_alpha' AND employee_id = 'emp_001' AND status = 'PENDING_APPROVAL' ORDER BY request_id DESC LIMIT 1;").Trim()
$alphaReqId = [int]($rawId -split "\s+" | Where-Object { $_ -match "^\d+$" } | Select-Object -Last 1)
Write-Host "Target Alpha Request ID: $alphaReqId" -ForegroundColor Gray

# 6b: mgr_beta tries to decide alpha request
$step6bPass = $false
try {
    $body6b = @{ request_id = $alphaReqId; decision = "approve"; reason = "Cross-tenant attempt" } | ConvertTo-Json
    $r6b = Invoke-WebRequest -Uri "$BaseUrl/manager/decision" -Method POST -Headers $mgrBetaHeaders -Body $body6b -UseBasicParsing
    Write-Host "UNEXPECTED SUCCESS: $r6b" -ForegroundColor Red
} catch {
    $statusCode = [int]$_.Exception.Response.StatusCode
    Write-Host "mgr_beta POST /manager/decision on Alpha request -> HTTP $statusCode (Isolation Enforced)" -ForegroundColor Gray
    if ($statusCode -eq 404) {
        $step6bPass = $true
    }
}
$Checks["Step 6 (Tenant Isolation Enforced)"] = ($step6aPass -and $step6bPass)

# -------------------------------------------------------------------------
# Step 7: Restart app container
# -------------------------------------------------------------------------
Write-Host "`n[Step 7] Restart Resilience: App container restarts mid-pause; PostgresSaver preserves workflow state" -ForegroundColor Yellow
docker compose restart app | Out-Null
Write-Host "Waiting for app service to recover after restart..." -ForegroundColor Gray
$recovered = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h7 = Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET -TimeoutSec 2
        if ($h7.status -eq "ok") {
            $recovered = $true
            break
        }
    } catch {}
}
Write-Host "App recovered after restart: $recovered" -ForegroundColor Gray
$Checks["Step 7 (Restart Survival)"] = $recovered

# -------------------------------------------------------------------------
# Step 8: mgr_alpha lists and approves request
# -------------------------------------------------------------------------
Write-Host "`n[Step 8] Manager Approval: mgr_alpha lists queue, approves request, and resumes paused workflow" -ForegroundColor Yellow
$mgrAlphaToken = New-DemoToken "comp_alpha" "mgr_alpha" "manager"
Write-Host "mgr_alpha Token: $(Format-RedactedToken $mgrAlphaToken)" -ForegroundColor DarkGray
$mgrAlphaHeaders = @{
    "Authorization" = "Bearer $mgrAlphaToken"
    "Content-Type"  = "application/json"
}

$r8a = Invoke-RestMethod -Uri "$BaseUrl/manager/requests" -Method GET -Headers $mgrAlphaHeaders
$alphaList = @($r8a.requests)
Write-Host "mgr_alpha GET /manager/requests -> Count: $($alphaList.Count)" -ForegroundColor Gray
$step8aPass = ($alphaList.Count -ge 1)
$targetItem = $alphaList | Where-Object { $_.employee_id -eq "emp_001" } | Select-Object -Last 1
if ($targetItem) {
    $alphaReqId = [int]$targetItem.request_id
}

$body8b = @{ request_id = $alphaReqId; decision = "approve"; reason = "Approved by Shift Lead" } | ConvertTo-Json
$r8b = Invoke-RestMethod -Uri "$BaseUrl/manager/decision" -Method POST -Headers $mgrAlphaHeaders -Body $body8b
Write-Host "Manager Decision Response: $($r8b | ConvertTo-Json -Compress)" -ForegroundColor Cyan
$step8bPass = ($r8b.status -eq "ok")
$Checks["Step 8 (Manager Approval Succeeded)"] = ($step8aPass -and $step8bPass)

# -------------------------------------------------------------------------
# Step 9: emp_001 checks status
# -------------------------------------------------------------------------
Write-Host "`n[Step 9] Employee Status Resolution: emp_001 queries /chat/status -> confirmed approved and applied for 8:00 PM" -ForegroundColor Yellow
$r9 = Invoke-RestMethod -Uri "$BaseUrl/chat/status?conversation_id=$convId3" -Method GET -Headers $emp001Headers
Write-Host "GET /chat/status -> Status: $($r9.status)" -ForegroundColor Gray
Write-Host "Assistant: $($r9.reply)" -ForegroundColor Green
$step9Pass = ($r9.reply -match "approved" -and $r9.reply -match "8:00 PM")
$Checks["Step 9 (Status Resolved as Approved 8:00 PM)"] = $step9Pass

# -------------------------------------------------------------------------
# Step 10: Database Audit Inspection
# -------------------------------------------------------------------------
Write-Host "`n[Step 10] Database Audit Inspection: Inspect shift_requests and audit_log rows" -ForegroundColor Yellow
Write-Host "--- shift_requests row ---" -ForegroundColor DarkGray
docker compose exec -T db psql -U shift -d shift_db -c "SELECT request_id, company_id, employee_id, requested_slot_time, status, decided_by FROM shift_requests WHERE request_id = $alphaReqId;"
Write-Host "--- audit_log rows ---" -ForegroundColor DarkGray
docker compose exec -T db psql -U shift -d shift_db -c "SELECT id, company_id, actor_id, action, detail FROM audit_log ORDER BY id DESC LIMIT 4;"

# -------------------------------------------------------------------------
# Final Summary and Self-Checking
# -------------------------------------------------------------------------
Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host "                 DEMO SELF-CHECK RESULTS                  " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$allPassed = $true
foreach ($key in $Checks.Keys) {
    $passed = $Checks[$key]
    if ($passed) {
        Write-Host "  [PASS] $key" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $key" -ForegroundColor Red
        $allPassed = $false
    }
}

Write-Host "==========================================================" -ForegroundColor Cyan
if ($allPassed) {
    Write-Host "           DEMO COMPLETED SUCCESSFULLY                    " -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Cyan
    exit 0
} else {
    Write-Host "           DEMO FAILED ON ONE OR MORE CHECKS              " -ForegroundColor Red
    Write-Host "==========================================================" -ForegroundColor Cyan
    exit 1
}
