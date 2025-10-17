# app/main.py
"""
FastAPI entrypoint for the TDS LLM Deployer.

Exposes:
 - GET  /               -> landing status page
 - GET  /health         -> quick liveness check
 - POST /api/task       -> accept TDS tasks (round 1 & 2)
                           Validates secret, stores TaskRecord, schedules background worker.
 - GET  /api/tasks      -> optional dev helper to list recent tasks
"""

import json
import logging
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from .db import get_session, init_db
from .settings import settings
from . import models
from .worker import process_task

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("app.main")

# -----------------------------------------------------------
# ⚙️ STARTUP INITIALIZATION
# -----------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    logger.info("✅ App startup complete. Database initialized.")
    yield
    # Shutdown (if needed)
    logger.info("🔄 App shutdown.")

# Initialize FastAPI app
app = FastAPI(title="TDS LLM Deployer API", version="1.0", lifespan=lifespan)


# -----------------------------------------------------------
# 🌐 ROOT PAGE (for Hugging Face base URL)
# -----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    html = """
    <html>
        <head>
            <meta charset="utf-8">
            <title>TDS Project 1 Deployer</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        </head>
        <body class="bg-light text-center py-5">
            <div class="container">
                <h1 class="display-5 fw-bold text-primary">🚀 TDS Project 1 Deployer</h1>
                <p class="lead">FastAPI + Hugging Face + GitHub Integration</p>
                <hr class="my-4">
                <p>This service accepts <strong>Round 1 & Round 2</strong> JSON tasks from the TDS Server,<br>
                   creates GitHub repos, deploys pages, and returns evaluation info.</p>
                <p class="text-muted">Status: <span class="text-success fw-bold">Online</span></p>
                <a href="/health" class="btn btn-success btn-lg mt-2">Health Check</a>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=200)


# -----------------------------------------------------------
# 🩺 HEALTH ENDPOINT
# -----------------------------------------------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": "TDS LLM Deployer",
        "version": "1.0",
        "database": "connected" if settings.DB_PATH else "not configured"
    }

@app.get("/config")
def config_check():
    """Debug endpoint to check configuration (without sensitive data)"""
    return {
        "openai_base_url": settings.OPENAI_BASE_URL,
        "ai_model": settings.AIMODEL_NAME,
        "github_owner": settings.GITHUB_OWNER or "NOT_SET",
        "db_path": settings.DB_PATH,
        "has_openai_key": bool(settings.OPENAI_API_KEY),
        "has_github_token": bool(settings.GITHUB_TOKEN),
        "has_student_secret": bool(settings.STUDENT_SECRET),
    }


# -----------------------------------------------------------
# 🧾 REQUEST MODEL
# -----------------------------------------------------------
class TaskPayload(BaseModel):
    email: str
    secret: str
    task: str
    round: int = Field(..., ge=1)
    nonce: str
    brief: Optional[str] = None
    checks: Optional[List[str]] = None
    evaluation_url: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None


# -----------------------------------------------------------
# 🔐 SECRET VALIDATION
# -----------------------------------------------------------
def _validate_secret(incoming: str) -> bool:
    """
    Validate incoming secret against configured STUDENT_SECRET.
    If STUDENT_SECRET is empty or not set, treat it as insecure (reject).
    """
    if not settings.STUDENT_SECRET:
        logger.warning("STUDENT_SECRET not set in settings. Rejecting all requests for safety.")
        return False
    return incoming == settings.STUDENT_SECRET

# -----------------------------------------------------------
# 🧠 MAIN ENDPOINT: /api/task
# -----------------------------------------------------------
@app.post("/api/task")
async def receive_task(payload: TaskPayload, background_tasks: BackgroundTasks, request: Request):
    """
    Endpoint for TDS server to POST tasks.
    - Validates secret
    - Inserts TaskRecord into DB with status 'queued'
    - Schedules background worker to process it
    - Returns immediate acknowledgement with task id
    """
    if not _validate_secret(payload.secret):
        logger.warning("Invalid secret from %s for task=%s", payload.email, payload.task)
        raise HTTPException(status_code=403, detail="Invalid secret")

    task_record = models.TaskRecord(
        email=payload.email,
        task=payload.task,
        round=payload.round,
        nonce=payload.nonce,
        brief=payload.brief or "",
        checks=json.dumps(payload.checks or []),
        evaluation_url=payload.evaluation_url or "",
        attachments=json.dumps(payload.attachments or []),
        status="queued",
        attempts=0,
    )

    with next(get_session()) as session:
        session.add(task_record)
        session.commit()
        session.refresh(task_record)
        task_id = task_record.id

    logger.info("Accepted task id=%s task=%s round=%s nonce=%s from %s",
                task_id, payload.task, payload.round, payload.nonce, payload.email)

    background_tasks.add_task(process_task, task_id)

    return {"status": "accepted", "task_id": task_id}


# -----------------------------------------------------------
# 🧰 DEV ENDPOINT: /api/tasks
# -----------------------------------------------------------
@app.get("/api/tasks")
def list_tasks(limit: int = 50):
    with next(get_session()) as session:
        statement = select(models.TaskRecord).limit(limit)
        results = session.exec(statement).all()
        out = []
        for r in results:
            out.append({
                "id": r.id,
                "email": r.email,
                "task": r.task,
                "round": r.round,
                "nonce": r.nonce,
                "status": r.status,
                "repo_name": r.repo_name,
                "pages_url": r.pages_url,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            })
        return {"count": len(out), "tasks": out}
