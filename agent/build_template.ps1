# Builds a lean, credential-free portable MetaTrader 5 template.
# Run this once on the Windows MT5 host before installing the agent.
#
#   powershell -ExecutionPolicy Bypass -File .\build_template.ps1
#
# The template preserves the existing WebRequest allow-list rule, removes all
# saved account credentials, and includes the compiled GuardRails EA.

param(
    [string]$SourceTerminal = "C:\Program Files\MetaTrader 5",
    [string]$SourceData = "",
    [string]$Destination = "C:\GuardRails\mt5-template",
    [string]$EaSource = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Fail($message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $SourceTerminal "terminal64.exe"))) {
    Fail "terminal64.exe not found in $SourceTerminal"
}

# Locate the terminal data directory (the one whose origin.txt points at the
# installed terminal). This holds the WebRequest allow-list and the EA build.
if (-not $SourceData) {
    $terminalRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    $candidates = Get-ChildItem $terminalRoot -Directory -ErrorAction SilentlyContinue
    foreach ($candidate in $candidates) {
        $originFile = Join-Path $candidate.FullName "origin.txt"
        if (Test-Path $originFile) {
            $origin = (Get-Content $originFile -Raw).Trim()
            if ($origin.TrimEnd('\') -ieq $SourceTerminal.TrimEnd('\')) {
                $SourceData = $candidate.FullName
                break
            }
        }
    }
}
if (-not $SourceData -or -not (Test-Path $SourceData)) {
    Fail "Could not find the MT5 data directory. Pass -SourceData explicitly."
}

if (-not $EaSource) {
    $repoEa = Join-Path $PSScriptRoot "..\EA_Bridge.ex5"
    if (Test-Path $repoEa) {
        $EaSource = (Resolve-Path $repoEa).Path
    } else {
        $EaSource = Join-Path $SourceData "MQL5\Experts\EA_Bridge.ex5"
    }
}
if (-not (Test-Path $EaSource)) {
    Fail "EA_Bridge.ex5 not found. Pass -EaSource explicitly."
}

if ((Test-Path $Destination) -and -not $Force) {
    Fail "$Destination already exists. Re-run with -Force to rebuild it."
}
if (Test-Path $Destination) {
    Remove-Item $Destination -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

Write-Host "Copying terminal files..."
$excludeDirs = @("Bases", "logs", "Profiles", "Sounds", "llm-agent")
$excludeFiles = @("uninstall.exe", "MetaEditor64.exe", "metatester64.exe")
$args = @($SourceTerminal, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
foreach ($name in $excludeDirs) { $args += @("/XD", $name) }
foreach ($name in $excludeFiles) { $args += @("/XF", $name) }
& robocopy @args | Out-Null
if ($LASTEXITCODE -ge 8) {
    Fail "robocopy failed with exit code $LASTEXITCODE"
}

New-Item -ItemType Directory -Force -Path (Join-Path $Destination "config") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Destination "MQL5\Experts") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Destination "MQL5\Presets") | Out-Null

Copy-Item $EaSource (Join-Path $Destination "MQL5\Experts\EA_Bridge.ex5") -Force
$eaSourceCode = Join-Path (Split-Path $EaSource -Parent) "EA_Bridge.mq5"
if (Test-Path $eaSourceCode) {
    Copy-Item $eaSourceCode (Join-Path $Destination "MQL5\Experts\EA_Bridge.mq5") -Force
}

$serversFile = Join-Path $SourceData "config\servers.dat"
if (Test-Path $serversFile) {
    Copy-Item $serversFile (Join-Path $Destination "config\servers.dat") -Force
}

# Copy common.ini without any saved login, server, password, installation ID,
# or push identifier. Keep the WebRequestUrl hash so the EA can reach the bot.
$commonSource = Join-Path $SourceData "config\common.ini"
$commonDest = Join-Path $Destination "config\common.ini"
if (Test-Path $commonSource) {
    $lines = [System.Collections.Generic.List[string]]::new()
    $section = ""
    foreach ($raw in (Get-Content $commonSource)) {
        $line = [string]$raw
        if ($line -match '^\s*\[(.+)\]\s*$') {
            $section = $matches[1]
            $lines.Add($line)
            continue
        }
        if ($section -ieq "Common" -and $line -match '^\s*(Login|Server|Password|Environment|CertInstall)\s*=') { continue }
        if ($section -ieq "Notification" -and $line -match '^\s*PushID\s*=') { continue }
        $lines.Add($line)
    }

    function Set-IniValue {
        param(
            [System.Collections.Generic.List[string]]$Lines,
            [string]$Section,
            [string]$Key,
            [string]$Value
        )
        $start = -1
        $end = $Lines.Count
        for ($i = 0; $i -lt $Lines.Count; $i++) {
            if ($Lines[$i] -match '^\s*\[') {
                if ($start -ge 0) { $end = $i; break }
                if ($Lines[$i] -match ('^\s*\[' + [regex]::Escape($Section) + '\]\s*$')) { $start = $i }
            }
        }
        if ($start -lt 0) {
            $Lines.Add("[$Section]")
            $Lines.Add("$Key=$Value")
            return
        }
        for ($i = $start + 1; $i -lt $end; $i++) {
            if ($Lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
                $Lines.RemoveAt($i)
                $i--
                $end--
            }
        }
        $Lines.Insert($end, "$Key=$Value")
    }

    Set-IniValue $lines "Experts" "AllowLiveTrading" "1"
    Set-IniValue $lines "Experts" "AllowDllImport" "0"
    Set-IniValue $lines "Experts" "Enabled" "1"
    Set-IniValue $lines "Experts" "Account" "0"
    Set-IniValue $lines "Experts" "Profile" "0"
    Set-IniValue $lines "Experts" "WebRequest" "1"

    $lines | Set-Content -Path $commonDest -Encoding Unicode
    $hasRule = (Select-String -Path $commonDest -Pattern '^\s*WebRequestUrl\s*=' -Quiet)
    Write-Host ("common.ini written; WebRequest rule present: {0}" -f $hasRule)
} else {
    Write-Host "WARNING: common.ini not found; the EA's WebRequest rule will need adding manually." -ForegroundColor Yellow
}

$sizeMb = [math]::Round(((Get-ChildItem $Destination -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host ""
Write-Host "Template ready: $Destination ($sizeMb MB)" -ForegroundColor Green
Write-Host "No saved MT5 login or password was copied."
