# hwp-mcp installer (Windows PowerShell 5.1+). Safe to run again: finished steps are skipped.
#
#   .\setup.ps1                 install for Claude Code (and print the Claude Desktop config)
#   .\setup.ps1 -Remote         also run an HTTP server in the background for claude.ai (needs a tunnel, e.g. Tailscale Funnel)
#                               remote access is read-only unless you add -AllowRemoteEdit
#   .\setup.ps1 -Uninstall      remove the Claude Code entry, background task and security-module registration
#
param(
    [switch]$Remote,
    [switch]$AllowRemoteEdit,
    [switch]$Uninstall,
    [int]$Port = 8772,
    [string]$Name = "hwp",
    [string]$TaskName = "HWPMCP",
    [switch]$SkipClaude,
    [switch]$ReinstallSecurity
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Server = Join-Path $Root "server.py"
$RegKey = "HKCU:\Software\HNC\HwpAutomation\Modules"
$RegName = "FilePathCheckerModule"
# Official Hancom automation security module (sample whose check always returns TRUE)
$SecUrl = "https://raw.githubusercontent.com/hancom-io/devcenter-archive/main/hwp-automation/" + [uri]::EscapeDataString("보안모듈(Automation).zip")
$SecSha = "9AC5B97C47AC8AED1E8BCA27A3EEF39411361D8F68C262509F0C40A8F9D21BB6"
$SecDir = Join-Path $Root "security"
$SecDll = Join-Path $SecDir "FilePathCheckerModuleExample.dll"

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "   OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   !!  $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Step "Uninstall"
    if (Get-Command claude -ErrorAction SilentlyContinue) { & claude mcp remove $Name -s user 2>$null | Out-Null; Ok "Claude Code entry '$Name' removed (if it existed)" }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Ok "task $TaskName removed"
    }
    $cur = (Get-ItemProperty $RegKey -ErrorAction SilentlyContinue).$RegName
    if ($cur -and $cur -like "$Root*") { Remove-ItemProperty $RegKey -Name $RegName; Ok "security module registration removed" }
    Write-Host "`nIf you exposed the server with a tunnel, turn that off too (e.g. tailscale funnel --set-path=/<path> off)."
    exit 0
}

# ---------------------------------------------------------------- 1. Python
Step "1/5 Python"
$py = $null
foreach ($c in @(@("py", "-3"), @("python"))) {
    try {
        $exe = & $c[0] $c[1..9] -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { $py = $exe.Trim(); break }
    } catch {}
}
if (-not $py) { throw "Python 3.10+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again." }
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
Ok $py

# ---------------------------------------------------------------- 2. packages
Step "2/5 Python packages (mcp, pywin32, pymupdf, psutil)"
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Ok "installed"

# ---------------------------------------------------------------- 3. Hangul + security module
Step "3/5 Hangul (Hancom Office) and its automation security module"
$hangul = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey("HWPFrame.HwpObject") -ne $null
if (-not $hangul) {
    Warn "Hangul is not installed: reading (.hwp/.hwpx) and .hwpx editing still work; previews and .hwp editing need Hangul."
} else {
    Ok "Hangul automation found"
    $cur = (Get-ItemProperty $RegKey -ErrorAction SilentlyContinue).$RegName
    if ($cur -and (Test-Path $cur) -and -not $ReinstallSecurity) {
        Ok "security module already registered: $cur"
    } else {
        Write-Host "   Without this module Hangul shows an 'allow access?' dialog and automation hangs."
        Write-Host "   Downloading Hancom's official module: $SecUrl"
        $tmp = Join-Path $env:TEMP ("hwpmcp_sec_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force $tmp | Out-Null
        $zip = Join-Path $tmp "sec.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $SecUrl -OutFile $zip
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $za = [IO.Compression.ZipFile]::OpenRead($zip)
        try {
            $entry = $za.Entries | Where-Object { $_.Name -eq "FilePathCheckerModuleExample.dll" } | Select-Object -First 1
            if (-not $entry) { throw "DLL not found in the downloaded zip" }
            $dllTmp = Join-Path $tmp "m.dll"
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dllTmp, $true)
        } finally { $za.Dispose() }
        $sha = (Get-FileHash $dllTmp -Algorithm SHA256).Hash
        if ($sha -ne $SecSha) { throw "security module checksum mismatch ($sha) — not installing it" }
        New-Item -ItemType Directory -Force $SecDir | Out-Null
        Copy-Item $dllTmp $SecDll -Force
        Remove-Item $tmp -Recurse -Force
        New-Item -Path $RegKey -Force | Out-Null
        New-ItemProperty -Path $RegKey -Name $RegName -Value $SecDll -PropertyType String -Force | Out-Null
        Ok "installed + registered (SHA256 verified): $SecDll"
    }
}

# ---------------------------------------------------------------- 4. self-test
Step "4/5 Self-test"
& $py (Join-Path $Root "tests\run_tests.py") | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) { throw "self-test failed" }
& $py -c "import sys; sys.path.insert(0, r'$Root'); import server; print('   server loads, tools:', len(server.mcp._tool_manager.list_tools()))"
if ($LASTEXITCODE -ne 0) { throw "server failed to load" }

# ---------------------------------------------------------------- 5. connect
Step "5/5 Connect to Claude"
if (-not $SkipClaude -and (Get-Command claude -ErrorAction SilentlyContinue)) {
    # No '--' and no variadic -e here: Windows PowerShell drops '--' when calling the npm claude.ps1 shim,
    # and then '-e' swallows the command. (MCP stdio is UTF-8 already, so no env is needed.)
    & claude mcp remove $Name -s user 2>$null | Out-Null
    & claude mcp add -s user $Name $py $Server 2>&1 | Out-Null
    & claude mcp get $Name 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "Claude Code: added as '$Name' (start a new Claude Code session to use it)" }
    else { Warn "claude mcp add failed — run it yourself:  claude mcp add -s user $Name `"$py`" `"$Server`"" }
} elseif (-not $SkipClaude) {
    Warn "Claude Code CLI not found — skip. Later: claude mcp add $Name -s user -- `"$py`" `"$Server`""
}
$cfg = @{ mcpServers = @{ $Name = @{ command = $py; args = @($Server); env = @{ PYTHONUTF8 = "1" } } } } | ConvertTo-Json -Depth 5
Write-Host "   Claude Desktop: merge this into %APPDATA%\Claude\claude_desktop_config.json`n$cfg"

if ($Remote) {
    Step "Remote (claude.ai) — background HTTP server on 127.0.0.1:$Port"
    $a  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Server`" --http --port $Port" -WorkingDirectory $Root
    $tr = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $pr = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $st = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
          -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
          -Priority 4   # Task Scheduler's default (7, below normal) can starve the server's startup on a busy PC
    Register-ScheduledTask -TaskName $TaskName -Action $a -Trigger $tr -Principal $pr -Settings $st -Force `
        -Description "hwp-mcp HTTP server for claude.ai" | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
    $up = $false
    for ($i = 0; $i -lt 30; $i++) { if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $up = $true; break }; Start-Sleep 2 }
    if ($up) { Ok "task '$TaskName' running, listening on 127.0.0.1:$Port/mcp" } else { Warn "server did not start — see server.log" }

    $pathFile = Join-Path $Root "funnel_path.txt"
    if (-not (Test-Path $pathFile)) {
        $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $secret = "hwp-" + ([Convert]::ToBase64String($bytes) -replace '[+/=]', '').Substring(0, 30)
        Set-Content -Path $pathFile -Value $secret -Encoding ascii -NoNewline
    }
    $secret = (Get-Content $pathFile -Raw).Trim()
    $hostName = $null
    if (Get-Command tailscale -ErrorAction SilentlyContinue) {
        try { $hostName = ((tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".") } catch {}
    }
    $envFile = Join-Path $Root ".env"
    $lines = @()
    if (Test-Path $envFile) { $lines = @(Get-Content $envFile | Where-Object { $_ -notmatch '^HWP_MCP_(PUBLIC_HOSTS|READONLY)=' }) }
    if ($hostName) { $lines += "HWP_MCP_PUBLIC_HOSTS=$hostName" }
    # safer default for a server reachable from the internet: find/read/preview only
    $lines += "HWP_MCP_READONLY=" + $(if ($AllowRemoteEdit) { "0" } else { "1" })
    [IO.File]::WriteAllLines($envFile, [string[]]$lines)   # UTF-8 without BOM
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName $TaskName
    if ($AllowRemoteEdit) { Warn "remote EDIT tools enabled (-AllowRemoteEdit): anyone with the URL can change documents in the allowed folders" }
    else { Ok "remote access is read-only (find/read/preview). Re-run with -AllowRemoteEdit to allow editing." }
    if ($hostName) {
        Write-Host "`n   Publish it (this opens the server to the internet — the secret path is the only key, keep it private):"
        Write-Host "     tailscale funnel --bg --set-path=/$secret http://127.0.0.1:$Port" -ForegroundColor White
        Write-Host "   Then in claude.ai → Settings → Connectors → Add custom connector, URL:"
        Write-Host "     https://$hostName/$secret/mcp" -ForegroundColor White
    } else {
        Warn "Tailscale not found. Expose http://127.0.0.1:$Port with any HTTPS tunnel, put its host name in .env as"
        Write-Host "     HWP_MCP_PUBLIC_HOSTS=<your-tunnel-host>   and use https://<host>/<secret path>/mcp in claude.ai"
    }
}
Write-Host "`nDone." -ForegroundColor Green
