import json
import os
import socket
import uuid
from typing import Any, Dict, List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    WORKER_NAME: str = "Mithril Worker"
    WORKER_VERSION: str = "0.1.0"
    DEBUG: bool = False

    WORKER_HOST: str = "0.0.0.0"
    WORKER_PORT: int = 8001
    
    # Admin key for restricted worker routes (listing jobs, container management)
    ADMIN_KEY: Optional[str] = os.getenv("ADMIN_KEY", None)

    # URL that clients or orchestrator should use to reach this worker
    WORKER_BASE_URL: str = "http://localhost:8001"
    HEALTH_ENDPOINT: str = "/health"

    # Orchestrator location and integrity verification
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    INTEGRITY_CHECK_ENABLED: bool = True
    GIT_COMMIT_SHA: Optional[str] = os.getenv("GIT_COMMIT_SHA", None)
    IMAGE_REF: Optional[str] = os.getenv("IMAGE_REF", None)
    IMAGE_DIGEST: Optional[str] = os.getenv("IMAGE_DIGEST", None)
    
    # Discovery announcement & heartbeat intervals
    AUTO_ANNOUNCE: bool = True
    ANNOUNCE_RETRY_INTERVAL_SECONDS: int = 5
    HEARTBEAT_INTERVAL_SECONDS: int = 10
    
    # yt-dlp child container & runner configuration
    DOCKER_HOST: Optional[str] = None
    RUNNER_IMAGE: str = "mithril-yt-dlp-runner:latest"
    DOCKER_NETWORK: str = "mithril-network"
    RUNNER_PORT: int = 8080
    
    # FlareSolverr configuration (required for challenge resolution)
    FLARESOLVERR_URL: str = "http://flaresolverr:8191/v1"
    FLARESOLVERR_PROXY: Optional[str] = None
    
    # Inactivity timeout for child containers: 20 minutes = 1200 seconds
    INACTIVITY_TIMEOUT_SECONDS: int = 1200
    INACTIVITY_CHECK_INTERVAL_SECONDS: int = 30
    
    # Volumes or host directories for shared downloads and isolated cookies
    SHARED_DOWNLOADS_VOLUME: str = "mithril-downloads"
    SHARED_COOKIES_VOLUME: str = "mithril-cookies"
    LOCAL_DOWNLOADS_PATH: str = os.getenv(
        "LOCAL_DOWNLOADS_PATH",
        "/app/downloads" if os.path.exists("/app/downloads") else "./downloads"
    )
    
    # Worker tags and capabilities
    WORKER_TAGS: List[str] = Field(default_factory=lambda: ["yt-dlp", "media", "downloader"])
    WORKER_METADATA_JSON: str = json.dumps({
        "capabilities": ["yt-dlp", "ffmpeg", "plugins", "dynamic-runner", "cookie-auth"],
        "inactivity_timeout_minutes": 20,
    })

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
