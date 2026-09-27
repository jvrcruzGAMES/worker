import json
import os
import socket
import uuid
from typing import Any, Dict, List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_worker_id() -> str:
    host_suffix = socket.gethostname().lower()[:8]
    random_suffix = uuid.uuid4().hex[:6]
    return f"worker-{host_suffix}-{random_suffix}"


class Settings(BaseSettings):
    WORKER_ID: str = Field(default_factory=default_worker_id)
    WORKER_NAME: str = "Mithril Worker"
    WORKER_VERSION: str = "0.1.0"
    DEBUG: bool = False

    WORKER_HOST: str = "0.0.0.0"
    WORKER_PORT: int = 8001
    
    # URL that clients or orchestrator should use to reach this worker
    WORKER_BASE_URL: str = "http://localhost:8001"
    HEALTH_ENDPOINT: str = "/health"

    # Orchestrator location
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    
    # Discovery announcement & heartbeat intervals
    AUTO_ANNOUNCE: bool = True
    ANNOUNCE_RETRY_INTERVAL_SECONDS: int = 5
    HEARTBEAT_INTERVAL_SECONDS: int = 10
    
    # Optional tags and capabilities
    WORKER_TAGS: List[str] = Field(default_factory=list)
    WORKER_METADATA_JSON: str = "{}"

    @field_validator("WORKER_BASE_URL", "ORCHESTRATOR_URL")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("HEALTH_ENDPOINT")
    @classmethod
    def ensure_leading_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            return f"/{v}"
        return v

    @property
    def metadata_dict(self) -> Dict[str, Any]:
        try:
            return json.loads(self.WORKER_METADATA_JSON)
        except Exception:
            return {}

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
