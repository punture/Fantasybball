#Requires -Version 5.1
<#
.SYNOPSIS
    Connect FBM to OpenClaw on Windows (native Gateway): the `fbm` chat skill plus scheduled Telegram posts.

.DESCRIPTION
    Installs FBM's `fbm` skill into your OpenClaw workspace (ask "how's my matchup?" in Telegram) and registers
    scheduled jobs named fbm:* that run FBM and post its output to your Telegram chat with no model call.
    Safe to re-run: it replaces only what it created (the fbm skill folder and jobs named fbm:*), changes OpenClaw
    settings only through the openclaw CLI, and asks before optional changes.

    If OpenClaw's Gateway runs inside WSL (the Windows Hub "Set up locally" option does this), install FBM inside
    that WSL distro and run openclaw/install.sh there instead: the Gateway runs FBM where it lives.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -ChatId 123456789

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -DryRun

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File openclaw\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # Telegram chat id for the posts (default: [notify] telegram_chat_id in config\settings.toml, else a prompt).
    [string]$ChatId = $env:FBM_OPENCLAW_CHAT_ID,
    # Only these jobs, comma-separated, e.g. 'morning-digest,lineup-guard'. Default: all.
    [string]$Jobs = '',
    # Install the skill only; register no scheduled jobs.
    [switch]$NoJobs,
    # Change schedules (ET, 5-field cron), e.g. -Cron 'morning-digest=0 9 * * *'. Also FBM_CRON_<NAME> variables.
    [string[]]$Cron = @(),
    # On OpenClaw older than 2026.6.5, schedule the digests as agent turns instead (uses your model).
    [switch]$Fallback,
    # Add FBM to OpenClaw's exec allowlist when exec isn't in full mode: ask | yes | no.
    [ValidateSet('ask', 'yes', 'no')][string]$Allowlist = 'ask',
    # Also add "openclaw" to FBM's [notify] channels: ask | yes | no (no keeps config\settings.toml unchanged).
    [ValidateSet('ask', 'yes', 'no')][string]$NotifyOpenClaw = 'ask',
    # Don't send the Telegram test message.
    [switch]$NoTestMessage,
    # OpenClaw agent for the allowlist and fallback jobs.
    [string]$Agent = 'main',
    # Skill folder (default: <OpenClaw workspace>\skills\fbm).
    [string]$SkillDir = '',
    # FBM checkout (default: the folder above this script).
    [string]$Repo = '',
    # FBM program (default: <repo>\.venv\Scripts\fbm.exe).
    [string]$FbmBin = '',
    # FBM settings file (default: <repo>\config\settings.toml).
    [string]$Settings = '',
    # openclaw CLI (default: openclaw on PATH).
    [string]$OpenClaw = 'openclaw',
    # Replace a skill folder named fbm that FBM didn't create; continue when the Gateway looks like WSL.
    [switch]$Force,
    # No questions: take the suggested answer to each.
    [switch]$Yes,
    # Print every command that would change something; change nothing.
    [switch]$DryRun,
    # Remove the fbm:* jobs, the skill, the allowlist entry and what this script set up.
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
# Pass native arguments the Windows PowerShell 5.1 way, which ConvertTo-ArgvJson relies on: PowerShell 7.3+ would
# escape the \" in a --command-argv value a second time.
if (Get-Variable -Name PSNativeCommandArgumentPassing -ErrorAction SilentlyContinue) { $PSNativeCommandArgumentPassing = 'Legacy' }

$MinCommandJobs = [version]'2026.6.5'  # first OpenClaw with command cron jobs (runs a program, no model call)
$MinCronFlags = [version]'2026.2.17'   # first OpenClaw with --exact on cron jobs (needed by the agent-turn fallback)
$JobTz = 'America/New_York'            # Yahoo NBA game days are US/Eastern: every schedule below is ET
$JobPrefix = 'fbm:'
$Marker = 'fbm-openclaw-skill'         # written into the generated SKILL.md; only folders carrying it are replaced
$TestMessage = 'FBM is connected to OpenClaw. Scheduled posts arrive in this chat; reply here to ask about your league.'

# Scheduled jobs (same table as install.sh). Delivery: announce = post stdout to Telegram, none = silent.
$JobTable = @(
    @{ Name = 'fbm:data-sync'; Cron = '0 3 * * *'; Delivery = 'none'; Timeout = 1800; Run = 'data sync' }
    @{ Name = 'fbm:health'; Cron = '30 3 * * *'; Delivery = 'none'; Timeout = 600; Run = 'health' }
    @{ Name = 'fbm:snapshot'; Cron = '0 4 * * *'; Delivery = 'none'; Timeout = 600; Run = 'snapshot' }
    @{ Name = 'fbm:weekly-retro'; Cron = '0 5 * * 1'; Delivery = 'none'; Timeout = 900; Run = 'learn retro' }
    @{ Name = 'fbm:morning-digest'; Cron = '30 8 * * *'; Delivery = 'announce'; Timeout = 600; Run = 'digest --kind daily' }
    @{ Name = 'fbm:weekly-preview'; Cron = '0 8 * * 1'; Delivery = 'announce'; Timeout = 600; Run = 'digest --kind weekly' }
    @{ Name = 'fbm:lineup-guard'; Cron = '*/15 10-23 * * *'; Delivery = 'announce'; Timeout = 300; Run = 'alert lineup' }
    @{ Name = 'fbm:injury-watch'; Cron = '5,20,35,50 10-23 * * *'; Delivery = 'announce'; Timeout = 300; Run = 'alert injuries' }
)
# On OpenClaw older than 2026.6.5 these can run as agent turns instead (one model call per run).
$FallbackJobs = @('fbm:morning-digest', 'fbm:weekly-preview')

$Interactive = (-not $Yes) -and [Environment]::UserInteractive -and (-not [Console]::IsInputRedirected)

# ------------------------------------------------------------------ helpers

function Write-Step([string]$Text) { Write-Host ''; Write-Host $Text -ForegroundColor White }
function Write-Ok([string]$Text) { Write-Host "  ok $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  ! $Text" -ForegroundColor Yellow }
function Stop-Install([string]$Text) {
    Write-Host "error: $Text" -ForegroundColor Red
    exit 1
}

function Format-Command([string]$Exe, [string[]]$ArgList) {
    $parts = foreach ($a in (@($Exe) + @($ArgList))) {
        if ($a -match '^[A-Za-z0-9_./:=@,+%\\-]+$') { $a } else { "'" + ($a -replace "'", "''") + "'" }
    }
    return ($parts -join ' ')
}

# Runs a program; returns Code (exit code) and Text (output). Never throws: native stderr must not stop the script.
function Invoke-Native([string]$Exe, [string[]]$ArgList = @(), [switch]$NoStderr) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try {
        if ($NoStderr) { $lines = & $Exe @ArgList 2>$null } else { $lines = & $Exe @ArgList 2>&1 }
        $code = $LASTEXITCODE
    } catch {
        $lines = @($_.Exception.Message)
        $code = 1
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($null -eq $code) { $code = 0 }
    $text = (@($lines) | ForEach-Object { "$_" }) -join "`n"
    return [pscustomobject]@{ Code = [int]$code; Text = $text.Trim() }
}

# A command that changes something: printed under -DryRun, else run.
function Invoke-Change([string]$Exe, [string[]]$ArgList) {
    if ($DryRun) {
        Write-Host ('  [dry-run] ' + (Format-Command $Exe $ArgList))
        return [pscustomobject]@{ Code = 0; Text = '' }
    }
    return (Invoke-Native $Exe $ArgList)
}

function Get-FirstLine([string]$Text) {
    if (-not $Text) { return '' }
    return ($Text -split "`r?`n")[0].Trim()
}

function Confirm-Choice([string]$Question, [bool]$Default) {
    if ($Yes) { return $Default }
    if (-not $Interactive) { return $false }
    $hint = '[y/N]'
    if ($Default) { $hint = '[Y/n]' }
    $answer = Read-Host "  $Question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLower().StartsWith('y')
}

function Resolve-Choice([string]$Value, [string]$Question, [bool]$Default) {
    if ($Value -eq 'yes') { return $true }
    if ($Value -eq 'no') { return $false }
    return (Confirm-Choice $Question $Default)
}

function Get-OpenClawConfig([string]$Path) {
    if (-not $script:OcFound) { return '' }
    $r = Invoke-Native $script:OcExe @('config', 'get', $Path) -NoStderr
    if ($r.Code -ne 0) { return '' }
    return (Get-FirstLine $r.Text)
}

function Get-JobSchedule([string]$Name, [string]$Default) {
    $short = $Name.Substring($JobPrefix.Length)
    foreach ($item in $Cron) {
        if ($item.StartsWith("$Name=") -or $item.StartsWith("$short=")) { return $item.Substring($item.IndexOf('=') + 1) }
    }
    $envValue = [Environment]::GetEnvironmentVariable('FBM_CRON_' + $short.ToUpper().Replace('-', '_'))
    if ($envValue) { return $envValue }
    return $Default
}

function Test-JobSelected([string]$Name) {
    if (-not $Jobs) { return $true }
    $wanted = $Jobs.Split(',') | ForEach-Object { $_.Trim() }
    return (($wanted -contains $Name) -or ($wanted -contains $Name.Substring($JobPrefix.Length)))
}

function Test-KnownJob([string]$Name) {
    foreach ($job in $JobTable) {
        if (($job.Name -eq $Name) -or ($job.Name -eq ($JobPrefix + $Name))) { return $true }
    }
    return $false
}

# A typo in -Jobs or -Cron would otherwise schedule nothing, or keep the default time, silently.
function Assert-JobNames {
    $all = ($JobTable | ForEach-Object { $_.Name }) -join ' '
    if ($Jobs) {
        foreach ($name in $Jobs.Split(',')) {
            $short = $name.Trim()
            if ($short -and (-not (Test-KnownJob $short))) { Stop-Install "Unknown job '$short' in -Jobs. Jobs: $all" }
        }
    }
    foreach ($item in $Cron) {
        $eq = $item.IndexOf('=')
        if (($eq -lt 1) -or ($eq -eq ($item.Length - 1))) {
            Stop-Install "-Cron needs NAME=EXPR (e.g. 'morning-digest=0 9 * * *'), got '$item'."
        }
        $short = $item.Substring(0, $eq)
        if (-not (Test-KnownJob $short)) { Stop-Install "Unknown job '$short' in -Cron. Jobs: $all" }
    }
}

# ------------------------------------------------------------------ state (what --uninstall undoes)

function Read-State {
    $script:State = @{ failure_alert = '0'; allowlist = '0'; notify_channel = '0'; chat_id = ''; skill_dir = ''; fbm_bin = '' }
    if (-not (Test-Path -LiteralPath $script:StateFile)) { return }
    foreach ($line in (Get-Content -LiteralPath $script:StateFile)) {
        if ($line -match '^([a-z_]+)=(.*)$') { $script:State[$matches[1]] = $matches[2] }
    }
}

function Save-State {
    if ($DryRun) { return }
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $script:StateFile)) | Out-Null
    $lines = @('# Written by openclaw/install.ps1 so -Uninstall removes exactly what it set up.')
    foreach ($key in @('skill_dir', 'fbm_bin', 'chat_id', 'failure_alert', 'allowlist', 'notify_channel')) {
        $lines += "$key=$($script:State[$key])"
    }
    [System.IO.File]::WriteAllText($script:StateFile, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
}

# ------------------------------------------------------------------ steps

function Find-Fbm {
    Write-Step 'FBM'
    if (-not $script:Repo) { $script:Repo = Split-Path -Parent $PSScriptRoot }
    if (-not (Test-Path -LiteralPath $script:Repo)) { Stop-Install "FBM folder not found: $($script:Repo)" }
    $script:Repo = (Resolve-Path -LiteralPath $script:Repo).ProviderPath.TrimEnd('\')
    if (-not ((Test-Path -LiteralPath (Join-Path $script:Repo 'pyproject.toml')) -and (Test-Path -LiteralPath (Join-Path $script:Repo 'agents\skills')))) {
        Stop-Install "$($script:Repo) doesn't look like an FBM checkout (use -Repo DIR)."
    }
    $script:Helper = Join-Path $script:Repo 'scripts\render_openclaw_skill.py'
    if (-not (Test-Path -LiteralPath $script:Helper)) { Stop-Install "$($script:Helper) is missing: update this FBM checkout (git pull)." }
    if (-not $script:Settings) { $script:Settings = Join-Path $script:Repo 'config\settings.toml' }
    $script:StateFile = Join-Path $script:Repo 'data\openclaw\install-state'
    if (-not $script:FbmBin) {
        $script:FbmBin = Join-Path $script:Repo '.venv\Scripts\fbm.exe'
        if ((-not (Test-Path -LiteralPath $script:FbmBin)) -and (-not $Uninstall)) {
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                Stop-Install "FBM isn't installed yet and uv isn't on PATH. Install uv (https://docs.astral.sh/uv/), run 'uv sync' in $($script:Repo), then run this again."
            }
            Write-Host '  Setting up FBM''s Python environment (uv sync)...'
            if ($DryRun) {
                Write-Host "  [dry-run] cd '$($script:Repo)'; uv sync"
            } else {
                Push-Location -LiteralPath $script:Repo
                try { $r = Invoke-Native 'uv' @('sync') } finally { Pop-Location }
                if ($r.Code -ne 0) { Stop-Install "uv sync failed: $(Get-FirstLine $r.Text)" }
            }
        }
    }
    $script:FbmBin = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($script:FbmBin)
    $script:Py = Join-Path $script:Repo '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $script:Py)) {
        $found = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $script:Py = $found.Path } else { $script:Py = '' }
    }
    if ($Uninstall) { return }
    if (-not $script:Py) { Stop-Install "No Python found to run $($script:Helper) (expected $($script:Repo)\.venv\Scripts\python.exe)." }
    if (-not (Test-Path -LiteralPath $script:FbmBin)) {
        if (-not $DryRun) { Stop-Install "FBM program not found at $($script:FbmBin) (use -FbmBin PATH)." }
        Write-Warn "FBM program not found at $($script:FbmBin) yet (dry run)."
        return
    }
    $r = Invoke-Native $script:FbmBin @('tool', '--list')
    if ($r.Code -ne 0) { Stop-Install "FBM didn't start: $(Get-FirstLine $r.Text)" }
    if ($r.Text -notmatch 'context_pack') { Stop-Install "'$($script:FbmBin) tool --list' didn't list FBM's tools." }
    Write-Ok "FBM at $($script:FbmBin)"
}

function Find-OpenClaw {
    Write-Step 'OpenClaw'
    $script:OcFound = $false
    $script:CommandJobs = $true
    $script:CronFlags = $true
    $script:OcExe = $OpenClaw
    foreach ($name in @("$OpenClaw.cmd", "$OpenClaw.exe", $OpenClaw)) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) {
            $script:OcExe = $cmd.Path
            $script:OcFound = $true
            break
        }
    }
    if (-not $script:OcFound) {
        if (-not ($DryRun -or $Uninstall)) {
            Stop-Install 'The openclaw CLI isn''t on PATH. Install OpenClaw (https://openclaw.ai) and connect Telegram, then run this again.'
        }
        Write-Warn 'The openclaw CLI isn''t on PATH; continuing without it.'
        return
    }
    if ($Uninstall) { return }
    $text = (Invoke-Native $script:OcExe @('--version')).Text
    if ($text -match '(\d{4})\.(\d{1,2})\.(\d{1,2})') {
        $version = [version]"$($matches[1]).$($matches[2]).$($matches[3])"
        if ($version -ge $MinCommandJobs) {
            Write-Ok "OpenClaw $version"
        } else {
            $script:CommandJobs = $false
            $script:CronFlags = ($version -ge $MinCronFlags)
            Write-Warn "OpenClaw $version is older than $MinCommandJobs, the first version that runs scheduled commands without a model call. Update it with: openclaw update"
        }
    } else {
        Write-Warn "Couldn't read the OpenClaw version ($(Get-FirstLine $text)); assuming $MinCommandJobs or newer."
    }
    $status = Invoke-Native $script:OcExe @('gateway', 'status', '--require-rpc', '--json')
    if ($status.Code -ne 0) { $status = Invoke-Native $script:OcExe @('health') }
    if ($status.Code -eq 0) {
        Write-Ok 'Gateway is running'
    } elseif ($DryRun) {
        Write-Warn 'The OpenClaw Gateway isn''t reachable (dry run continues).'
    } else {
        Stop-Install 'The OpenClaw Gateway isn''t reachable. Start it (''openclaw gateway start'', or open the OpenClaw app) and run this again.'
    }
    # Best effort: a Gateway that reports a Linux platform runs in WSL, where a Windows path to fbm.exe won't work.
    if (($status.Text -match '"platform"\s*:\s*"linux"') -and (-not $Force)) {
        Stop-Install 'Your OpenClaw Gateway runs on Linux (WSL). Install FBM inside that WSL distro and run openclaw/install.sh there (or pass -Force to continue anyway).'
    }
}

function Resolve-ChatId {
    Write-Step 'Telegram'
    $value = $ChatId
    if (-not $value) {
        $r = Invoke-Native $script:Py @($script:Helper, 'chat-id', '--settings', $script:Settings) -NoStderr
        if ($r.Code -eq 0) { $value = Get-FirstLine $r.Text }
    }
    if ((-not $value) -and $Interactive) {
        Write-Host '  Your chat id: send /whoami to your OpenClaw bot in Telegram (for a direct chat it''s your user id).'
        $value = Read-Host '  Telegram chat id'
    }
    if ($value) { $value = $value.Trim() }
    if ((-not $value) -or ($value -notmatch '^(-?\d{3,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$')) {
        Stop-Install "A Telegram chat id is needed for the posts: pass -ChatId ID or set [notify] telegram_chat_id in $($script:Settings)."
    }
    $script:ChatId = $value
    Write-Ok "Posts go to Telegram chat $value"
}

function Resolve-SkillDir {
    if ($script:SkillDir) { return }
    $ws = $env:OPENCLAW_WORKSPACE_DIR
    if (-not $ws) { $ws = Get-OpenClawConfig "agents.entries.$Agent.workspace" }
    if (-not $ws) { $ws = Get-OpenClawConfig 'agents.defaults.workspace' }
    if (-not $ws) {
        if ($env:OPENCLAW_STATE_DIR) {
            $ws = Join-Path $env:OPENCLAW_STATE_DIR 'workspace'
        } elseif ($env:OPENCLAW_PROFILE -and ($env:OPENCLAW_PROFILE -ne 'default')) {
            $ws = Join-Path $HOME ".openclaw-$($env:OPENCLAW_PROFILE)\workspace"
        } else {
            $ws = Join-Path $HOME '.openclaw\workspace'
        }
    }
    if ($ws.StartsWith('~')) { $ws = $HOME + $ws.Substring(1) }
    $script:SkillDir = Join-Path (Join-Path $ws 'skills') 'fbm'
}

function Install-Skill {
    Write-Step 'Chat skill'
    Resolve-SkillDir
    $argList = @($script:Helper, 'render', '--bin', $script:FbmBin, '--out', $script:SkillDir, '--repo', $script:Repo, '--platform', 'windows')
    if ($Force) { $argList += '--force' }
    $r = Invoke-Change $script:Py $argList
    if ($r.Code -ne 0) { Stop-Install "Couldn't write the skill: $(Get-FirstLine $r.Text)" }
    $script:State['skill_dir'] = $script:SkillDir
    Write-Ok "Skill 'fbm' in $($script:SkillDir) (with FBM's playbooks in references\)"
    if ($DryRun -or (-not $script:OcFound)) { return }
    # With several agents OpenClaw needs --agent here; an OpenClaw without the option gets the plain command.
    $r = Invoke-Native $script:OcExe @('skills', 'info', 'fbm', '--agent', $Agent)
    if ($r.Code -ne 0) { $r = Invoke-Native $script:OcExe @('skills', 'info', 'fbm') }
    if ($r.Code -eq 0) {
        Write-Ok 'OpenClaw sees the skill (start a new chat with /new if it''s open)'
    } elseif ((Get-OpenClawConfig 'skills.load.watch') -eq 'false') {
        Write-Warn 'OpenClaw doesn''t list the ''fbm'' skill yet: skills.load.watch is off, so restart the Gateway, then start a new chat (/new).'
    } else {
        Write-Warn 'OpenClaw doesn''t list the ''fbm'' skill yet. Start a new chat (/new) or restart the Gateway; if it''s still missing, re-run with -SkillDir <your agent workspace>\skills\fbm.'
    }
    Test-SkillAllowlist
}

# A skill allowlist (agents.entries.<agent>.skills, else agents.defaults.skills) hides every skill it doesn't name.
function Test-SkillAllowlist {
    foreach ($key in @("agents.entries.$Agent.skills", 'agents.defaults.skills')) {
        $r = Invoke-Native $script:OcExe @('config', 'get', $key, '--json') -NoStderr
        if (($r.Code -ne 0) -or ($r.Text -notmatch '(?s)^\[.*\]$')) { continue }  # not set: no JSON array
        try { $names = @(foreach ($v in ($r.Text | ConvertFrom-Json)) { [string]$v }) } catch { return }
        if ($names -notcontains 'fbm') {
            Write-Warn "Agent '$Agent' only uses the skills listed in $key, and 'fbm' isn't one: add it there (a list in agents.entries.$Agent.skills replaces the defaults), then restart the Gateway."
        }
        return
    }
}

function Send-TestMessage {
    if ($NoTestMessage -or (-not $script:OcFound)) { return }
    $r = Invoke-Change $script:OcExe @('message', 'send', '--channel', 'telegram', '--target', $script:ChatId, '--message', $TestMessage)
    if ($r.Code -eq 0) {
        Write-Ok 'Test message sent to Telegram'
    } else {
        Write-Warn "The Telegram test message failed: $(Get-FirstLine $r.Text). Check 'openclaw channels status' and the chat id."
    }
}

function Set-FailureAlert {
    Write-Step 'Failure alerts'
    if ($script:OcFound -and ((Invoke-Native $script:OcExe @('config', 'get', 'cron.failureAlert', '--json') -NoStderr).Code -eq 0)) {
        if ((Get-OpenClawConfig 'cron.failureAlert.enabled') -eq 'false') {
            Write-Warn 'cron.failureAlert is set but disabled (enabled: false): a failing fbm:health, fbm:snapshot or fbm:data-sync won''t alert you. Left unchanged.'
        } else {
            Write-Ok 'cron.failureAlert is already set; left unchanged'
        }
        return
    }
    $patch = Join-Path ([System.IO.Path]::GetTempPath()) ('fbm-openclaw-patch-' + [guid]::NewGuid().ToString('N') + '.json')
    $json = '{"cron":{"failureAlert":{"enabled":true,"mode":"announce","channel":"telegram","to":"' + $script:ChatId + '"}}}'
    [System.IO.File]::WriteAllText($patch, $json, (New-Object System.Text.UTF8Encoding($false)))
    if ($DryRun) { Write-Host "  patch file: $json" }
    try {
        $r = Invoke-Change $script:OcExe @('config', 'patch', '--file', $patch)
    } finally {
        Remove-Item -LiteralPath $patch -ErrorAction SilentlyContinue
    }
    if ($r.Code -eq 0) {
        if (-not $DryRun) { $script:State['failure_alert'] = '1' }
        Write-Ok "A failed job (e.g. fbm health) now alerts Telegram chat $($script:ChatId) (cron.failureAlert)"
    } else {
        Write-Warn "Couldn't set cron.failureAlert: $(Get-FirstLine $r.Text)"
    }
}

function Test-ExecPolicy {
    Write-Step 'Chat commands (exec policy)'
    if (-not $script:OcFound) { return }
    $mode = Get-OpenClawConfig "agents.entries.$Agent.tools.exec.mode"
    if (-not $mode) { $mode = Get-OpenClawConfig 'tools.exec.mode' }
    if ((-not $mode) -or ($mode -eq 'full')) {
        Write-Ok 'exec mode is full: the agent can run FBM without approval prompts'
    } elseif ($mode -eq 'deny') {
        Write-Warn 'exec is denied (tools.exec.mode=deny): the agent can''t run FBM in chat. Scheduled posts still work.'
    } elseif (Resolve-Choice $Allowlist "exec mode is '$mode'. Add FBM to the exec allowlist so chat questions run without prompts?" $true) {
        $r = Invoke-Change $script:OcExe @('approvals', 'allowlist', 'add', '--agent', $Agent, $script:FbmBin)
        if ($r.Code -eq 0) {
            if (-not $DryRun) { $script:State['allowlist'] = '1' }
            Write-Ok "FBM is on the exec allowlist for agent '$Agent'"
        } else {
            Write-Warn "Couldn't update the allowlist: $(Get-FirstLine $r.Text)"
        }
    } else {
        Write-Warn "Skipped. To allow it later: openclaw approvals allowlist add --agent $Agent '$($script:FbmBin)'"
    }
}

function Get-ExistingJobs {
    $script:Existing = @()
    if (-not $script:OcFound) { return }
    $r = Invoke-Native $script:OcExe @('cron', 'list', '--all', '--json') -NoStderr
    if ($r.Code -ne 0) { $r = Invoke-Native $script:OcExe @('cron', 'list', '--json') -NoStderr }
    if ($r.Code -ne 0) {
        Write-Warn 'Couldn''t list existing OpenClaw jobs; same-named fbm:* jobs may be duplicated.'
        return
    }
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ('fbm-openclaw-jobs-' + [guid]::NewGuid().ToString('N') + '.json')
    [System.IO.File]::WriteAllText($file, $r.Text, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $ids = Invoke-Native $script:Py @($script:Helper, 'job-ids', '--prefix', $JobPrefix, '--file', $file) -NoStderr
    } finally {
        Remove-Item -LiteralPath $file -ErrorAction SilentlyContinue
    }
    if ($ids.Code -ne 0) { return }
    foreach ($line in ($ids.Text -split "`r?`n")) {
        $cols = $line.Split("`t")
        if ($cols.Count -eq 2) { $script:Existing += [pscustomobject]@{ Id = $cols[0]; Name = $cols[1] } }
    }
}

$script:FbmHas = @{}
function Test-FbmCommand([string]$Words) {
    if (-not (Test-Path -LiteralPath $script:FbmBin)) { return $true }
    if (-not $script:FbmHas.ContainsKey($Words)) {
        $argList = @($Words.Split(' ')) + @('--help')
        $script:FbmHas[$Words] = ((Invoke-Native $script:FbmBin $argList -NoStderr).Code -eq 0)
    }
    return $script:FbmHas[$Words]
}

# The --command-argv value: a JSON array OpenClaw runs as is, with no shell (a --command job runs `sh -lc` even on
# Windows, and sh eats a Windows path's backslashes). It has to arrive as one argument through Windows PowerShell 5.1,
# which doesn't escape quotes inside an argument, and the openclaw.cmd shim (cmd.exe): so nothing outside
# [A-Za-z0-9_.:/-] goes out literally. Spaces and other characters become JSON \u escapes, and each JSON quote is \",
# which Node reads back as ".
function ConvertTo-ArgvJson([string[]]$Words) {
    $items = foreach ($word in $Words) {
        $text = New-Object System.Text.StringBuilder
        foreach ($ch in $word.ToCharArray()) {
            if ($ch -cmatch '[A-Za-z0-9_.:/-]') { [void]$text.Append($ch) } else { [void]$text.Append(('\u{0:x4}' -f [int]$ch)) }
        }
        '\"' + $text.ToString() + '\"'
    }
    return '[' + (@($items) -join ',') + ']'
}

function Register-Jobs {
    Write-Step 'Scheduled posts'
    $script:Registered = @()
    $script:Skipped = @()
    if ($NoJobs) {
        Write-Ok 'Skipped (-NoJobs)'
        return
    }
    $useFallback = $false
    if (-not $script:CommandJobs) {
        $question = 'Schedule the morning digest and weekly preview as agent turns instead (each run is one model call)?'
        if ($script:CronFlags -and ($Fallback -or (Confirm-Choice $question $false))) {
            $useFallback = $true
        } else {
            Write-Warn "No jobs scheduled: update OpenClaw ('openclaw update') and run this again. Meanwhile FBM's own scheduler can run data sync and health: & '$($script:FbmBin)' automation install --mode engine-only --jobs data-sync,health"
            return
        }
    }
    Get-ExistingJobs
    $program = $script:FbmBin.Replace('\', '/')  # Windows takes forward slashes too; the JSON stays readable
    foreach ($job in $JobTable) {
        $name = $job.Name
        if (-not (Test-JobSelected $name)) { continue }
        $expr = Get-JobSchedule $name $job.Cron
        $words = @()
        foreach ($word in $job.Run.Split(' ')) {
            if ($word.StartsWith('-')) { break }  # 'digest --kind daily' -> 'digest'
            $words += $word
        }
        $words = $words -join ' '
        if (-not (Test-FbmCommand $words)) {
            Write-Warn "$name skipped: this FBM has no 'fbm $words' yet. Update FBM (git pull, uv sync) and run this again."
            $script:Skipped += $name
            continue
        }
        if ($useFallback) {
            if ($FallbackJobs -notcontains $name) {
                $script:Skipped += $name
                continue
            }
            $message = "Scheduled FBM post. Use the fbm skill: run its FBM program once with exec, with the arguments: $($job.Run). Reply with exactly what it printed and nothing else. If it printed only NO_REPLY, reply exactly NO_REPLY."
            $argList = @('cron', 'add', '--name', $name, '--cron', $expr, '--tz', $JobTz, '--exact', '--agent', $Agent,
                '--session', 'isolated', '--message', $message, '--announce', '--channel', 'telegram', '--to', $script:ChatId)
        } else {
            $argv = ConvertTo-ArgvJson (@($program) + $job.Run.Split(' '))
            $argList = @('cron', 'add', '--name', $name, '--cron', $expr, '--tz', $JobTz, '--exact',
                '--command-argv', $argv, '--command-cwd', $script:Repo, '--timeout-seconds', [string]$job.Timeout)
            if ($job.Delivery -eq 'announce') {
                $argList += @('--announce', '--channel', 'telegram', '--to', $script:ChatId)
            } else {
                $argList += '--no-deliver'
            }
        }
        $oldIds = @($script:Existing | Where-Object { $_.Name -eq $name } | ForEach-Object { $_.Id })
        $r = Invoke-Change $script:OcExe $argList
        if ($r.Code -ne 0) {
            Write-Warn "${name}: couldn't register it: $(Get-FirstLine $r.Text)"
            $script:Skipped += $name
            continue
        }
        foreach ($id in $oldIds) {  # replace the previous version only once the new one exists
            $rm = Invoke-Change $script:OcExe @('cron', 'remove', $id)
            if ($rm.Code -ne 0) { Write-Warn "${name}: couldn't remove the old job ${id}: $(Get-FirstLine $rm.Text)" }
        }
        $script:Registered += $name
        $where = ''
        if ($job.Delivery -eq 'announce') { $where = '  (posts to Telegram)' }
        Write-Ok "$name  $expr (ET)  ->  fbm $($job.Run)$where"
    }
}

function Set-NotifyChannel {
    Write-Step 'FBM''s own notifications'
    $question = 'Also let FBM send its own alerts (fbm ... --send) through OpenClaw? This adds "openclaw" to [notify] channels in config\settings.toml.'
    if (Resolve-Choice $NotifyOpenClaw $question $false) {
        $r = Invoke-Change $script:Py @($script:Helper, 'notify-channel', '--settings', $script:Settings, '--add', 'openclaw',
            '--chat-id', $script:ChatId, '--openclaw-bin', $script:OcExe)
        if ($r.Code -eq 0) {
            if (-not $DryRun) { $script:State['notify_channel'] = '1' }
            Write-Ok "$(Get-FirstLine $r.Text) (backup: settings.toml.bak)"
        } else {
            Write-Warn "Couldn't update $($script:Settings): $(Get-FirstLine $r.Text)"
        }
    } else {
        Write-Ok 'Left config\settings.toml unchanged (the scheduled posts don''t need it)'
    }
}

function Test-FbmScheduler {
    if (-not (Test-Path -LiteralPath $script:FbmBin)) { return }
    $r = Invoke-Native $script:FbmBin @('automation', 'list') -NoStderr
    if ((-not $r.Text) -or ($r.Text -match 'No FBM scheduler entries')) { return }
    Write-Warn "FBM's own scheduler also has jobs ('fbm automation list'), so digests and alerts would arrive twice. OpenClaw now runs all of that work: remove them and keep 'fbm setup' from installing them again: & '$($script:FbmBin)' automation remove; & '$($script:FbmBin)' setup --automation none --skip-data --yes"
}

function Write-Summary {
    Write-Step 'Done'
    if ($DryRun) { Write-Host '  Dry run: nothing was changed.' }
    if ($script:Registered.Count -gt 0) { Write-Host "  Jobs: $($script:Registered -join ' ')  (times are US/Eastern; list them: openclaw cron list)" }
    if ($script:Skipped.Count -gt 0) { Write-Host "  Not scheduled: $($script:Skipped -join ' ')" }
    Write-Host '  Try it in Telegram: "how''s my matchup?", "set today''s lineup", "any pickups for steals?"'
    Write-Host '  Run a job now: openclaw cron run <job-id>   History: openclaw cron runs <job-id>'
    Write-Host "  Remove everything: powershell -ExecutionPolicy Bypass -File '$PSScriptRoot\install.ps1' -Uninstall"
}

function Invoke-Uninstall {
    Write-Step 'Uninstall'
    Read-State
    if ((-not $script:SkillDir) -and $script:State['skill_dir']) { $script:SkillDir = $script:State['skill_dir'] }
    Resolve-SkillDir
    if ($script:OcFound) {
        if ($script:Py) { Get-ExistingJobs } else { $script:Existing = @() }
        if (@($script:Existing).Count -eq 0) { Write-Ok 'No fbm:* jobs found in OpenClaw' }
        foreach ($job in $script:Existing) {
            $r = Invoke-Change $script:OcExe @('cron', 'remove', $job.Id)
            if ($r.Code -eq 0) { Write-Ok "Removed job $($job.Name)" } else { Write-Warn "Couldn't remove $($job.Name) ($($job.Id)): $(Get-FirstLine $r.Text)" }
        }
        $bin = $script:FbmBin
        if ($script:State['fbm_bin']) { $bin = $script:State['fbm_bin'] }
        if ((Invoke-Change $script:OcExe @('approvals', 'allowlist', 'remove', '--agent', $Agent, $bin)).Code -eq 0) {
            Write-Ok 'FBM is off the exec allowlist (if it was there)'
        }
        if ($script:State['failure_alert'] -eq '1') {
            if ((Get-OpenClawConfig 'cron.failureAlert.to') -eq $script:State['chat_id']) {
                if ((Invoke-Change $script:OcExe @('config', 'unset', 'cron.failureAlert')).Code -eq 0) { Write-Ok 'Removed the cron.failureAlert this script set' }
            } else {
                Write-Warn 'cron.failureAlert was changed since install; left as is'
            }
        }
    } else {
        Write-Warn 'openclaw CLI not found: remove the fbm:* jobs yourself (openclaw cron list, openclaw cron remove <id>).'
    }
    $skillFile = Join-Path $script:SkillDir 'SKILL.md'
    if ((Test-Path -LiteralPath $skillFile) -and (Select-String -LiteralPath $skillFile -Pattern $Marker -SimpleMatch -Quiet)) {
        if ($DryRun) {
            Write-Host "  [dry-run] Remove-Item -Recurse -Force '$($script:SkillDir)'"
        } else {
            Remove-Item -LiteralPath $script:SkillDir -Recurse -Force
        }
        Write-Ok "Removed the skill folder $($script:SkillDir)"
    } elseif (Test-Path -LiteralPath $script:SkillDir) {
        Write-Warn "$($script:SkillDir) wasn't created by FBM; left as is"
    } else {
        Write-Ok "No skill folder at $($script:SkillDir)"
    }
    if (($script:State['notify_channel'] -eq '1') -and $script:Py) {
        $r = Invoke-Change $script:Py @($script:Helper, 'notify-channel', '--settings', $script:Settings, '--remove', 'openclaw')
        if ($r.Code -eq 0) { Write-Ok 'Removed "openclaw" from FBM''s [notify] channels' }
    }
    if ((-not $DryRun) -and (Test-Path -LiteralPath $script:StateFile)) { Remove-Item -LiteralPath $script:StateFile }
    Write-Step 'Done'
    if ($DryRun) { Write-Host '  Dry run: nothing was changed.' }
    Write-Host '  FBM itself is untouched; run openclaw\install.ps1 to connect it again.'
}

# ------------------------------------------------------------------ main

$title = 'FBM x OpenClaw'
if ($DryRun) { $title += ' (dry run)' }
Write-Host $title -ForegroundColor White
Assert-JobNames
Find-Fbm
Find-OpenClaw
if ($Uninstall) {
    Invoke-Uninstall
    exit 0
}
Read-State
Resolve-ChatId
$script:State['chat_id'] = $script:ChatId
$script:State['fbm_bin'] = $script:FbmBin
Install-Skill
Send-TestMessage
Set-FailureAlert
Test-ExecPolicy
Register-Jobs
Set-NotifyChannel
Test-FbmScheduler
Save-State
Write-Summary
