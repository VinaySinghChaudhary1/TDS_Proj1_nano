# app/db_migrate.py
"""
Simple migration script to create DB tables (call once at setup).
Usage:
  python -m app.db_migrate
"""

import logging
from app.db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Running DB migration / init")
    init_db()
    logger.info("DB initialization complete.")
