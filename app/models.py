# app/models.py
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class TaskRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str
    task: str
    round: int
    nonce: str
    brief: Optional[str] = None
    checks: Optional[str] = None       # JSON string
    evaluation_url: Optional[str] = None
    attachments: Optional[str] = None  # JSON string or comma-separated
    status: str = "queued"
    repo_name: Optional[str] = None
    commit_sha: Optional[str] = None
    pages_url: Optional[str] = None
    received_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    attempts: int = 0


class RepoRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task: str
    email: str
    repo_name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
