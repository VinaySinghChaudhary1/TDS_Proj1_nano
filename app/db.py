# app/db.py
"""
Database helpers using SQLModel (SQLite).
Provides: engine, get_session() generator, init_db()

This module normalizes the DB_PATH from settings so both plain filepaths like
'./data/tds_deployer.sqlite' and full SQLAlchemy URLs like
'sqlite:///./data/tds_deployer.sqlite' are supported.
"""

import logging
import os
from sqlmodel import SQLModel, create_engine, Session

from .settings import settings
from . import models  # ensure models are imported so SQLModel metadata includes them

from typing import Optional
from . import models

logger = logging.getLogger(__name__)


def _make_db_url(db_path: str) -> str:
    """
    Normalize db_path to an SQLAlchemy URL.

    Accepts:
      - full SQLAlchemy URL (starts with 'sqlite://' or other driver)
      - relative or absolute file path (e.g. './data/tds_deployer.sqlite' or '/home/user/...')

    Returns:
      - normalized URL e.g. 'sqlite:////absolute/path/to/data/tds_deployer.sqlite'
        (note: windows paths may differ; this is targeted for Unix-like systems)
    """
    if not db_path:
        raise ValueError("DB_PATH is empty in settings")

    db_path = str(db_path).strip()

    # If it already looks like a SQLAlchemy URL, return as-is
    if db_path.startswith("sqlite://") or "://" in db_path:
        return db_path

    # Otherwise treat as a file path. Make absolute path.
    abs_path = db_path
    if not os.path.isabs(abs_path):
        abs_path = os.path.abspath(abs_path)

    # For sqlite the URL is sqlite:///absolute/path (three slashes + absolute path)
    # SQLAlchemy accepts sqlite:////full/path on some systems; sqlite:/// + absolute path is typical.
    return f"sqlite:///{abs_path}"


# Normalize DB_PATH into a SQLAlchemy URL
try:
    DB_URL = _make_db_url(settings.DB_PATH)
except Exception as exc:
    logger.error("Invalid DB_PATH in settings: %s", settings.DB_PATH)
    raise

logger.info("Using database URL: %s", DB_URL)

# create_engine; allow check_same_thread for SQLite threaded use
engine = create_engine(DB_URL, echo=False, connect_args={"check_same_thread": False})


def get_session():
    """
    Context manager / generator for DB sessions.
    Usage:
      with next(get_session()) as session: ...
    Or in FastAPI dependency injection style return a generator
    """
    with Session(engine) as session:
        yield session

# ---------- convenience helpers for worker.py ----------
def get_task_by_id(task_id: int) -> Optional[models.TaskRecord]:
    """Return the TaskRecord instance for given id or None."""
    with Session(engine) as session:
        return session.get(models.TaskRecord, task_id)


def update_task_status(task_id: int, status: str, extra: dict = None) -> Optional[models.TaskRecord]:
    """
    Update status (and optional extra fields) on TaskRecord.
    Returns the updated TaskRecord.
    """
    extra = extra or {}
    with Session(engine) as session:
        task = session.get(models.TaskRecord, task_id)
        if not task:
            return None
        task.status = status
        # store extra fields if provided: repo_url/pages_url/error etc.
        if 'repo_url' in extra:
            task.repo_name = extra.get('repo_url')  # or parse repo name if needed
        if 'pages_url' in extra:
            task.pages_url = extra.get('pages_url')
        if 'commit_sha' in extra:
            task.commit_sha = extra.get('commit_sha')
        if 'error' in extra:
            # keep a simple last-error text field if you want
            # optional: add an 'error' column to models if desired
            pass
        # increment attempts optionally
        try:
            task.attempts = (task.attempts or 0) + 1
        except Exception:
            task.attempts = 1
        session.add(task)
        session.commit()
        session.refresh(task)
        return task



def init_db():
    """
    Create tables (idempotent).
    """
    logger.info("Initializing DB at %s", DB_URL)
    SQLModel.metadata.create_all(engine)
