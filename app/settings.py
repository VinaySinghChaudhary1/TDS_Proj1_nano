# app/settings.py
from pydantic_settings import BaseSettings
from typing import Optional
import os

class Settings(BaseSettings):
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    AIMODEL_NAME: str = "gpt-4o"
    GITHUB_TOKEN: str = ""
    GITHUB_OWNER: str = ""
    STUDENT_SECRET: str = ""
    # SQLAlchemy/SQLModel compatible URL for SQLite:
    DB_PATH: str = "sqlite:///./data/tds_deployer.sqlite"

    # optional extras (present in some environments)
    LOG_LEVEL: Optional[str] = "INFO"

    # pydantic-settings uses 'model_config' for BaseSettings configuration
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Validate critical settings
        missing = []
        if not self.OPENAI_API_KEY:
            missing.append("OPENAI_API_KEY")
        if not self.GITHUB_TOKEN:
            missing.append("GITHUB_TOKEN")
        if not self.GITHUB_OWNER:
            missing.append("GITHUB_OWNER")
        if not self.STUDENT_SECRET:
            missing.append("STUDENT_SECRET")
            
        if missing:
            print(f"WARNING: Missing environment variables: {', '.join(missing)}")
            print("The app may not function correctly without these values.")

settings = Settings()
