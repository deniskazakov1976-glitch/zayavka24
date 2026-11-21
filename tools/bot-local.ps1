# ============================================================================
#  БОТ ЛОКАЛЬНО — останавливает копию на сервере и поднимает бота на этом ПК.
#  Пока это окно открыто, бот работает локально, а серверный сторож его не трогает.
#  Закрой окно (Enter) — бот автоматически вернётся на сервер.
# ============================================================================
param([switch]$NoOpen)
$ErrorActionPreference = 'Continue'
$Project  = 'F:\BotAvitoTelegram'
$SshHost  = 'aeza-vps'
$Marker   = '/opt/BotAvitoTelegram/.maintenance'
$LocalUrl = 'http://127.0.0.1:8080/'
$Health   = 'http://127.0.0.1:8080/health'

function Line($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

Clear-Host
Line '=====================================================' 'Cyan'
Line '            БОТ ЛОКАЛЬНО (на этом компьютере)' 'Cyan'
Line '=====================================================' 'Cyan'
Line ''

# --- 1. Останавливаем серверную копию --------------------------------------
Line '[1/3] Останавливаю бота на сервере...' 'Yellow'
ssh $SshHost "touch $Marker; cd /opt/BotAvitoTelegram && docker compose stop app" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { Line '      серверный бот остановлен' 'Green' }
else { Line '      НЕ УДАЛОСЬ связаться с сервером — работаю без остановки серверной копии' 'Red' }

# --- 2. Гасим старые локальные копии --------------------------------------
Line '[2/3] Закрываю прежние локальные копии...' 'Yellow'
$killed = 0
Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like "$Project*" -and $_.CommandLine -match 'main\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++ }
Line "      закрыто процессов: $killed"

# --- 3. Запускаем локально -------------------------------------------------
Line '[3/3] Запускаю бота локально...' 'Yellow'
if (-not (Test-Path "$Project\start.bat")) {
    Line "      НЕТ ФАЙЛА $Project\start.bat" 'Red'
    Read-Host 'Enter — закрыть'
    exit 1
}
Start-Process -FilePath "$Project\start.bat" -WorkingDirectory $Project

$ok = $false
foreach ($i in 1..25) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri $Health -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Line '      локальный бот поднялся' 'Green'
    if (-not $NoOpen) { Start-Process $LocalUrl }
} else {
    Line '      бот ещё стартует — открой адрес вручную через полминуты' 'DarkYellow'
}

Line ''
Line "  ДАШБОРД:  $LocalUrl" 'White'
Line '            логин admin, пароль — DASHBOARD_PASSWORD из F:\BotAvitoTelegram\.env' 'DarkGray'
Line ''
Line '  Логи бота — в отдельном чёрном окне. Остановить его: Ctrl+C в том окне.' 'Gray'
Line ''
Line '  Пока это окно открыто, бот живёт на этом компьютере.' 'Yellow'
Line '  Нажми Enter здесь — бот вернётся на сервер и заработает 24/7.' 'Yellow'
Line ''

# --- Держим метку свежей, пока локальный бот жив ---------------------------
$interactive = $true
try { [void][Console]::KeyAvailable } catch { $interactive = $false }

$started = Get-Date
$lastPing = Get-Date
while ($true) {
    Start-Sleep -Seconds 2
    if (-not $interactive) { break }

    if ((Get-Date) - $lastPing -gt [TimeSpan]::FromSeconds(45)) {
        ssh $SshHost "touch $Marker" 2>&1 | Out-Null
        $lastPing = Get-Date
    }
    try {
        if ([Console]::KeyAvailable) { [void][Console]::ReadKey($true); break }
    } catch { break }

    $up = (Get-Date) - $started
    Write-Host ("`r  работаю локально: {0:hh\:mm\:ss}" -f $up) -NoNewline -ForegroundColor DarkGray
}

# --- Возвращаем бота на сервер ---------------------------------------------
Line ''
Line ''
Line '[+] Возвращаю бота на сервер...' 'Yellow'
Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like "$Project*" -and $_.CommandLine -match 'main\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

ssh $SshHost "rm -f $Marker; cd /opt/BotAvitoTelegram && docker compose start app" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    $up = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 6 'https://77.221.155.74.sslip.io/health' 2>$null
        if ($code -eq '200') { $up = $true; break }
    }
    if ($up) { Line '      бот снова на сервере, работает 24/7' 'Green' }
    else { Line '      контейнер запущен, но health пока не 200 — проверь через минуту' 'DarkYellow' }
} else {
    Line '      не удалось вернуть бота на сервер — запусти ярлык «Бот на сервер»' 'Red'
}

Line ''
Read-Host 'Enter — закрыть окно'