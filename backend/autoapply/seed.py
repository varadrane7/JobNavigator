"""Startup seeding for AutoApply, called once from main.lifespan after run_seeds()."""
import logging

from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.autoapply.seed")


def run() -> None:
    from backend.autoapply import settings
    db = SessionLocal()
    try:
        settings.seed(db)
    except Exception as e:
        # AutoApply must never take the whole backend down at startup.
        logger.error(f"AutoApply seed failed: {e}")
        db.rollback()
    finally:
        db.close()
