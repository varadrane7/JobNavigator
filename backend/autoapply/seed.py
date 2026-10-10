"""Startup seeding for AutoApply, called once from main.lifespan after run_seeds()."""
import logging

from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.autoapply.seed")


# Columns added after a table first shipped; create_all() never alters an existing table.
COLUMN_ADDS = [
    ("autoapply_items", "apply_url", "VARCHAR"),
]


def migrate(db) -> None:
    from sqlalchemy import text
    if db.bind.dialect.name != "postgresql":
        return
    for table, col, ddl in COLUMN_ADDS:
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}"))
    db.commit()


def run() -> None:
    from backend.autoapply import settings
    db = SessionLocal()
    try:
        migrate(db)
        settings.seed(db)
    except Exception as e:
        # AutoApply must never take the whole backend down at startup.
        logger.error(f"AutoApply seed failed: {e}")
        db.rollback()
    finally:
        db.close()
