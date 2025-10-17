# app/models.py
from sqlmodel import SQLModel, Field, Column, Text
from typing import Optional
from datetime import datetime

class TaskRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str
    task: str
    round: int
    nonce: str
    brief: str = Field(default="", sa_column=Column(Text))  # ✅ added
    checks: str = Field(default="[]", sa_column=Column(Text))
    evaluation_url: str = Field(default="", sa_column=Column(Text))
    attachments: str = Field(default="[]", sa_column=Column(Text))
    status: str = Field(default="queued")
    repo_name: Optional[str] = None
    commit_sha: Optional[str] = None
    pages_url: Optional[str] = None
    attempts: int = Field(default=0)
    received_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

class RepoRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task: str
    email: str
    repo_name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
