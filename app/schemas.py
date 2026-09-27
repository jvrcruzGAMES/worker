import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HealthStatusResponse(BaseModel):
    status: str
    worker_id: str
    worker_name: str
    uptime_seconds: float
    version: str
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
    uptime_seconds: float


class JobCreateRequest(BaseModel):
    url: str = Field(..., description="Media URL to download via yt-dlp")
    cookie_content: Optional[str] = Field(
        default=None,
        description="Raw Netscape cookie file content (stored isolated in runner container)"
    )
    custom_args: Optional[List[str]] = Field(
        default_factory=list,
        description="Optional additional yt-dlp CLI arguments"
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
        description="Optional list of Python yt-dlp plugin packages to install in runner prior to download"
    )


class JobResponse(BaseModel):
    job_id: str
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
    error: Optional[str] = None
    created_at: datetime.datetime
    completed_at: Optional[datetime.datetime] = None
    logs: List[str] = Field(default_factory=list)


class PluginInstallRequest(BaseModel):
    packages: List[str] = Field(
        ...,
        description="Python packages/plugins to install inside runner container"
    )
    container_id: Optional[str] = Field(
        default=None,
        description="Optional specific container ID (if omitted, installs into active/primary runner)"
    )


class PluginInstallResponse(BaseModel):
    success: bool
    container_id: str
    packages: List[str]
    stdout: str
    stderr: str


class ContainerInfoResponse(BaseModel):
    container_id: str
    name: str
    status: str
    ip_address: Optional[str] = None
    endpoint_url: str
    idle_seconds: float
    remaining_idle_seconds: float
    active_jobs: int
    last_activity: datetime.datetime


class FileItemResponse(BaseModel):
    filename: str
    size_bytes: int
    modified_at: datetime.datetime
    download_url: str
