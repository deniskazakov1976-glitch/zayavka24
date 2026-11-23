import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path
import paramiko

SERVER_IP = "77.221.155.74"
DOMAIN = f"{SERVER_IP}.sslip.io"
REMOTE_DIR = "/opt/BotAvitoTelegram"


def make_tarball(source_dir: Path) -> Path:
    """Creates a temporary tarball of the project excluding venv/cache/data/videos."""
    tar_path = Path(tempfile.gettempdir()) / "bot_deploy.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for root, dirs, files in os.walk(source_dir):
            # Exclude runtime and heavy media folders
            dirs[:] = [
                d for d in dirs
                if d not in (
                    ".venv",
                    ".pytest_cache",
                    "__pycache__",
                    ".git",
                    "backups",
                    "data",
                    "video_motion_promo",
                    "video_explainer",
                    "video_explainer_v2",
                )
            ]
            for file in files:
                if file.endswith((".pyc", ".pyo")):
                    continue
                full_path = Path(root) / file
                rel_path = full_path.relative_to(source_dir)
                tar.add(full_path, arcname=str(rel_path))
    return tar_path


def deploy(root_password: str = None, key_path: str = None) -> None:
    print(f"[*] Подключение по SSH к root@{SERVER_IP}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if key_path and Path(key_path).exists():
        print(f"[*] Использование приватного SSH-ключа: {key_path}")
        ssh.connect(SERVER_IP, username="root", key_filename=key_path, timeout=15)
    elif root_password:
        ssh.connect(SERVER_IP, username="root", password=root_password, timeout=15)
    else:
        # Check standard default key
        default_key = Path(os.environ.get("USERPROFILE", "")) / ".ssh" / "id_vps_aeza"
        if default_key.exists():
            print(f"[*] Найден ключ {default_key}, подключение...")
            ssh.connect(SERVER_IP, username="root", key_filename=str(default_key), timeout=15)
        else:
            raise ValueError("Необходимо указать пароль или путь к SSH-ключу.")

    print("[+] Успешное SSH-подключение!")

    def run_cmd(cmd: str, check: bool = True) -> str:
        print(f"[*] Выполнение: {cmd}")
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode("utf-8")
        err = stderr.read().decode("utf-8")
        code = stdout.channel.recv_exit_status()
        if code != 0 and check:
            raise RuntimeError(f"Команда '{cmd}' завершилась с ошибкой ({code}):\n{err}\n{out}")
        return out

    # 1. Добавление SSH-ключа пользователя для беспарольного доступа
    pub_key_path = Path(os.environ.get("USERPROFILE", "")) / ".ssh" / "id_ed25519.pub"
    if pub_key_path.exists():
        pub_key = pub_key_path.read_text(encoding="utf-8").strip()
        print(f"[*] Авторизация вашего локального SSH-ключа ({pub_key.split()[-1]})...")
        run_cmd(f"mkdir -p /root/.ssh && chmod 700 /root/.ssh")
        run_cmd(f'grep -qxF "{pub_key}" /root/.ssh/authorized_keys 2>/dev/null || echo "{pub_key}" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys')

    # 2. Проверка и установка Docker
    print("[*] Проверка Docker на сервере...")
    out_docker = run_cmd("which docker || true", check=False).strip()
    if not out_docker:
        print("[*] Установка Docker и Docker Compose...")
        run_cmd("apt update && apt install -y curl tar git")
        run_cmd("curl -fsSL https://get.docker.com | sh")
        run_cmd("apt install -y docker-compose-plugin")
    else:
        print("[+] Docker уже установлен на сервере.")

    # 3. Упаковка и загрузка исходного кода
    print("[*] Упаковка локального проекта...")
    project_root = Path(__file__).resolve().parent.parent
    tar_path = make_tarball(project_root)
    print(f"[*] Размер архива: {tar_path.stat().st_size / 1024:.1f} KB. Загрузка через SFTP...")

    sftp = ssh.open_sftp()
    remote_tar = "/tmp/bot_deploy.tar.gz"
    sftp.put(str(tar_path), remote_tar)
    sftp.close()
    tar_path.unlink()

    # 4. Распаковка проекта на сервере
    print(f"[*] Распаковка в {REMOTE_DIR}...")
    run_cmd(f"mkdir -p {REMOTE_DIR} && tar -xzf {remote_tar} -C {REMOTE_DIR} && rm -f {remote_tar}")

    # 5. Настройка .env на сервере
    print(f"[*] Настройка переменных окружения для домена {DOMAIN}...")
    run_cmd(f"sed -i 's/^DOMAIN=.*/DOMAIN={DOMAIN}/g' {REMOTE_DIR}/.env || echo 'DOMAIN={DOMAIN}' >> {REMOTE_DIR}/.env", check=False)
    run_cmd(f"sed -i 's/^PUBLIC_BASE_URL=.*/PUBLIC_BASE_URL=https:\\/\\/{DOMAIN}/g' {REMOTE_DIR}/.env || echo 'PUBLIC_BASE_URL=https://{DOMAIN}' >> {REMOTE_DIR}/.env", check=False)

    # 6. Запуск через Docker Compose
    print("[*] Сборка и запуск контейнеров (docker compose up -d --build)...")
    build_out = run_cmd(f"cd {REMOTE_DIR} && docker compose up -d --build")
    print(build_out)

    # 7. Проверка статуса контейнеров и HTTPS Caddy
    print("[*] Ожидание запуска сервисов и получения SSL-сертификата Let's Encrypt (15 сек)...")
    time.sleep(15)
    ps_out = run_cmd(f"cd {REMOTE_DIR} && docker compose ps")
    print(ps_out)

    # Проверка healthcheck
    health_out = run_cmd(f"curl -sSL --max-time 10 https://{DOMAIN}/health || curl -s http://127.0.0.1:8080/health", check=False)
    print(f"[+] Ответ healthcheck: {health_out.strip()}")

    # 8. Настройка ежедневного бэкапа
    cron_cmd = f"0 3 * * * {REMOTE_DIR}/scripts/backup.sh > /dev/null 2>&1"
    run_cmd(f'(crontab -l 2>/dev/null | grep -F "{REMOTE_DIR}/scripts/backup.sh") || (crontab -l 2>/dev/null; echo "{cron_cmd}") | crontab -', check=False)
    print("[+] Автоматический бэкап настроен в cron (каждую ночь в 03:00).")

    ssh.close()
    print("\n" + "=" * 60)
    print("🚀 ДЕПЛОЙ УСПЕШНО ЗАВЕРШЁН!")
    print(f"1. Домен сервера:       https://{DOMAIN}")
    print(f"2. Healthcheck:          https://{DOMAIN}/health")
    print(f"3. Клиентский лендинг:   https://{DOMAIN}/landing")
    print("4. Webhook для Авито:    см. AVITO_WEBHOOK_SECRET в .env на сервере")
    print("5. Кабинет:              доступен по адресу домена, вход по логину/паролю")
    print("   Логин и пароль — в переменных DASHBOARD_USER / DASHBOARD_PASSWORD (.env на сервере).")
    print("   В целях безопасности значения здесь не печатаются.")
    print("=" * 60)


if __name__ == "__main__":
    pwd = sys.argv[1] if len(sys.argv) > 1 else None
    deploy(root_password=pwd)
