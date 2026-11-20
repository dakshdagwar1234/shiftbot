# Shiftbot End-to-End Interactive Demonstration Script
# Runs the 9-step realistic story in < 2 minutes against http://localhost:8000

$ErrorActionPreference = "Stop"
$BaseUrl = "http://localhost:8000"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "     SHIFT-CHANGE REQUEST AGENT - LIVE DEMO (9 STEPS)     " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Check server health
try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET -TimeoutSec 5
    if ($health.status -ne "ok") {
        Write-Host "Server health check failed: $($health | ConvertTo-Json -Compress)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Server is UP and healthy on $BaseUrl" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Could not connect to $BaseUrl/health. Make sure 'docker compose up' is running." -ForegroundColor Red
    exit 1
}

# Resolve JWT Secret (default or from .env)
$Secret = "demo-only-secret-change-me"
if (Test-Path ".env") {
    $line = Get-Content ".env" | Where-Object { $_ -match "^JWT_SECRET=(.+)$" } | Select-Object -First 1
    if ($line) {
        $Secret = ($line -split "=", 2)[1].Trim()
    }
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
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($Secret)
    $sig = To-B64Url ($hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$h.$p")))
    return "$h.$p.$sig"
}

function Format-RedactedToken([string]$tok) {
    if ($tok.Length -gt 12) {
        return "$($tok.Substring(0, 12))..."
    }
    return "$tok..."
}

# Ensure clean database state for demo run
try {
    docker compose exec -T db psql -U shift -d shift_db -c "DELETE FROM shift_requests WHERE company_id = 'comp_alpha'; UPDATE roster SET cab_id = 'cab_a_20261120_2000' WHERE employee_id = 'emp_001' AND company_id = 'comp_alpha';" | Out-Null
} catch {}

$ConvId = "demo_conv_" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))

# -------------------------------------------------------------------------
# Step 1: Issue token for Alice (comp_alpha, emp_001, employee)
# -------------------------------------------------------------------------
Write-Host "`n[Step 1] Issue token for Alice (comp_alpha, emp_001, role: employee)" -ForegroundColor Yellow
$AliceToken = New-DemoToken "comp_alpha" "emp_001" "employee"
Write-Host "Alice JWT: $(Format-RedactedToken $AliceToken)" -ForegroundColor White
$AliceHeaders = @{
    "Authorization" = "Bearer $AliceToken"
    "Content-Type"  = "application/json"
}

# -------------------------------------------------------------------------
# Step 2: Alice asks for status / schedule
# -------------------------------------------------------------------------
Write-Host "`n[Step 2] Alice asks 'what is the status of my request?'" -ForegroundColor Yellow
$body2 = @{ conversation_id = $ConvId; message = "what is the status of my request?" } | ConvertTo-Json
Write-Host "POST /chat -> body: $body2" -ForegroundColor Gray
$r2 = Invoke-RestMethod -Uri "$BaseUrl/chat" -Method POST -Headers $AliceHeaders -Body $body2
Write-Host "Assistant: $($r2.reply)" -ForegroundColor Cyan
Write-Host "State: awaiting=$($r2.awaiting)" -ForegroundColor DarkGray

# -------------------------------------------------------------------------
# Step 3: Alice asks to move drop to 9 PM (booked)
# -------------------------------------------------------------------------
Write-Host "`n[Step 3] Alice asks 'move my drop to 9 PM' (> 2h cutoff; cab found and booked)" -ForegroundColor Yellow
$body3 = @{ conversation_id = $ConvId; message = "move my drop to 9 PM" } | ConvertTo-Json
Write-Host "POST /chat -> body: $body3" -ForegroundColor Gray
$r3 = Invoke-RestMethod -Uri "$BaseUrl/chat" -Method POST -Headers $AliceHeaders -Body $body3
Write-Host "Assistant: $($r3.reply)" -ForegroundColor Cyan
Write-Host "State: awaiting=$($r3.awaiting)" -ForegroundColor DarkGray

# -------------------------------------------------------------------------
# Step 4: Alice asks for 6 PM (within 2h cutoff)
# -------------------------------------------------------------------------
Write-Host "`n[Step 4] Alice asks 'can I leave at 6 PM instead?' (inside 2h cutoff; rejected, offers manager)" -ForegroundColor Yellow
$body4 = @{ conversation_id = $ConvId; message = "can I leave at 6 PM instead?" } | ConvertTo-Json
Write-Host "POST /chat -> body: $body4" -ForegroundColor Gray
$r4 = Invoke-RestMethod -Uri "$BaseUrl/chat" -Method POST -Headers $AliceHeaders -Body $body4
Write-Host "Assistant: $($r4.reply)" -ForegroundColor Cyan
Write-Host "State: awaiting=$($r4.awaiting)" -ForegroundColor DarkGray

# -------------------------------------------------------------------------
# Step 5: Alice says 'please send to manager'
# -------------------------------------------------------------------------
Write-Host "`n[Step 5] Alice confirms 'please send to manager'" -ForegroundColor Yellow
$body5 = @{ conversation_id = $ConvId; message = "please send to manager" } | ConvertTo-Json
Write-Host "POST /chat -> body: $body5" -ForegroundColor Gray
$r5 = Invoke-RestMethod -Uri "$BaseUrl/chat" -Method POST -Headers $AliceHeaders -Body $body5
Write-Host "Assistant: $($r5.reply)" -ForegroundColor Cyan
Write-Host "State: awaiting=$($r5.awaiting)" -ForegroundColor DarkGray

# -------------------------------------------------------------------------
# Step 6: Issue token for Manager (comp_alpha, mgr_001, manager)
# -------------------------------------------------------------------------
Write-Host "`n[Step 6] Issue token for Manager (comp_alpha, mgr_001, role: manager)" -ForegroundColor Yellow
$MgrToken = New-DemoToken "comp_alpha" "mgr_001" "manager"
Write-Host "Manager JWT: $(Format-RedactedToken $MgrToken)" -ForegroundColor White
$MgrHeaders = @{
    "Authorization" = "Bearer $MgrToken"
    "Content-Type"  = "application/json"
}

# -------------------------------------------------------------------------
# Step 7: Manager checks pending requests (GET /manager/requests)
# -------------------------------------------------------------------------
Write-Host "`n[Step 7] Manager checks pending requests (GET /manager/requests)" -ForegroundColor Yellow
$mgrResp = Invoke-RestMethod -Uri "$BaseUrl/manager/requests" -Method GET -Headers $MgrHeaders
$pendingList = @($mgrResp.requests)
Write-Host "GET /manager/requests -> Count: $($pendingList.Count)" -ForegroundColor Gray
$pendingItem = $pendingList | Where-Object { $_.employee_id -eq "emp_001" } | Select-Object -Last 1
if (-not $pendingItem -and $pendingList.Count -gt 0) {
    $pendingItem = $pendingList[0]
}
Write-Host "Pending Request Found: ID=$($pendingItem.request_id), Employee=$($pendingItem.employee_id), Slot=$($pendingItem.requested_slot_time), Status=$($pendingItem.status)" -ForegroundColor Cyan

# -------------------------------------------------------------------------
# Step 8: Manager approves (POST /manager/decision)
# -------------------------------------------------------------------------
Write-Host "`n[Step 8] Manager approves request ID $($pendingItem.request_id)" -ForegroundColor Yellow
$decisionBody = @{
    request_id = [int]$pendingItem.request_id
    decision   = "approve"
    reason     = "Approved by Transport Lead"
} | ConvertTo-Json
Write-Host "POST /manager/decision -> body: $decisionBody" -ForegroundColor Gray
$decResp = Invoke-RestMethod -Uri "$BaseUrl/manager/decision" -Method POST -Headers $MgrHeaders -Body $decisionBody
Write-Host "Manager Decision Response: $($decResp | ConvertTo-Json -Compress)" -ForegroundColor Cyan

# -------------------------------------------------------------------------
# Step 9: Alice checks status (GET /chat/status)
# -------------------------------------------------------------------------
Write-Host "`n[Step 9] Alice checks status (GET /chat/status?conversation_id=$ConvId)" -ForegroundColor Yellow
$statusResp = Invoke-RestMethod -Uri "$BaseUrl/chat/status?conversation_id=$ConvId" -Method GET -Headers $AliceHeaders
Write-Host "GET /chat/status -> Status: $($statusResp.status)" -ForegroundColor Gray
Write-Host "Alice Assistant: $($statusResp.reply)" -ForegroundColor Green

Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host "           DEMO COMPLETED SUCCESSFULLY IN < 2 MINS         " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan
