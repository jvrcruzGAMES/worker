import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class EncryptedPayload(BaseModel):
    client_public_key: str = Field(..., description="Base64-encoded client ephemeral X25519 public key")
    nonce: str = Field(..., description="Base64-encoded 12-byte ChaCha20-Poly1305 nonce")
    ciphertext: str = Field(..., description="Base64-encoded ciphertext")


class HealthStatusResponse(BaseModel):
    status: str
    worker_id: str
    worker_name: str
    uptime_seconds: float
    version: str
    public_key: Optional[str] = None
    active_runner_containers: int
    active_jobs: int


class WorkerInfoResponse(BaseModel):
    worker_id: str
    worker_name: str
    base_url: str
    health_endpoint: str
    orchestrator_url: str
    tags: List[str]
    metadata: Dict[str, Any]
    version: str
    public_key: Optional[str] = None
    uptime_seconds: float


class JobCreateRequest(BaseModel):
    url: str = Field(..., description="Media URL to download via yt-dlp")
    cookie_content: Optional[str] = Field(
        default=None,
        description="Raw Netscape cookie file content (stored isolated in runner container)"
    )
    encrypted_credentials: Optional[EncryptedPayload] = Field(
        default=None,
        description="Optional envelope-encrypted credentials (cookies, proxy, options) protected against reverse proxies"
    )
    custom_args: Optional[List[str]] = Field(
        default_factory=list,
        description="Optional additional yt-dlp CLI arguments (e.g. ['--write-subs', '--write-thumbnail'])"
    )
    output_template: Optional[str] = Field(
        default="%(title)s [%(id)s].%(ext)s",
        description="Filename format template"
    )
    format_selection: Optional[str] = Field(
        default=None,
        description="Optional format selector (e.g. 'bestvideo+bestaudio/best')"
    )
    plugins: Optional[List[str]] = Field(
        default_factory=list,
        description="Optional list of Python yt-dlp plugin packages or git repos to install prior to download"
    )


class SingleUseTokenResponse(BaseModel):
    token: str = Field(..., description="Single-use token for job creation")
    expires_in: int = Field(default=300, description="Token validity in seconds")


class FileItemResponse(BaseModel):
    file_id: str = Field(..., description="16-character hex identifier for the file")
    filename: str = Field(..., description="Original filename with extension")
    size_bytes: int = Field(..., description="Size of file in bytes")
    mime_type: str = Field(default="application/octet-stream", description="Detected MIME type")
    download_url: str = Field(..., description="Direct download URL using the hex ID")
    modified_at: datetime.datetime


class JobResponse(BaseModel):
    job_id: str
    tracking_token: Optional[str] = Field(
        default=None,
        description="Auth token used to track download progress and stream the finished file"
    )
    url: str
    status: str  # pending, starting_container, installing_plugins, downloading, completed, failed, cancelled
    plugins: List[str] = Field(default_factory=list)
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bytes_per_sec: Optional[float] = None
    eta_seconds: Optional[int] = None
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    task_id: Optional[str] = None
    filename: Optional[str] = None
    download_url: Optional[str] = None
    files: List[FileItemResponse] = Field(
        default_factory=list,
        description="Table of all generated files with their hex file IDs and download URLs"
    )
    error: Optional[str] = None
    created_at: datetime.datetime
    completed_at: Optional[datetime.datetime] = None
    logs: List[str] = Field(default_factory=list)


class ContainerInfoResponse(BaseModel):
    container_id: str
    name: str
    status: str
    ip_address: Optional[str] = None
    endpoint_url: str
    idle_seconds: float
    remaining_idle_seconds: float
    lifetime_seconds: float = 0.0
    remaining_lifetime_seconds: float = 0.0
    file_retention_remaining_seconds: Optional[float] = None
    is_draining: bool = False
    can_accept_jobs: bool = True
    active_jobs: int
    last_activity: datetime.datetime

