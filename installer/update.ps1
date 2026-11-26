# Студія моментів: оновлення до найновішої версії з GitHub.
# Запускається кнопкою «Оновити» в студії або файлом «Оновити.bat».
# Твої дані не чіпаються: ключ (.env), налаштування, історія відео й самі відео лишаються.

param([switch]$FromStudio)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root  = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root "tools"
$Py    = Join-Path $Root ".venv\Scripts\python.exe"
$UvExe = Join-Path $Tools "uv\uv.exe"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR          = Join-Path $Tools "uv-cache"
$env:UV_LINK_MODE          = "copy"

# Що ніколи не перезаписуємо
$Keep = @(".env", "user_settings.json", "studio_data.db", "studio_data.db-wal", "studio_data.db-shm",
          ".venv", "tools", "web_output", "web_uploads", ".transcript_cache", ".render_tmp",
          "install.log", "update.log", "shorts_automator.log", "backups", "gemini_quota_state.json",
          "gemini_models_check.json")

function Run($exe, [string[]]$argv) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $exe @argv 2>&1 | ForEach-Object { "$_" } | Out-Host
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    return $code
}

Start-Transcript -Path (Join-Path $Root "update.log") -Force | Out-Null
try {
    Write-Host "Студія моментів: оновлення" -ForegroundColor Yellow

    if (Test-Path (Join-Path $Root ".git")) {
        Write-Host "Це робоча копія з git. Оновлюй її через git pull, а не цим файлом." -ForegroundColor Yellow
        exit 0
    }

    $info = Get-Content (Join-Path $Root "version.json") -Raw -Encoding UTF8 | ConvertFrom-Json

    # Чекаємо, поки студія закриється (кнопка «Оновити» сама її зупиняє)
    for ($i = 0; $i -lt 30; $i++) {
        $busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
        if (-not $busy) { break }
        if ($i -eq 0) { Write-Host "Чекаю, поки студія закриється..." }
        Start-Sleep -Seconds 1
    }
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
        throw "Студія досі працює. Закрий її чорне вікно і запусти «Оновити» ще раз."
    }

    Write-Host "Шукаю нову версію..."
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$($info.repo)/releases/latest" -UseBasicParsing -Headers @{ "User-Agent" = "studio-momentiv-updater" }
    $latest = $rel.tag_name.TrimStart("v")
    if ([version]$latest -le [version]$info.version) {
        Write-Host "У тебе вже найновіша версія ($($info.version))." -ForegroundColor Green
    } else {
        $asset = $rel.assets | Where-Object { $_.name -eq $info.asset } | Select-Object -First 1
        if (-not $asset) { throw "У релізі $latest немає файлу $($info.asset)." }

        Write-Host "Качаю версію $latest..."
        $tmp = Join-Path $env:TEMP ("studio-update-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $zip = Join-Path $tmp "update.zip"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing -TimeoutSec 600
        Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp "x") -Force

        # Архів може мати одну верхню папку
        $src = Join-Path $tmp "x"
        $inner = Get-ChildItem $src
        if ($inner.Count -eq 1 -and $inner[0].PSIsContainer) { $src = $inner[0].FullName }

        Get-ChildItem $src -Force | Where-Object { $Keep -notcontains $_.Name } | ForEach-Object {
            Copy-Item $_.FullName -Destination $Root -Recurse -Force
        }
        Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike (Join-Path $Root ".venv*") -and $_.FullName -notlike (Join-Path $Root "tools*") } |
            Unblock-File -ErrorAction SilentlyContinue
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

        Write-Host "Оновлюю бібліотеки..."
        if ((Run $UvExe @("pip", "install", "--python", $Py, "-r", (Join-Path $Root "requirements-base.txt"))) -ne 0) { throw "Не вдалося оновити бібліотеки." }
        if (Test-Path (Join-Path $Root ".venv\Lib\site-packages\nvidia")) {
            Run $UvExe @("pip", "install", "--python", $Py, "-r", (Join-Path $Root "requirements-gpu.txt")) | Out-Null
        }
        Run $UvExe @("cache", "clean") | Out-Null
        Write-Host "Готово! Оновлено до версії $latest." -ForegroundColor Green
    }

    if ($FromStudio) {
        Start-Process -FilePath (Join-Path $Root "Запустити.bat") -WorkingDirectory $Root -WindowStyle Minimized
        Start-Sleep -Seconds 2
    }
}
catch {
    Write-Host ""
    Write-Host "Оновлення не вдалося: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Студія лишилась як була. Можна просто запустити її з ярлика." -ForegroundColor Yellow
    if ($FromStudio) { Read-Host "Натисни Enter, щоб закрити" | Out-Null }
}
finally {
    Stop-Transcript | Out-Null
}
