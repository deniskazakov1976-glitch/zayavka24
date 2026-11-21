#!/usr/bin/env bash
set -e

echo "=== 1. Создание бэкапа внутри контейнера ==="
BACKUP_PATH=$(docker exec botavitotelegram-app-1 python -c "from scripts.backup import create_backup; print(create_backup(backups_dir='data/backups'))")
echo "Создан бэкап: $BACKUP_PATH"

echo "=== 2. Проверка файла на хосте в /opt/BotAvitoTelegram/data/backups ==="
ls -lh /opt/BotAvitoTelegram/data/backups/

echo "=== 3. Проверка восстановления и целостности SQLite ==="
rm -rf /tmp/backup_test_restore
mkdir -p /tmp/backup_test_restore
NEWEST_ZIP=$(ls -t /opt/BotAvitoTelegram/data/backups/backup_*.zip | head -1)
echo "Тестируем архив: $NEWEST_ZIP"
unzip -q "$NEWEST_ZIP" -d /tmp/backup_test_restore

python3 - << 'EOF'
import sqlite3
conn = sqlite3.connect('/tmp/backup_test_restore/bot.db')
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(f"Успешно открыт bot.db. Таблицы ({len(tables)}): {', '.join(sorted(tables))}")
assert "leads" in tables, "Таблица leads отсутствует!"
assert "tenants" in tables, "Таблица tenants отсутствует!"
assert "backup_runs" in tables, "Таблица backup_runs отсутствует!"
assert "llm_usage" in tables, "Таблица llm_usage отсутствует!"
leads_count = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
tenants_count = conn.execute("SELECT count(*) FROM tenants").fetchone()[0]
print(f"Данные на месте: leads={leads_count}, tenants={tenants_count}")
conn.close()
EOF

rm -rf /tmp/backup_test_restore
echo "=== 4. Тест восстановления успешно пройден! ==="
