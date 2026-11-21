# Студія моментів: встановлення на Windows.
# Запускається з «Встановити.bat». Усе ставиться в цю ж папку (tools\, .venv\),
# нічого не потрапляє в систему, крім ярлика на робочому столі.
# Повторний запуск безпечний: він лише докачує те, чого бракує.

param([switch]$Quiet)   # -Quiet: без питання «Запустити зараз?» (для автоматичної перевірки)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # інакше Invoke-WebRequest у PowerShell 5 качає в рази повільніше
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root  = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root "tools"
$Venv  = Join-Path $Root ".venv"
$Py    = Join-Path $Venv "Scripts\python.exe"
$UvExe = Join-Path $Tools "uv\uv.exe"
$FfBin = Join-Path $Tools "ffmpeg\bin"
$Log   = Join-Path $Root "install.log"

$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR          = Join-Path $Tools "uv-cache"
$env:UV_LINK_MODE          = "copy"

function Step($n, $total, $text) { Write-Host ""; Write-Host "[$n/$total] $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    !   $text" -ForegroundColor Yellow }

function Fail($text) {
    Write-Host ""
    Write-Host "Не вдалося встановити студію." -ForegroundColor Red
    Write-Host $text -ForegroundColor Red
    Write-Host ""
    Write-Host "Що зробити: перевір інтернет і запусти «Встановити» ще раз." -ForegroundColor Yellow
    Write-Host "Якщо не допомогло, надішли файл install.log з цієї папки тому, хто дав тобі студію."
    exit 1
}

# Запускає програму так, щоб увесь її вивід (і помилки) потрапили у вікно та в install.log.
# У PowerShell 5 рядки stderr при ErrorActionPreference=Stop стають фатальними, тому тут Continue.
function Run($exe, [string[]]$argv) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $exe @argv 2>&1 | ForEach-Object { "$_" } | Out-Host
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    return $code
}

function Download($url, $dest) {
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 600
            return
        } catch {
            if ($i -eq 3) { throw }
            Start-Sleep -Seconds (3 * $i)
        }
    }
}

Start-Transcript -Path $Log -Force | Out-Null
try {
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "   Студія моментів: встановлення" -ForegroundColor Yellow
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "Папка: $Root"
    Write-Host "Перше встановлення займає 10-30 хвилин (качається 3-5 ГБ). Не закривай це вікно."

    if ($Root -match "OneDrive") {
        Warn "Студія лежить у папці OneDrive. Краще перенести її, наприклад, у C:\StudioMomentiv: OneDrive синхронізує гігабайти відео."
    }

    # Файли, скачані з інтернету, Windows позначає як «небезпечні». Знімаємо позначку з наших.
    Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "$Venv*" -and $_.FullName -notlike "$Tools*" } |
        Unblock-File -ErrorAction SilentlyContinue

    New-Item -ItemType Directory -Force -Path $Tools | Out-Null
    $total = 6

    # 1. uv: швидкий установник Python і бібліотек
    Step 1 $total "Готую установник Python (uv)"
    if (-not (Test-Path $UvExe)) {
        $zip = Join-Path $Tools "uv.zip"
        Download "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $Tools "uv") -Force
        Remove-Item $zip -Force
    }
    if (-not (Test-Path $UvExe)) { Fail "Не знайшов uv.exe після завантаження." }
    Ok "uv готовий"

    # 2. Python 3.12 у .venv
    Step 2 $total "Ставлю Python 3.12"
    if (-not (Test-Path $Py)) {
        if ((Run $UvExe @("venv", $Venv, "--python", "3.12")) -ne 0) { Fail "uv не зміг створити середовище Python." }
    }
    Ok (& $Py --version)

    # 3. Відеокарта
    Step 3 $total "Перевіряю відеокарту"
    $gpu = $null
    try {
        $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if (-not $smi -and (Test-Path "$env:WINDIR\System32\nvidia-smi.exe")) { $smi = "$env:WINDIR\System32\nvidia-smi.exe" }
        if ($smi) { $gpu = (& $smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1) }
    } catch { $gpu = $null }
    if ($gpu) {
        Ok "Знайдено відеокарту NVIDIA: $gpu"
    } else {
        Warn "Відеокарту NVIDIA не знайдено. Студія працюватиме, але повільніше (відео на годину ~30-40 хв)."
    }

    # 4. Бібліотеки
    Step 4 $total "Ставлю бібліотеки (найдовший крок)"
    if ((Run $UvExe @("pip", "install", "--python", $Py, "-r", (Join-Path $Root "requirements-base.txt"))) -ne 0) { Fail "Не вдалося поставити бібліотеки (requirements-base.txt)." }
    if ($gpu) {
        if ((Run $UvExe @("pip", "install", "--python", $Py, "-r", (Join-Path $Root "requirements-gpu.txt"))) -ne 0) { Warn "Бібліотеки для відеокарти не поставились, студія працюватиме на процесорі." }
    }
    Ok "Бібліотеки встановлено"

    # 5. ffmpeg
    Step 5 $total "Ставлю ffmpeg (робота з відео)"
    if (-not (Test-Path (Join-Path $FfBin "ffmpeg.exe"))) {
        $zip = Join-Path $Tools "ffmpeg.zip"
        $tmp = Join-Path $Tools "ffmpeg-tmp"
        Download "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" $zip
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $bin = Get-ChildItem -Path $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
        if (-not $bin) { Fail "У архіві ffmpeg немає ffmpeg.exe." }
        New-Item -ItemType Directory -Force -Path $FfBin | Out-Null
        Copy-Item (Join-Path $bin.DirectoryName "*.exe") $FfBin -Force
        Remove-Item $zip -Force
        Remove-Item $tmp -Recurse -Force
    }
    Ok "ffmpeg готовий"

    # 6. Модель розпізнавання мови, щоб перше відео не чекало завантаження
    Step 6 $total "Качаю модель розпізнавання мови"
    Push-Location $Root
    $code = Run $Py @("-c", "import yaml; from faster_whisper.utils import download_model; m = (yaml.safe_load(open('config.yaml', encoding='utf-8')) or {}).get('whisper', {}).get('model', 'small'); print('model', m); download_model(m)")
    Pop-Location
    if ($code -ne 0) { Warn "Модель докачається під час першого відео." } else { Ok "Модель готова" }

    Run $UvExe @("cache", "clean") | Out-Null

    # Ярлик на робочому столі
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shell = New-Object -ComObject WScript.Shell
    $target = Join-Path $Root "Запустити.bat"
    # Не перезаписуємо ярлик, що веде на іншу копію студії: робимо «Студія моментів 2», «3»...
    $lnkPath = Join-Path $desktop "Студія моментів.lnk"
    $n = 1
    while ((Test-Path $lnkPath) -and ($shell.CreateShortcut($lnkPath).TargetPath -ne $target)) {
        $n++
        $lnkPath = Join-Path $desktop "Студія моментів $n.lnk"
    }
    if ($n -gt 1) { Warn "На робочому столі вже є ярлик іншої копії студії. Новий названо «Студія моментів $n»." }
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $target
    $lnk.WorkingDirectory = $Root
    $lnk.WindowStyle = 7
    $icon = Join-Path $Root "assets\icon.ico"
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Description = "Студія моментів"
    $lnk.Save()

    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "   Готово! На робочому столі з'явився ярлик" -ForegroundColor Green
    Write-Host "   «Студія моментів». Запускай студію з нього." -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host ""
    $answer = if ($Quiet) { "n" } else { Read-Host "Запустити студію зараз? (Enter = так, n = ні)" }
    if ($answer -ne "n") { Start-Process -FilePath (Join-Path $Root "Запустити.bat") -WorkingDirectory $Root -WindowStyle Minimized }
}
catch {
    Fail $_.Exception.Message
}
finally {
    Stop-Transcript | Out-Null
}
