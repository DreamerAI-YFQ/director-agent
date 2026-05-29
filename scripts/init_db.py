"""
Initialize or upgrade the AI Director Agent database.

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --status
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from agent import storage
from agent.migrations import migration_status, run_migrations


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize AI Director Agent database")
    parser.add_argument("--status", action="store_true", help="show migration status without applying")
    args = parser.parse_args()

    if args.status:
        for item in migration_status():
            status = "applied" if item["applied"] else "pending"
            print(f"{item['version']}\t{status}\t{item['file']}")
        return 0

    applied = run_migrations()
    result = storage.init_db()
    print(f"pgvector: {result['pgvector_version']}")
    if applied:
        print("applied migrations:")
        for version in applied:
            print(f"  - {version}")
    else:
        print("no pending migrations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
