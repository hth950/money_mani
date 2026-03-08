"""SQLite database connection management."""
import logging
import sqlite3
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger("money_mani.web.db.connection")

DB_PATH = Path(__file__).parent.parent.parent / "data" / "money_mani.db"

def init_db():
    """Create all tables if they don't exist. Set WAL mode."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    schema_path = Path(__file__).parent / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    # Split by semicolons and execute each statement
    # (skip PRAGMA since they're already set)
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.upper().startswith("PRAGMA"):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e):
                    logger.warning(f"Schema DDL warning: {e} | stmt: {stmt[:80]}")
    conn.commit()
    conn.close()

@contextmanager
def get_db():
    """Context manager yielding a sqlite3 connection with Row factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
