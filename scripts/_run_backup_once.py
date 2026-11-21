import asyncio
from app.config import get_settings
from app.storage import Storage
from app.maintenance import BackupScheduler

async def main():
    s = get_settings()
    st = Storage(s.db_path)
    sc = BackupScheduler(st, s)
    res = await sc.run_once()
    print("BACKUP_SCHEDULER_RESULT:", res)

if __name__ == "__main__":
    asyncio.run(main())
