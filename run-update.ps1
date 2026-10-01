[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

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
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    & $git @GitArgs 2>&1 | ForEach-Object { Log $_ }
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
    & $git -c http.proxy= -c https.proxy= @GitArgs 2>&1 | ForEach-Object { Log $_ }
    $rc = $LASTEXITCODE
    foreach ($n in $names) {
        if ($saved[$n]) { [Environment]::SetEnvironmentVariable($n, $saved[$n]) }
    }
    return $rc
}

Set-Location $repo
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
Log 'step 0/3 sync with origin'
$rc = Invoke-Git pull --rebase --autostash origin main
if ($rc -ne 0) { Log "FAIL git pull --rebase exit=$rc"; exit 1 }

# The scraper deps live in Miniconda; do not assume `python` is on PATH in the
# task scheduler / Hermes cron context.
$py = 'python'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $condaPy = 'C:\ProgramData\Miniconda3\python.exe'
    if (Test-Path $condaPy) { $py = $condaPy; Log "python not on PATH - using $condaPy" }
    else { Log 'FAIL python not found on PATH or in Miniconda'; exit 1 }
}

Log 'step 1/3 fetch'
& $py fetch.py -o data/topics.jsonl 2>&1 | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) { Log "FAIL fetch exit=$LASTEXITCODE"; exit 1 }

Log 'step 2/3 generate'
& $py generate.py -i data/topics.jsonl -o docs/index.html 2>&1 | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) { Log "FAIL generate exit=$LASTEXITCODE"; exit 1 }

Log 'step 3/3 commit + push'
& $git config user.name 'linuxsb-daily-bot'
& $git config user.email 'bot@users.noreply.github.com'

# -A over the two data dirs: tolerates a missing data/seen_ids.txt instead of
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
        if ($rc -ne 0) { Log "FAIL git push exit=$rc"; exit 1 }
    }
    Log 'push ok'
}
Log '=== update end ==='
exit 0
