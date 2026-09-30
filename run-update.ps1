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

Set-Location $repo
Log '=== update start ==='

Log 'step 1/3 fetch'
& python fetch.py -o data/topics.jsonl 2>&1 | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) { Log "FAIL fetch exit=$LASTEXITCODE"; exit 1 }

Log 'step 2/3 generate'
& python generate.py -i data/topics.jsonl -o docs/index.html 2>&1 | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) { Log "FAIL generate exit=$LASTEXITCODE"; exit 1 }

Log 'step 3/3 commit + push'
& $git config user.name 'linuxsb-daily-bot'
& $git config user.email 'bot@users.noreply.github.com'

& $git add -- data/topics.jsonl
& $git add -- data/seen_ids.txt
& $git add -- docs/index.html
if ($LASTEXITCODE -ne 0) { Log "FAIL git add exit=$LASTEXITCODE"; exit 1 }

& $git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Log 'no changes to commit'
} else {
    & $git commit -m ('chore: daily welfare update ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')) 2>&1 | ForEach-Object { Log $_ }
    if ($LASTEXITCODE -ne 0) { Log "FAIL git commit exit=$LASTEXITCODE"; exit 1 }
    & $git push origin main 2>&1 | ForEach-Object { Log $_ }
    if ($LASTEXITCODE -ne 0) { Log "FAIL git push exit=$LASTEXITCODE"; exit 1 }
    Log 'push ok'
}
Log '=== update end ==='
exit 0
