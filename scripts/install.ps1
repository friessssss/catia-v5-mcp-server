<#
.SYNOPSIS
    One-shot installer for the CATIA MCP server (CATIA V5 and 3DEXPERIENCE).

.DESCRIPTION
    Installs the server as an always-on background service for the current
    Windows user, without administrator rights:

      1. installs uv (Astral's Python manager) into the user profile if missing;
      2. installs the server with its own private Python 3.13:
           uv tool install --force --python 3.13 "catia-v5-mcp-server[images] @ <repo>@<branch>"
      3. runs `catia-mcp install`: registers the service to start at logon
         (HKCU Run key), starts it now on http://127.0.0.1:8765/mcp, and points
         Claude Desktop and Claude Code at it;
      4. prints `catia-mcp status`.

    Re-running it upgrades in place. Nothing is written outside the user
    profile (%USERPROFILE%\.local\bin, %APPDATA%\uv, %LOCALAPPDATA%\catia-mcp,
    the Claude config files).

.PARAMETER Branch
    Git branch, tag or commit to install. Default: main (or $env:CATIA_MCP_BRANCH).

.PARAMETER Repo
    GitHub repository (owner/name). Default: friessssss/catia-v5-mcp-server.

.PARAMETER Port
    Local port of the service. Default: 8765.

.PARAMETER NoClients
    Do not configure Claude Desktop / Claude Code.

.PARAMETER Python
    Python version uv installs for the tool. Default: 3.13.

.PARAMETER Uninstall
    Stop the service, remove the autostart and the Claude client entries,
    then remove the tool.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1 | iex"

.EXAMPLE
    # A branch other than main, straight from the web:
    powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1))) -Branch my-branch"

.EXAMPLE
    scripts\install.cmd -Branch my-branch
    scripts\install.cmd -Uninstall
#>
param(
    [string]$Branch = $(if ($env:CATIA_MCP_BRANCH) { $env:CATIA_MCP_BRANCH } else { 'main' }),
    [string]$Repo = 'friessssss/catia-v5-mcp-server',
    [int]$Port = 8765,
    [switch]$NoClients,
    [string]$Python = '3.13',
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PackageName = 'catia-v5-mcp-server'

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "    $Text" }
function Write-Warn([string]$Text) { Write-Host "    WARNING: $Text" -ForegroundColor Yellow }

function Invoke-Native {
    # Runs an external program and throws on a non-zero exit code. Windows
    # PowerShell 5.1 does not stop on native failures by itself, and under
    # 'Stop' it can turn harmless stderr progress output into an error, so
    # the preference is relaxed for the call only.
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @(), [string]$What = '')
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Out-Host: a function returns everything written to its output
        # stream, so a program's console text would otherwise be mixed into
        # the caller's return value (the uv installer's banner became part
        # of the uv.exe path on a live run).
        & $Exe @Arguments | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) {
        if (-not $What) { $What = "$Exe $($Arguments -join ' ')" }
        throw "$What failed (exit code $code)."
    }
}

function Add-SessionPath([string]$Dir) {
    if ($Dir -and (Test-Path $Dir) -and (($env:Path -split ';') -notcontains $Dir)) {
        $env:Path = "$Dir;$env:Path"
    }
}

function Find-Uv {
    Add-SessionPath (Join-Path $env:USERPROFILE '.local\bin')
    Add-SessionPath (Join-Path $env:USERPROFILE '.cargo\bin')  # older uv installers
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Install-Uv {
    $uv = Find-Uv
    if ($uv) {
        Write-Note "uv found: $uv"
        return $uv
    }
    Write-Step 'Installing uv (user profile, no admin rights needed)'
    # A child PowerShell keeps the uv installer's variables out of this scope.
    Invoke-Native -Exe 'powershell.exe' -What 'The uv installer' -Arguments @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
        '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; irm https://astral.sh/uv/install.ps1 | iex'
    )
    $uv = Find-Uv
    if (-not $uv) {
        throw ("uv was installed but is not on PATH. Open a new PowerShell window and " +
               "re-run this installer, or check $env:USERPROFILE\.local\bin\uv.exe.")
    }
    Write-Note "uv installed: $uv"
    return [string]$uv
}

function Get-ToolBin([string]$Uv) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $dir = (& $Uv tool dir --bin 2>$null | Select-Object -First 1) } finally { $ErrorActionPreference = $saved }
    if (-not $dir) { $dir = Join-Path $env:USERPROFILE '.local\bin' }
    return "$dir".Trim()
}

function Find-CatiaMcp([string]$Uv) {
    $bin = Get-ToolBin $Uv
    Add-SessionPath $bin
    $exe = Join-Path $bin 'catia-mcp.exe'
    if (Test-Path $exe) { return $exe }
    $cmd = Get-Command catia-mcp -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Stop-ToolProcesses {
    # uv replaces the tool's virtual environment on --force; Windows refuses
    # while its python.exe / pythonw.exe is running (the service itself, or
    # the `catia-mcp proxy` that Claude Desktop keeps open).
    try {
        $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath -like "*\tools\$PackageName\*"
        })
    } catch {
        return
    }
    # The venv's python.exe / pythonw.exe is a launcher that starts the base
    # interpreter (under %APPDATA%\uv\python) as a CHILD process; the child
    # is what loads the tool's site-packages DLLs (pywin32), and its own
    # ExecutablePath is outside the tool folder. Kill each match with its
    # process tree so the child goes too. (Needs live validation on Windows.)
    foreach ($p in $procs) {
        Write-Note "Stopping $($p.Name) (PID $($p.ProcessId)) that runs from the old install"
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & taskkill.exe /PID $p.ProcessId /T /F *> $null
        } finally {
            $ErrorActionPreference = $saved
        }
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($procs.Count -gt 0) { Start-Sleep -Seconds 1 }
}

function Get-PackageSpec {
    # A git URL tracks the branch's latest commit; without git on the machine
    # (common on managed laptops) the GitHub source archive works the same.
    if (Get-Command git -ErrorAction SilentlyContinue) {
        return "$PackageName[images] @ git+https://github.com/$Repo@$Branch"
    }
    return "$PackageName[images] @ https://github.com/$Repo/archive/$Branch.zip"
}

function Invoke-Install {
    Write-Step "CATIA MCP server installer ($Repo, branch '$Branch')"
    if ($env:OS -ne 'Windows_NT') { throw 'This installer runs on Windows only.' }

    $uv = Install-Uv
    # Corporate proxies re-sign TLS traffic with a company root certificate
    # that lives in the Windows store; native TLS makes uv trust it.
    if (-not $env:UV_NATIVE_TLS) { $env:UV_NATIVE_TLS = '1' }

    $existing = Find-CatiaMcp $uv
    if ($existing) {
        Write-Step 'Stopping the running service before upgrading'
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $existing stop --port $Port | ForEach-Object { Write-Note $_ } } finally { $ErrorActionPreference = $saved }
    }
    Stop-ToolProcesses

    $spec = Get-PackageSpec
    Write-Step "Installing $spec (Python $Python)"
    Invoke-Native -Exe $uv -What 'uv tool install' -Arguments @(
        'tool', 'install', '--force', '--python', $Python,
        '--reinstall-package', $PackageName, '--refresh-package', $PackageName,
        $spec
    )

    $catia = Find-CatiaMcp $uv
    if (-not $catia) {
        throw "catia-mcp.exe not found after the install (looked in $(Get-ToolBin $uv))."
    }
    # Make the command available in future terminals too (user PATH only).
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $uv tool update-shell *> $null } finally { $ErrorActionPreference = $saved }

    Write-Step 'Registering the background service and the Claude clients'
    $installArgs = @('install', '--port', "$Port")
    if ($NoClients) { $installArgs += '--no-clients' }
    Invoke-Native -Exe $catia -Arguments $installArgs -What 'catia-mcp install'

    Write-Step 'Status'
    Invoke-Native -Exe $catia -Arguments @('status', '--port', "$Port") -What 'catia-mcp status'

    Write-Host ''
    Write-Host 'Done.' -ForegroundColor Green
    Write-Note "Endpoint: http://127.0.0.1:$Port/mcp   Health: http://127.0.0.1:$Port/health"
    Write-Note 'Restart Claude Desktop (quit it from the tray icon) so it loads the "catia" server.'
    Write-Note 'Start CATIA (and log in, on 3DEXPERIENCE); the server attaches on the first tool call.'
}

function Invoke-Uninstall {
    Write-Step 'Uninstalling the CATIA MCP server'
    $uv = Find-Uv
    if (-not $uv) {
        Write-Warn 'uv not found; nothing installed by this script to remove.'
        return
    }
    $catia = Find-CatiaMcp $uv
    if ($catia) {
        Invoke-Native -Exe $catia -Arguments @('uninstall', '--port', "$Port") -What 'catia-mcp uninstall'
    } else {
        Write-Warn 'catia-mcp not found; skipping service and client clean-up.'
    }
    Stop-ToolProcesses
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $uv tool uninstall $PackageName } finally { $ErrorActionPreference = $saved }
    Write-Host 'Uninstalled. Logs remain in %LOCALAPPDATA%\catia-mcp (delete the folder if unwanted).' -ForegroundColor Green
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    if ($Uninstall) { Invoke-Uninstall } else { Invoke-Install }
} catch {
    Write-Host ''
    Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Troubleshooting: docs/background-service.md (https://github.com/friessssss/catia-v5-mcp-server/blob/main/docs/background-service.md)'
    # `exit` would close the user's window when the script runs through
    # `irm | iex`; only a script file run (install.cmd, -File) gets an exit code.
    if ($PSCommandPath) { exit 1 }
}
