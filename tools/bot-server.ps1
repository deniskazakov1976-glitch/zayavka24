# ============================================================================
#  БОТ НА СЕРВЕРЕ — гасит локальные копии и поднимает бота на VPS (24/7).
# ============================================================================
param([switch]$NoOpen)
$ErrorActionPreference = 'Continue'
$Project  = 'F:\BotAvitoTelegram'
$SshHost  = 'aeza-vps'
$Marker   = '/opt/BotAvitoTelegram/.maintenance'
$DashUrl  = 'https://77.221.155.74.sslip.io/'
$Health   = 'https://77.221.155.74.sslip.io/health'

function Line($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

Clear-Host
Line '=====================================================' 'Cyan'
Line '              БОТ НА СЕРВЕРЕ (24/7)' 'Cyan'
Line '=====================================================' 'Cyan'
Line ''

# --- 1. Гасим локальные копии ---------------------------------------------
Line '[1/3] Закрываю локальные копии бота...' 'Yellow'
$killed = 0
Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like "$Project*" -and $_.CommandLine -match 'main\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++ }
Line "      закрыто процессов: $killed"

# --- 2. Поднимаем контейнер на сервере ------------------------------------
Line '[2/3] Запускаю бота на сервере...' 'Yellow'
ssh $SshHost "rm -f $Marker; cd /opt/BotAvitoTelegram && docker compose start app && docker compose ps app" 2>&1 | ForEach-Object { "      $_" }
if ($LASTEXITCODE -ne 0) {
    Line '      НЕ УДАЛОСЬ связаться с сервером — проверь VPN/интернет' 'Red'
    Read-Host 'Enter — закрыть'
    exit 1
}

# --- 3. Ждём public health -------------------------------------------------
Line '[3/3] Жду, пока бот ответит...' 'Yellow'
$up = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 2
    $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 6 $Health 2>$null
    if ($code -eq '200') { $up = $true; break }
}
if ($up) {
    Line '      бот на сервере отвечает' 'Green'
    if (-not $NoOpen) { Start-Process $DashUrl }
} else {
    Line '      контейнер запущен, но health не 200 — смотри логи:' 'DarkYellow'
    Line '      ssh aeza-vps "cd /opt/BotAvitoTelegram && docker compose logs --tail 40 app"' 'DarkGray'
}

Line ''
Line "  ДАШБОРД:  $DashUrl" 'White'
Line '            логин admin, пароль — DASHBOARD_PASSWORD из серверного .env' 'DarkGray'
Line ''
Line '  Бот работает круглосуточно, компьютер можно выключать.' 'Green'
Line ''
Read-Host 'Enter — закрыть окно'