import json
import os
import socket
import uuid
from typing import Any, Dict, List, Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


OFFICIAL_RUNNER_IMAGE: str = "ghcr.io/jvrcruzgames/yt-dlp-runner:latest"


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
    # Note: RUNNER_IMAGE is strictly hardcoded to the official GHCR runner image
    DOCKER_HOST: Optional[str] = None
    RUNNER_IMAGE: str = OFFICIAL_RUNNER_IMAGE
    RUNNER_GIT_COMMIT_SHA: Optional[str] = os.getenv("RUNNER_GIT_COMMIT_SHA", None)
    RUNNER_IMAGE_DIGEST: Optional[str] = os.getenv("RUNNER_IMAGE_DIGEST", None)
    DOCKER_NETWORK: str = "mithril-network"
    RUNNER_PORT: int = 8080
    
    # FlareSolverr & HTTP Proxy configuration (embedded by default inside child runner)
    # Traffic flow: yt-dlp -> FlareSolverr -> User defined proxy (if defined)
    HTTP_PROXY: Optional[str] = os.getenv(
        "HTTP_PROXY",
        os.getenv("http_proxy", os.getenv("FLARESOLVERR_PROXY", None))
    )
    FLARESOLVERR_URL: str = os.getenv("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1")
    FLARESOLVERR_PROXY: Optional[str] = os.getenv("FLARESOLVERR_PROXY", os.getenv("HTTP_PROXY", None))
    
    # bgutil YouTube POT token provider configuration (embedded by default inside child runner)
    BGUTIL_POT_PROVIDER_URL: Optional[str] = os.getenv(
        "BGUTIL_POT_PROVIDER_URL",
        os.getenv("POT_PROVIDER_URL", "http://127.0.0.1:4416")
    )
    
    # Inactivity timeout for child containers: 20 minutes = 1200 seconds
    INACTIVITY_TIMEOUT_SECONDS: int = 1200
    INACTIVITY_CHECK_INTERVAL_SECONDS: int = 30
    
    # Worker tags and capabilities
    WORKER_TAGS: List[str] = Field(default_factory=lambda: ["yt-dlp", "media", "downloader"])
    WORKER_METADATA_JSON: str = json.dumps({
        "capabilities": ["yt-dlp", "ffmpeg", "plugins", "dynamic-runner", "cookie-auth"],
        "inactivity_timeout_minutes": 20,
    })

    @field_validator("RUNNER_IMAGE", mode="before")
    @classmethod
    def enforce_runner_image(cls, v: Any) -> str:
        return OFFICIAL_RUNNER_IMAGE

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

    @model_validator(mode="after")
    def sync_proxy_settings(self) -> "Settings":
        if self.HTTP_PROXY and not self.FLARESOLVERR_PROXY:
            self.FLARESOLVERR_PROXY = self.HTTP_PROXY
        elif self.FLARESOLVERR_PROXY and not self.HTTP_PROXY:
            self.HTTP_PROXY = self.FLARESOLVERR_PROXY
        return self

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
