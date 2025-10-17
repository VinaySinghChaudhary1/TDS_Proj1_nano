# app/main.py
"""
FastAPI entrypoint for the TDS LLM Deployer (Final – GPT-4o Nano Build)
"""

import json
import logging
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlmodel import select, Session

from .db import get_session, init_db
from .settings import settings
from . import models
from .worker import process_task

# -----------------------------------------------------------
# 🧩 Logging configuration
# -----------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("app.main")


# -----------------------------------------------------------
# ⚙️ Startup / Shutdown lifecycle
# -----------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB and environment on startup."""
    try:
        init_db()
        logger.info("✅ App startup complete. Database initialized.")
    except Exception as e:
        logger.exception("Database initialization failed: %s", e)
    yield
    logger.info("🔄 App shutdown.")


# Initialize FastAPI
app = FastAPI(title="TDS LLM Deployer API", version="3.0", lifespan=lifespan)


# -----------------------------------------------------------
# 🌐 Landing page
# -----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    """Landing page for Hugging Face deployment."""
    return HTMLResponse(
        """
        <html><head><title>TDS Project 1 Deployer</title></head>
        <body style='text-align:center; font-family:sans-serif;'>
            <h1>🚀 TDS Project 1 Deployer</h1>
            <p>FastAPI + GPT-4o + GitHub Integration</p>
            <hr>
            <a href='/docs'>View API Docs</a>
        </body></html>
        """,
        200,
    )


# -----------------------------------------------------------
# 🩺 Health check
# -----------------------------------------------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "3.0",
        "database": "connected" if settings.DB_PATH else "not configured",
    }


# -----------------------------------------------------------
# 📥 Request schema
# -----------------------------------------------------------
class TaskPayload(BaseModel):
    email: str
    secret: str
    task: str
    round: int = Field(..., ge=1)
    nonce: str
    brief: str = Field(..., description="Task brief / LLM generation instruction")
    checks: Optional[List[str]] = None
    evaluation_url: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None


# -----------------------------------------------------------
# 🔐 Secret validation
# -----------------------------------------------------------
def _validate_secret(incoming: str) -> bool:
    if not settings.STUDENT_SECRET:
        logger.warning("STUDENT_SECRET not set. Rejecting all requests.")
        return False
    return incoming == settings.STUDENT_SECRET


# -----------------------------------------------------------
# 🧠 Main endpoint: /api/task
# -----------------------------------------------------------
@app.post("/api/task")
async def receive_task(payload: TaskPayload, background_tasks: BackgroundTasks, request: Request):
    if not _validate_secret(payload.secret):
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        with next(get_session()) as session:  # ✅ unified DB model usage
            task_record = models.TaskRecord(
                email=payload.email,
                task=payload.task,
                round=payload.round,
                nonce=payload.nonce,
                brief=payload.brief.strip(),
                checks=json.dumps(payload.checks or []),
                evaluation_url=payload.evaluation_url or "",
                attachments=json.dumps(payload.attachments or []),
                status="queued",
                attempts=0,
            )
            session.add(task_record)
            session.commit()
            session.refresh(task_record)
            task_id = task_record.id
            logger.info(f"Accepted task {task_id}: {payload.task}")

        background_tasks.add_task(process_task, task_id)  # ✅ safe
        logger.info(f"Background worker scheduled for {task_id}")

    except Exception as e:
        logger.exception("Failed to save or queue task: %s", e)
        raise HTTPException(status_code=500, detail="Database or background error")

    return {"status": "accepted", "task_id": task_id, "round": payload.round}


# -----------------------------------------------------------
# 🧰 Dev endpoint: /api/tasks
# -----------------------------------------------------------
@app.get("/api/tasks")
def list_tasks(limit: int = 50):
    """List recent tasks."""
    with next(get_session()) as session:
        statement = select(models.TaskRecord).limit(limit)
        records = session.exec(statement).all()
        return {"tasks": [r.dict() for r in records]}
