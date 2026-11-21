"""Migration Utility: SQLite -> PostgreSQL for 'Заявка 24/7 — ИИ-Менеджер'

WT Hardening / P1 Initiative.
Usage:
    python scripts/migrate_sqlite_to_postgres.py --sqlite data/bot.db --pg "postgresql://user:password@localhost:5432/botavitodb"
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sqlite_to_pg")

# Tables to migrate in dependency order
TABLES_TO_MIGRATE = [
    "schema_migrations",
    "tenants",
    "users",
    "sessions",
    "messages",
    "leads",
    "knowledge_gaps",
    "processed_events",
    "bot_sent_messages",
    "rate_limits",
    "usage_counters",
    "backup_history",
    "onboarding_state",
]


def extract_sqlite_table_data(conn: sqlite3.Connection, table_name: str) -> Tuple[List[str], List[tuple]]:
    cursor = conn.cursor()
    try:
        cursor.execute(f"PRAGMA table_info({table_name});")
        columns = [row[1] for row in cursor.fetchall()]
        if not columns:
            return [], []
        cursor.execute(f"SELECT {', '.join(columns)} FROM {table_name};")
        rows = cursor.fetchall()
        return columns, rows
    except Exception as exc:
        logger.warning("Could not read table %s from SQLite: %s", table_name, exc)
        return [], []


def verify_tables_in_sqlite(sqlite_path: Path) -> List[str]:
    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tables


def run_migration(sqlite_path: Path, pg_url: str, dry_run: bool = False) -> bool:
    if not sqlite_path.exists():
        logger.error("SQLite database file not found: %s", sqlite_path)
        return False

    existing_tables = verify_tables_in_sqlite(sqlite_path)
    logger.info("Found %d tables in SQLite: %s", len(existing_tables), ", ".join(existing_tables))

    if dry_run:
        logger.info("DRY-RUN mode enabled: validating record counts only...")
        conn = sqlite3.connect(sqlite_path)
        total_records = 0
        for tbl in existing_tables:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {tbl};")
            cnt = cursor.fetchone()[0]
            total_records += cnt
            logger.info("  Table '%s': %d rows", tbl, cnt)
        conn.close()
        logger.info("Total SQLite rows to migrate: %d. Dry-run PASS.", total_records)
        return True

    try:
        import psycopg2
        from psycopg2.extras import execute_batch
    except ImportError:
        logger.error(
            "psycopg2 is not installed. To run actual PostgreSQL migration, install: pip install psycopg2-binary"
        )
        return False

    sqlite_conn = sqlite3.connect(sqlite_path)
    try:
        pg_conn = psycopg2.connect(pg_url)
        pg_conn.autocommit = False
    except Exception as exc:
        logger.error("Failed to connect to PostgreSQL at %s: %s", pg_url, exc)
        sqlite_conn.close()
        return False

    logger.info("Connected to PostgreSQL successfully. Beginning transaction...")
    total_migrated = 0

    try:
        with pg_conn.cursor() as pg_cur:
            for tbl in TABLES_TO_MIGRATE:
                if tbl not in existing_tables:
                    continue

                cols, rows = extract_sqlite_table_data(sqlite_conn, tbl)
                if not rows:
                    logger.info("Table '%s' is empty, skipping data copy.", tbl)
                    continue

                col_names = ", ".join(cols)
                val_placeholders = ", ".join(["%s"] * len(cols))
                insert_query = f"INSERT INTO {tbl} ({col_names}) VALUES ({val_placeholders}) ON CONFLICT DO NOTHING;"

                execute_batch(pg_cur, insert_query, rows, page_size=500)
                logger.info("Successfully copied %d rows into PostgreSQL table '%s'.", len(rows), tbl)
                total_migrated += len(rows)

        pg_conn.commit()
        logger.info("Migration transaction COMMITTED. Total migrated rows: %d.", total_migrated)
        return True
    except Exception as exc:
        pg_conn.rollback()
        logger.error("Error during migration, transaction ROLLED BACK: %s", exc)
        return False
    finally:
        sqlite_conn.close()
        pg_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite data to PostgreSQL for Zayavka 24/7.")
    parser.add_argument("--sqlite", type=Path, default=Path("data/bot.db"), help="Path to SQLite database")
    parser.add_argument("--pg", type=str, required=False, default="", help="PostgreSQL connection string")
    parser.add_argument("--dry-run", action="store_true", help="Perform count validation without connecting to PG")
    args = parser.parse_args()

    if not args.dry_run and not args.pg:
        logger.info("No --pg URL provided, switching to --dry-run validation mode.")
        args.dry_run = True

    success = run_migration(args.sqlite, args.pg, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
