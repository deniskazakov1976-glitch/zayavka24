import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def create_backup(
    db_path: Path | str = Path("data/bot.db"),
    companies_dir: Path | str = Path("companies"),
    backups_dir: Path | str = Path("backups"),
    keep_count: int = 14,
) -> Path:
    db_path = Path(db_path)
    companies_dir = Path(companies_dir)
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    temp_db_copy = backups_dir / f"temp_{timestamp}.db"
    zip_path = backups_dir / f"backup_{timestamp}.zip"

    # Step 1: Hot consistent sqlite backup
    if db_path.exists():
        src_conn = sqlite3.connect(db_path)
        dst_conn = sqlite3.connect(temp_db_copy)
        with dst_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()

    # Step 2: Archive into zip
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        if temp_db_copy.exists():
            zipf.write(temp_db_copy, arcname="bot.db")
        if companies_dir.exists():
            for yaml_file in companies_dir.glob("*.yaml"):
                zipf.write(yaml_file, arcname=f"companies/{yaml_file.name}")

    if temp_db_copy.exists():
        temp_db_copy.unlink()

    # Step 3: Purge old backups beyond keep_count
    all_backups = sorted(backups_dir.glob("backup_*.zip"), key=lambda p: p.stat().st_mtime)
    while len(all_backups) > keep_count:
        oldest = all_backups.pop(0)
        oldest.unlink()

    return zip_path


if __name__ == "__main__":
    b_path = create_backup()
    print(f"Backup created successfully: {b_path}")
