import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class DownloadRequest(BaseModel):
    url: str = Field(..., description="Target video/audio URL to download")
    cookie_content: Optional[str] = Field(
        default=None,
        description="Raw text content of Netscape/curl cookie file to be saved in isolated cookies dir"
    )
    custom_args: Optional[List[str]] = Field(
        default_factory=list,
        description="Optional list of additional yt-dlp CLI arguments (e.g. ['-f', 'best', '--extract-audio'])"
    )
    output_template: Optional[str] = Field(
        default="%(title)s [%(id)s].%(ext)s",
        description="Filename format template inside the downloads folder"
    )
    format_selection: Optional[str] = Field(
        default=None,
        description="Optional format selector (e.g. 'bestvideo+bestaudio/best')"
    )


class DownloadTaskResponse(BaseModel):
    task_id: str
    url: str
    status: str
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bytes_per_sec: Optional[float] = None
    eta_seconds: Optional[int] = None
    filename: Optional[str] = None
    filepath: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None
    logs: List[str] = Field(default_factory=list)


class PluginInstallRequest(BaseModel):
    packages: List[str] = Field(
        ...,
        description="List of Python plugin package names or git URLs to install (e.g. ['yt-dlp-get-pot', 'git+https://...'])"
    )
    upgrade: bool = Field(default=True, description="Whether to pass --upgrade to pip")


class PluginInstallResponse(BaseModel):
    success: bool
    packages: List[str]
    stdout: str
    stderr: str
    installed_packages: List[str] = Field(default_factory=list)


class FileInfo(BaseModel):
    filename: str
    size_bytes: int
    modified_at: datetime.datetime
    download_url: str


class ActivityResponse(BaseModel):
    is_busy: bool
    active_tasks_count: int
    last_activity_timestamp: float
    idle_seconds: float
    total_downloads: int
