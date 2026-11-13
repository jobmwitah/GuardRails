# Installs the GuardRails Windows provisioning agent.
# Run from an elevated PowerShell on the MT5 host:
#
#   powershell -ExecutionPolicy Bypass -File .\install_agent.ps1 `
#       -BackendUrl "https://your-public-host" `
#       -AgentToken "the-same-AGENT_TOKEN-as-the-backend"
#
# The token is written to config.json with inheritance disabled and access
# limited to the current user, SYSTEM, and Administrators.

param(
    [Parameter(Mandatory = $true)][string]$BackendUrl,
    [Parameter(Mandatory = $true)][string]$AgentToken,
    [string]$AgentId = "windows-1",
    [string]$InstanceRoot = "C:\GuardRails\instances",
    [string]$TemplateDir = "C:\GuardRails\mt5-template",
    [string]$ServerListsDir = "C:\GuardRails\server-lists",
    [string]$Pythonw = "",
    [switch]$SkipTask
)

$ErrorActionPreference = "Stop"

function Fail($message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

$agentDir = $PSScriptRoot
$agentScript = Join-Path $agentDir "mt5_agent.py"
if (-not (Test-Path $agentScript)) { Fail "mt5_agent.py not found next to this script." }
if (-not (Test-Path (Join-Path $TemplateDir "terminal64.exe"))) {
    Fail "Portable MT5 template not found at $TemplateDir. Run build_template.ps1 first."
}
if ($BackendUrl -notmatch '^https?://') { Fail "BackendUrl must start with https:// (or http:// for local testing)." }
if ($BackendUrl -match '^http://' -and $BackendUrl -notmatch '^http://(127\.0\.0\.1|localhost)') {
    Write-Host "WARNING: the backend URL is not HTTPS. Use HTTPS for provisioning in production." -ForegroundColor Yellow
}
if ($AgentId -ne "windows-1") {
    Write-Host "WARNING: this release supports one Windows host with agent_id 'windows-1'. Keep -AgentId at its default." -ForegroundColor Yellow
}

if (-not $Pythonw) {
    $pythonwCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $pythonwCommand) { Fail "pythonw.exe not found. Pass -Pythonw explicitly." }
    $Pythonw = $pythonwCommand.Source
}
$python = Join-Path (Split-Path $Pythonw -Parent) "python.exe"
if (-not (Test-Path $python)) { Fail "python.exe not found next to $Pythonw" }

New-Item -ItemType Directory -Force -Path $InstanceRoot | Out-Null
New-Item -ItemType Directory -Force -Path $ServerListsDir | Out-Null

$configPath = Join-Path $agentDir "config.json"
$config = [ordered]@{
    backend_url = $BackendUrl.TrimEnd('/')
    agent_token = $AgentToken
    agent_id = $AgentId
    instance_root = $InstanceRoot
    template_dir = $TemplateDir
    server_lists_dir = $ServerListsDir
    poll_seconds = 3
    heartbeat_seconds = 20
    start_timeout_seconds = 120
    stop_timeout_seconds = 30
    max_restarts = 5
}
$configJson = $config | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($configPath, $configJson, (New-Object System.Text.UTF8Encoding($false)))

# Lock the secret file down to the current user, SYSTEM, and Administrators.
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls $configPath /inheritance:r | Out-Null
& icacls $configPath /grant:r "*${currentSid}:(F)" "*S-1-5-18:(F)" "*S-1-5-32-544:(F)" | Out-Null

$runBat = Join-Path $agentDir "run_agent.bat"
@"
@echo off
cd /d "%~dp0"
"$Pythonw" "%~dp0mt5_agent.py"
"@ | Set-Content -Path $runBat -Encoding ASCII

# Validate the config without printing the token.
& $python -c "import sys; sys.path.insert(0, r'$agentDir'); import mt5_agent; c=mt5_agent.load_config(); print('config ok:', c['agent_id'], c['backend_url'])"
if ($LASTEXITCODE -ne 0) { Fail "Agent config validation failed." }

if (-not $SkipTask) {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $taskName = "GuardRailsAgent"
    $action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$agentScript`"" -WorkingDirectory $agentDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Scheduled task '$taskName' registered and started." -ForegroundColor Green
}

Write-Host ""
Write-Host "Agent installed." -ForegroundColor Green
Write-Host "  config:   $configPath"
Write-Host "  log:      $(Join-Path $agentDir 'agent.log')"
Write-Host "  manual:   $runBat"
Write-Host ""
Write-Host "The task runs at logon for $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)."
Write-Host "Keep the Windows user signed in (RDP may be disconnected, but do not sign out)."
