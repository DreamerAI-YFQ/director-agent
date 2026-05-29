"""
Database migration runner for the AI Director Agent.

Migrations are plain SQL files in the project-level migrations/ directory.
Each file is applied once and recorded in schema_migrations.
"""

from pathlib import Path

from agent import storage


PROJECT_ROOT = Path(__file__).parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _ensure_migration_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT NOW()
            )
        """)


def applied_versions(conn) -> set[str]:
    _ensure_migration_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def run_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending SQL migrations and return the applied version names."""
    conn = storage.get_connection()
    _ensure_migration_table(conn)

    applied = applied_versions(conn)
    pending = sorted(Path(migrations_dir).glob("*.sql"))
    ran: list[str] = []

    for path in pending:
        version = path.stem
        if version in applied:
            continue

        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (version,),
            )
        ran.append(version)

    return ran


def migration_status(migrations_dir: Path = MIGRATIONS_DIR) -> list[dict]:
    """Return all migration files with applied/pending status."""
    conn = storage.get_connection()
    applied = applied_versions(conn)
    return [
        {
            "version": path.stem,
            "file": str(path),
            "applied": path.stem in applied,
        }
        for path in sorted(Path(migrations_dir).glob("*.sql"))
    ]
