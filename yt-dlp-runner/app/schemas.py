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
        description="Optional list of additional yt-dlp CLI arguments (e.g. ['-f', 'best', '--extract-audio', '--write-subs', '--write-thumbnail'])"
    )
    output_template: Optional[str] = Field(
        default="%(title)s [%(id)s].%(ext)s",
        description="Filename format template inside the downloads folder"
    )
    format_selection: Optional[str] = Field(
        default=None,
        description="Optional format selector (e.g. 'bestvideo+bestaudio/best')"
    )
    plugins: Optional[List[str]] = Field(
        default_factory=list,
        description="List of Python yt-dlp plugin packages to install prior to starting this download"
    )


class FileInfo(BaseModel):
    file_id: str = Field(..., description="Hex identifier for the file")
    filename: str = Field(..., description="Original filename with extension")
    size_bytes: int = Field(..., description="Size of file in bytes")
    mime_type: str = Field(default="application/octet-stream", description="Detected MIME content type")
    download_url: str = Field(..., description="Download URL using the hex file ID")
    modified_at: datetime.datetime


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
    files: List[FileInfo] = Field(default_factory=list, description="Table of all generated files with hex download URLs")
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


class ActivityResponse(BaseModel):
    is_busy: bool
    active_tasks_count: int
    last_activity_timestamp: float
    idle_seconds: float
    total_downloads: int
