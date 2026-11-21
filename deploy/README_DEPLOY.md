# Инструкция по развертыванию на VPS (24/7, Постоянный домен, Авто-HTTPS)

Эта инструкция позволяет запустить бота «Заявка 24/7» на постоянном сервере, чтобы он работал 24/7 без необходимости держать свой компьютер включённым.

---

## 1. Покупка VPS и домена (5 минут)

1. **Сервер (VPS):**
   - Любой провайдер в РФ (Timeweb Cloud, Beget, Selectel, Aeza).
   - Характеристики: **1 vCPU, 1–2 GB RAM, 20 GB SSD, ОС Ubuntu 22.04 LTS**.
   - Стоимость: ~250–400 ₽/мес.

2. **Домен:**
   - Купите любой домен в зоне `.ru` (от 150 ₽/год, например на Beget или Reg.ru).
   - В DNS-настройках домена добавьте **A-запись**:
     - Имя: `@` (или поддомен, например `bot`)
     - Значение: **IP-адрес вашего VPS**.

---

## 2. Подготовка сервера (3 минуты)

Подключитесь к VPS по SSH (через PowerShell или PuTTY):
```bash
ssh root@<IP_ВАШЕГО_СЕРВЕРА>
```

Установите Docker и Docker Compose:
```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
apt install -y docker-compose-plugin git
```

---

## 3. Клонирование и настройка проекта

```bash
cd /opt
git clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ> BotAvitoTelegram
cd BotAvitoTelegram
cp .env.example .env
nano .env
```

Заполните ключевые переменные в `.env`:
* `DOMAIN=bot.yourdomain.ru` (ваш домен)
* `PUBLIC_BASE_URL=https://bot.yourdomain.ru`
* `DASHBOARD_PASSWORD=придумайте_надежный_пароль`
* `AVITO_WEBHOOK_SECRET=сгенерируйте_случайную_строку_32_символа`
* `LLM_API_KEY=ваш_ключ_polza_ai`
* `TELEGRAM_BOT_TOKEN=токен_бота_от_botfather`

Сохраните файл (`Ctrl+O`, `Enter`, `Ctrl+X`).

---

## 4. Запуск в 1 команду

```bash
docker compose up -d --build
```

Caddy автоматически выпустит бесплатный SSL-сертификат Let's Encrypt для вашего домена!

Проверьте статус:
```bash
curl https://bot.yourdomain.ru/health
# Ответ: {"status":"ok"}
```

---

## 5. Регистрация Webhook в Авито

1. Зайдите в кабинет разработчика Авито ([avito.ru/professionals/api](https://www.avito.ru/professionals/api)).
2. Ваш постоянный Webhook URL:
   `https://bot.yourdomain.ru/webhook/avito/<ВАШ_AVITO_WEBHOOK_SECRET>`
3. Зарегистрируйте его один раз — он больше **никогда не изменится** и будет работать 24/7.

---

## 6. Безопасность и дашборд

* **Безопасность по умолчанию:** Файл `deploy/Caddyfile` наружу проксирует **только** пути `/webhook/*` и `/health`.
* Веб-панель управления (`/` и `/api/*`) недоступна из открытого интернета.
* Чтобы открыть дашборд со своего компьютера, используйте безопасный SSH-туннель:
  ```powershell
  ssh -L 8080:localhost:8080 root@<IP_СЕРВЕРА>
  ```
  И откройте в браузере: `http://localhost:8080/`.

---

## 7. Автоматические бэкапы

Включите ежедневный бэкап базы данных и настроек в cron:
```bash
crontab -e
# Добавьте строчку (каждую ночь в 03:00):
0 3 * * * /opt/BotAvitoTelegram/scripts/backup.sh > /dev/null 2>&1
```
Архивы будут сохраняться в `/opt/BotAvitoTelegram/backups/`.
