import asyncio
import datetime
import hashlib
import logging
import mimetypes
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
import yt_dlp

from app.args_sanitizer import sanitize_yt_dlp_args
from app.config import settings
from app.flaresolverr_proxy import flaresolverr_proxy
from app.plugins import plugin_manager
from app.schemas import (
    DownloadRequest,
    DownloadTaskResponse,
    FileInfo,
    PluginInstallRequest,
)

logger = logging.getLogger("yt_dlp_runner.downloader")


def generate_file_hex_id(filename: str, file_path: Optional[str] = None) -> str:
    """Generates a unique, deterministic 16-character hex identifier for a file."""
    salt = ""
    if file_path and os.path.exists(file_path):
        salt = f":{os.path.getmtime(file_path)}:{os.path.getsize(file_path)}"
    raw = f"{filename}{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def create_file_info(filepath: Path, base_url_prefix: str = "/files") -> FileInfo:
    stat = filepath.stat()
    file_id = generate_file_hex_id(filepath.name, str(filepath))
    mime_type, _ = mimetypes.guess_type(filepath.name)
    return FileInfo(
        file_id=file_id,
        filename=filepath.name,
        size_bytes=stat.st_size,
        mime_type=mime_type or "application/octet-stream",
        download_url=f"{base_url_prefix}/{file_id}/download",
        modified_at=datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc),
    )


class DownloadTaskManager:
    def __init__(self):
        self.tasks: Dict[str, DownloadTaskResponse] = {}
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self.file_registry: Dict[str, Path] = {}  # file_id -> Path
        self.last_activity_time: float = time.time()
        self.total_completed_downloads: int = 0
        self._refresh_file_registry()

    def touch_activity(self):
        self.last_activity_time = time.time()

    def get_active_tasks_count(self) -> int:
        return sum(
            1 for t in self.tasks.values()
            if t.status in ["pending", "installing_plugins", "downloading", "processing"]
        )

    def _refresh_file_registry(self) -> List[FileInfo]:
        downloads_path = Path(settings.DOWNLOADS_DIR)
        file_infos: List[FileInfo] = []
        if downloads_path.exists():
            for f in downloads_path.iterdir():
                if f.is_file():
                    info = create_file_info(f)
                    self.file_registry[info.file_id] = f
                    # Also map filename for convenience
                    self.file_registry[f.name] = f
                    file_infos.append(info)
        return file_infos

    def get_file_by_id_or_name(self, identifier: str) -> Optional[Path]:
        self.touch_activity()
        # Check cache
        if identifier in self.file_registry:
            path = self.file_registry[identifier]
            if path.is_file() and path.exists():
                return path

        # Rescan downloads dir
        self._refresh_file_registry()
        return self.file_registry.get(identifier)

    def list_all_files(self) -> List[FileInfo]:
        self.touch_activity()
        return self._refresh_file_registry()

    async def start_download(self, request: DownloadRequest) -> DownloadTaskResponse:
        self.touch_activity()
        task_id = str(uuid.uuid4())
        
        task_record = DownloadTaskResponse(
            task_id=task_id,
            url=request.url,
            status="pending",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            logs=[f"Task queued at {datetime.datetime.now(datetime.timezone.utc).isoformat()}"],
        )
        self.tasks[task_id] = task_record

        # Launch background task
        async_task = asyncio.create_task(self._execute_download(task_id, request))
        self._async_tasks[task_id] = async_task
        return task_record

    async def _execute_download(self, task_id: str, request: DownloadRequest):
        task = self.tasks[task_id]
        cookie_path: Optional[str] = None
        start_timestamp = time.time()

        # Snapshot files before download to detect newly created / modified ones
        downloads_dir = Path(settings.DOWNLOADS_DIR)
        before_files = {
            f.name: f.stat().st_mtime for f in downloads_dir.iterdir() if f.is_file()
        } if downloads_dir.exists() else {}

        try:
            self.touch_activity()

            # 0. Install plugins dynamically if specified in the job request
            if request.plugins:
                task.status = "installing_plugins"
                task.logs.append(f"Installing {len(request.plugins)} plugin(s) for job: {request.plugins}")
                plugin_resp = await plugin_manager.install_plugins(
                    PluginInstallRequest(packages=request.plugins)
                )
                if plugin_resp.success:
                    task.logs.append(f"Successfully installed plugins: {request.plugins}")
                else:
                    task.logs.append(f"Plugin install output/warning: {plugin_resp.stderr or plugin_resp.stdout}")

            task.status = "downloading"
            task.logs.append(f"Starting download for URL: {request.url}")

            # 1. Handle cookie file if provided (saved in isolated cookies dir)
            if request.cookie_content:
                cookie_filename = f"cookie_{task_id}.txt"
                cookie_path = os.path.join(settings.COOKIES_DIR, cookie_filename)
                with open(cookie_path, "w", encoding="utf-8") as f:
                    f.write(request.cookie_content)
                task.logs.append(f"Wrote cookie file to isolated storage at: {cookie_path}")

            # 2. Setup output path template in downloads directory
            output_template = request.output_template or "%(title)s [%(id)s].%(ext)s"
            outtmpl = os.path.join(settings.DOWNLOADS_DIR, output_template)

            # 3. Setup yt-dlp logger and progress hook
            def ydl_progress_hook(d: dict):
                self.touch_activity()
                status = d.get("status")
                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    task.downloaded_bytes = downloaded
                    task.total_bytes = total if total > 0 else None
                    if total > 0:
                        task.progress_percent = round((downloaded / total) * 100, 2)
                    task.speed_bytes_per_sec = d.get("speed")
                    task.eta_seconds = d.get("eta")
                elif status == "finished":
                    task.status = "processing"
                    task.progress_percent = 100.0
                    filename = d.get("filename")
                    if filename:
                        task.filepath = filename
                        task.filename = os.path.basename(filename)
                    task.logs.append("Download finished, processing/merging formats...")

            class CustomYDLLogger:
                def debug(self, msg):
                    if not msg.startswith("[debug]"):
                        task.logs.append(msg)

                def warning(self, msg):
                    task.logs.append(f"[WARNING] {msg}")

                def error(self, msg):
                    task.logs.append(f"[ERROR] {msg}")

            # 4. Prepare yt-dlp options (nocheckcertificate=True is required for mitmproxy)
            ydl_opts: Dict[str, Any] = {
                "outtmpl": outtmpl,
                "progress_hooks": [ydl_progress_hook],
                "logger": CustomYDLLogger(),
                "noplaylist": True,
                "nocheckcertificate": True,
            }

            # Route through FlareSolverr mitmproxy if active
            if flaresolverr_proxy.is_active or settings.USE_FLARESOLVERR_PROXY:
                ydl_opts["proxy"] = flaresolverr_proxy.proxy_url
                task.logs.append(f"Routing through FlareSolverr mitmproxy at: {flaresolverr_proxy.proxy_url}")

            if cookie_path and os.path.exists(cookie_path):
                ydl_opts["cookiefile"] = cookie_path

            if request.format_selection:
                ydl_opts["format"] = request.format_selection

            # Sanitize and strip any disallowed / worker-reserved CLI arguments (including client-defined --proxy)
            if request.custom_args:
                sanitized_args, stripped_args = sanitize_yt_dlp_args(request.custom_args)
                if stripped_args:
                    task.logs.append(f"Stripped worker-reserved arguments: {stripped_args}")
                if sanitized_args:
                    try:
                        _, cli_opts, _ = yt_dlp.parse_options(sanitized_args)
                        cli_dict = vars(cli_opts)
                        filtered_cli_opts = {
                            k: v for k, v in cli_dict.items()
                            if v is not None and k not in ["proxy", "outtmpl", "cookiefile"]
                        }
                        ydl_opts.update(filtered_cli_opts)
                    except Exception as parse_err:
                        task.logs.append(f"Warning: Could not parse custom args {sanitized_args}: {parse_err}")
                for arg in sanitized_args:
                    task.logs.append(f"Applying sanitized custom arg: {arg}")

            # Ensure no SSL checking is always enforced
            ydl_opts["nocheckcertificate"] = True

            # Run extraction and download in thread pool
            loop = asyncio.get_running_loop()

            def run_ydl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(request.url, download=True)
                    if info:
                        actual_filename = ydl.prepare_filename(info)
                        return actual_filename, info
                    return None, None

            actual_filepath, info_dict = await loop.run_in_executor(None, run_ydl)

            # 5. Detect all generated files for this task
            discovered_files: List[FileInfo] = []
            if downloads_dir.exists():
                for f in downloads_dir.iterdir():
                    if not f.is_file():
                        continue
                    mtime = f.stat().st_mtime
                    # File was newly created or modified during this job execution
                    if f.name not in before_files or mtime >= (start_timestamp - 1.0):
                        info = create_file_info(f)
                        self.file_registry[info.file_id] = f
                        self.file_registry[f.name] = f
                        discovered_files.append(info)

            # If specific actual_filepath found, ensure it is in the list
            if actual_filepath and os.path.exists(actual_filepath):
                p = Path(actual_filepath)
                task.filepath = str(p)
                task.filename = p.name
                if not any(df.filename == p.name for df in discovered_files):
                    info = create_file_info(p)
                    self.file_registry[info.file_id] = p
                    discovered_files.insert(0, info)
            elif discovered_files:
                task.filepath = str(self.file_registry[discovered_files[0].file_id])
                task.filename = discovered_files[0].filename

            task.files = discovered_files
            task.status = "completed"
            task.progress_percent = 100.0
            task.completed_at = datetime.datetime.now(datetime.timezone.utc)
            task.logs.append(f"Task completed. Generated {len(discovered_files)} file(s).")
            self.total_completed_downloads += 1
            self.touch_activity()

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "Download task was cancelled."
            task.completed_at = datetime.datetime.now(datetime.timezone.utc)
            task.logs.append("Download task was cancelled.")
        except Exception as e:
            logger.error(f"Download failed for task {task_id}: {e}", exc_info=True)
            task.status = "failed"
            task.error = str(e)
            task.completed_at = datetime.datetime.now(datetime.timezone.utc)
            task.logs.append(f"Error: {e}")
        finally:
            if cookie_path and os.path.exists(cookie_path):
                try:
                    os.remove(cookie_path)
                except Exception as ex:
                    logger.warning(f"Could not remove cookie file {cookie_path}: {ex}")

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self._async_tasks and not self._async_tasks[task_id].done():
            self._async_tasks[task_id].cancel()
            self.touch_activity()
            return True
        return False

    def get_task(self, task_id: str) -> Optional[DownloadTaskResponse]:
        self.touch_activity()
        return self.tasks.get(task_id)


downloader_manager = DownloadTaskManager()
