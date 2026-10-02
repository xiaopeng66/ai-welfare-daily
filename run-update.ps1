[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

# Hide the console window. The scheduled task launches powershell.exe with a
# visible console at 08:00 and 20:00 unless -WindowStyle Hidden is on it (the
# task XML and the Hermes cron wrapper both pass that flag now); this block is
# the second layer, so a manual run or a task registered without the flag is
# invisible too.
#
# Use GetConsoleWindow(), NOT (Get-Process -Id $PID).MainWindowHandle. Measured
# 2026-10-02: MainWindowHandle is 0 whenever powershell was started from WSL, so
# ShowWindow(0, 0) was a silent no-op there, and it only happened to work from
# Task Scheduler. GetConsoleWindow() returns the real console handle on both
# launch paths.
$Console = Add-Type -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@ -Name Con -Namespace Native -PassThru
$hWnd = $Console::GetConsoleWindow()
if ($hWnd -ne [IntPtr]::Zero) { [void]$Console::ShowWindow($hWnd, 0) }


$repo = 'E:\AI\Hermes\scripts\linuxsb-daily'
$log  = Join-Path $repo 'update.log'
$git  = 'C:\Program Files\Git\cmd\git.exe'
if (-not (Test-Path $git)) { $git = 'git' }

function Log($msg) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
    Write-Host $line
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

# Network git commands with a direct-connection retry. Clash only starts at
# logon, so a run triggered while nobody is logged on (Hermes cron) has no
# proxy to reach, and the user-level http.proxy setting would fail the command.
# Abort a transfer that stalls instead of hanging on it for the whole run.
$gitStall = @('-c', 'http.lowSpeedLimit=1000', '-c', 'http.lowSpeedTime=45')

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    & $git @gitStall @GitArgs 2>&1 | ForEach-Object { Log $_ }
    if ($LASTEXITCODE -eq 0) { return 0 }

    # Retry over a direct connection. Two different things point git at Clash:
    # the user-level http.proxy setting, and the HTTP(S)_PROXY env vars Hermes
    # passes down to cmd.exe. When Clash is not running -- e.g. a run triggered
    # with nobody logged on, since Clash itself starts at logon -- both have to
    # be bypassed, not just the config, or the retry fails the same way.
    Log "  [retry] git $($GitArgs -join ' ') - proxy unreachable, retrying direct"
    $names = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
    $saved = @{}
    foreach ($n in $names) {
        $saved[$n] = [Environment]::GetEnvironmentVariable($n)
        [Environment]::SetEnvironmentVariable($n, $null)
    }
    & $git @gitStall -c http.proxy= -c https.proxy= @GitArgs 2>&1 | ForEach-Object { Log $_ }
    $rc = $LASTEXITCODE
    foreach ($n in $names) {
        if ($saved[$n]) { [Environment]::SetEnvironmentVariable($n, $saved[$n]) }
    }
    return $rc
}

$py = 'python'

# Run a python step and copy its output into the log.
#
# Do NOT go back to `& $py ... 2>&1 | ForEach-Object { Log $_ }`: that pipeline
# waits for EOF on the child stdout handle, and Playwright's fingerprint helper
# processes inherit it. When the fetch's python exits but a helper survives, EOF
# never arrives and the run blocks forever at 0% CPU (observed: wedged 14+
# minutes mid-fetch). Redirecting to files and waiting on the process handle is
# immune to that, and the timeout guarantees no run can wedge indefinitely.
function Run-Py {
    param([string]$Label, [string[]]$PyArgs)
    $outFile = Join-Path $env:TEMP ('lsb-out-' + [guid]::NewGuid().ToString('N') + '.log')
    $errFile = Join-Path $env:TEMP ('lsb-err-' + [guid]::NewGuid().ToString('N') + '.log')
    # python on Windows writes the ANSI code page when stdout is redirected, which
    # the log read below then renders as mojibake. Pin both sides to UTF-8.
    $env:PYTHONIOENCODING = 'utf-8'
    # -Wait is required: PowerShell 5.1 only fills in ExitCode when Start-Process
    # is called with it (verified: -PassThru alone leaves it empty even after
    # WaitForExit). A step that still wedges is killed by the reaper at the top
    # of the next trigger, which is what the mutex makes safe.
    $proc = Start-Process -FilePath $py -ArgumentList $PyArgs -NoNewWindow -Wait -PassThru `
                          -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $rc = $proc.ExitCode
    if ($null -eq $rc) { $rc = 1 }
    foreach ($f in @($outFile, $errFile)) {
        if (Test-Path $f) {
            Get-Content -LiteralPath $f -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Log $_ }
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        }
    }
    Log ("$Label exit code $rc")
    return $rc
}

Set-Location $repo

# Reap a run that wedged. Scheduled task and Hermes cron both fire at
# 08:00/20:00, and a wedged run holds the resources the next one needs.
$self = $PID
foreach ($proc in @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'")) {
    if ($proc.ProcessId -ne $self -and $proc.CommandLine -match 'run-update\.ps1' -and
        $proc.CreationDate -lt (Get-Date).AddMinutes(-10)) {
        Write-Host ('reaping wedged run PID ' + $proc.ProcessId)
        & taskkill /T /F /PID $proc.ProcessId 2>&1 | Out-Null
    }
}

# One run at a time: the two triggers fire on the same wall clock and two
# concurrent fetches fight over Cloudflare and the git branch. Verified on
# 2026-10-01 20:00: without a working lock the scheduled task and the Hermes cron
# both fetched and both committed into the same worktree.
$mutex = $null
$locked = $false
try {
    $mutex = New-Object System.Threading.Mutex($false, 'Global\linuxsb-daily-run')
    $locked = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $locked = $true   # holder was killed - ownership is ours now
} catch {
    # Global\ creation can be refused outright (no SeCreateGlobalPrivilege).
    # Falling straight through would disable the lock silently, so retry under
    # Local\ - both triggers run in the same user session.
    Log ('global mutex unavailable (' + $_.Exception.GetType().Name + ') - retrying under Local\')
    try {
        $mutex = New-Object System.Threading.Mutex($false, 'Local\linuxsb-daily-run')
        $locked = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $locked = $true
    } catch {
        Log ('no mutex available (' + $_.Exception.GetType().Name + ') - proceeding unlocked')
        $locked = $true
    }
}
if (-not $locked) {
    Write-Host 'another run is already in progress - exiting'
    Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] skipped: another run holds the lock') -Encoding UTF8
    exit 0
}

Log '=== update start ==='

# Self-heal a checkout left mid-rebase/mid-merge by an earlier interrupted run
# (shutdown or task kill while pulling). Without this every later automated run
# dies on "a rebase is in progress" and needs a human to unblock it.
$gitDir = Join-Path $repo '.git'
if ((Test-Path (Join-Path $gitDir 'rebase-merge')) -or (Test-Path (Join-Path $gitDir 'rebase-apply'))) {
    Log 'stale rebase found - aborting before sync'
    & $git rebase --abort 2>&1 | ForEach-Object { Log $_ }
}
if (Test-Path (Join-Path $gitDir 'MERGE_HEAD')) {
    Log 'stale merge found - aborting before sync'
    & $git merge --abort 2>&1 | ForEach-Object { Log $_ }
}

# Sync BEFORE fetching. GitHub Actions runs the same job on the same wall-clock
# schedule (0 0,12 * * * UTC == 08:00/20:00 CST), so starting from a stale tip
# makes the push below non-fast-forward and loses this run's data.
#
# A tracked file left dirty outside data/docs is committed by nothing here and
# makes the rebase refuse to run ("cannot rebase: You have unstaged changes").
# That is exactly how the 2026-10-01 20:00 GitHub Actions run went red after
# .gitattributes landed and renormalized run-update.ps1. Print the dirty list so
# the next occurrence is diagnosable instead of invisible.
$dirty = @(& $git status --porcelain)
if ($dirty.Count -gt 0) {
    Log 'working tree not clean:'
    $dirty | ForEach-Object { Log ('  ' + $_) }
}

Log 'step 0/3 sync with origin'
$rc = Invoke-Git pull --rebase --autostash origin main
if ($rc -ne 0) {
    # Do not lose the whole update over a sync problem: clear any half-applied
    # rebase and carry on with the fetch. The push path below reconciles with
    # origin and still fails loudly if it cannot.
    Log "WARN git pull --rebase exit=$rc - clearing any partial rebase and continuing"
    & $git rebase --abort 2>&1 | ForEach-Object { Log $_ }
}

# The scraper deps live in Miniconda; do not assume `python` is on PATH in the
# task scheduler / Hermes cron context.
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $condaPy = 'C:\ProgramData\Miniconda3\python.exe'
    if (Test-Path $condaPy) { $py = $condaPy; Log "python not on PATH - using $condaPy" }
    else { Log 'FAIL python not found on PATH or in Miniconda'; exit 1 }
}

Log 'step 1/3 fetch'
$rc = Run-Py -Label 'fetch' -PyArgs @('fetch.py', '-o', 'data/topics.jsonl')
if ($rc -ne 0) { Log "FAIL fetch exit=$rc"; exit 1 }

# fetch.py writes data\.fetch_errors when a source failed but the run is still
# survivable (cached rows kept). CI turns that marker into a red build; nothing
# on this box read it, so a source silently vanishing stayed invisible here.
# That matters most for linux.do: it is only fetched from this residential IP,
# so a linux.do failure never shows up in CI either. Log it.
$fetchErr = Join-Path $repo 'data\.fetch_errors'
if (Test-Path $fetchErr) {
    Log 'WARN partial fetch - cached rows kept for the sources below:'
    Get-Content -LiteralPath $fetchErr -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Log ('  ' + $_) }
}

Log 'step 2/3 generate'
$rc = Run-Py -Label 'generate' -PyArgs @('generate.py', '-i', 'data/topics.jsonl', '-o', 'docs/index.html')
if ($rc -ne 0) { Log "FAIL generate exit=$rc"; exit 1 }

Log 'step 3/3 commit + push'
& $git config user.name 'linuxsb-daily-bot'
& $git config user.email 'bot@users.noreply.github.com'

# -A over the two data dirs: tolerates a missing data/ file instead of
# failing the whole run on git add.
& $git add -A -- data docs
if ($LASTEXITCODE -ne 0) { Log "FAIL git add exit=$LASTEXITCODE"; exit 1 }

& $git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Log 'no changes to commit'
} else {
    & $git commit -m ('chore: daily welfare update ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')) 2>&1 | ForEach-Object { Log $_ }
    if ($LASTEXITCODE -ne 0) { Log "FAIL git commit exit=$LASTEXITCODE"; exit 1 }
    # Retry once: the other runner may still have pushed while we were fetching.
    $rc = Invoke-Git push origin main
    if ($rc -ne 0) {
        Log 'push rejected - rebasing onto origin and retrying once'
        $rc = Invoke-Git pull --rebase --autostash origin main
        if ($rc -eq 0) {
            $rc = Invoke-Git push origin main
        }
        if ($rc -ne 0) {
            # Rows are only rewritten when they change now, so the two runners
            # mostly touch disjoint lines and a rebase usually just works. If it
            # does not, keep this run's snapshot - it is a complete fetch.
            Log 'rebase conflicted - aborting and merging onto origin'
            & $git rebase --abort 2>&1 | ForEach-Object { Log $_ }
            $rc = Invoke-Git pull --no-rebase -X ours origin main
            if ($rc -eq 0) {
                $rc = Invoke-Git push origin main
            }
        }
        if ($rc -ne 0) { Log "FAIL git push exit=$rc"; exit 1 }
    }
    Log 'push ok'
}
Log '=== update end ==='
exit 0
