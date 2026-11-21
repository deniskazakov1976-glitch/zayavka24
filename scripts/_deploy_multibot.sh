#!/usr/bin/env bash
set -e
cd /opt/BotAvitoTelegram

echo "=== бэкап текущего кода и базы ==="
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p /root/backups
tar -czf "/root/backups/project-$STAMP.tgz" app main.py requirements.txt deploy 2>/dev/null || true
[ -f data/bot.db ] && cp -a data/bot.db "/root/backups/bot-$STAMP.db"
echo "  /root/backups/project-$STAMP.tgz"

echo "=== распаковка новой версии ==="
tar -xzf /root/botapp.tgz

echo "=== зависимости (python-multipart) ==="
grep -i multipart requirements.txt || true

echo "=== пересборка контейнера ==="
docker compose up -d --build app 2>&1 | tail -8

echo "=== ждём старт ==="
for i in $(seq 1 30); do
  sleep 3
  st=$(docker inspect botavitotelegram-app-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo "?")
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve 77.221.155.74.sslip.io:443:127.0.0.1 https://77.221.155.74.sslip.io/health)
  echo "  t=$((i*3))s health=$st public=$code"
  [ "$code" = "200" ] && [ "$st" = "healthy" ] && break
done

echo
echo "=== логи запуска ==="
docker logs --tail 40 botavitotelegram-app-1 2>&1 | grep -vE 'Sleep for|tryings' | tail -30