import asyncio
import datetime
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
import yt_dlp

from app.config import settings
from app.schemas import DownloadRequest, DownloadTaskResponse

logger = logging.getLogger("yt_dlp_runner.downloader")


class DownloadTaskManager:
    def __init__(self):
        self.tasks: Dict[str, DownloadTaskResponse] = {}
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self.last_activity_time: float = time.time()
        self.total_completed_downloads: int = 0

    def touch_activity(self):
        self.last_activity_time = time.time()

    def get_active_tasks_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ["pending", "downloading", "processing"])

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

        try:
            self.touch_activity()
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
                    task.logs.append(f"Download finished, processing/merging formats...")

            class CustomYDLLogger:
                def debug(self, msg):
                    if not msg.startswith("[debug]"):
                        task.logs.append(msg)

                def warning(self, msg):
                    task.logs.append(f"[WARNING] {msg}")

                def error(self, msg):
                    task.logs.append(f"[ERROR] {msg}")

            # 4. Prepare yt-dlp options
            ydl_opts: Dict[str, Any] = {
                "outtmpl": outtmpl,
                "progress_hooks": [ydl_progress_hook],
                "logger": CustomYDLLogger(),
                "noplaylist": True,
                "nocheckcertificate": False,
            }

            if cookie_path and os.path.exists(cookie_path):
                ydl_opts["cookiefile"] = cookie_path

            if request.format_selection:
                ydl_opts["format"] = request.format_selection

            # Run extraction and download in thread pool to not block asyncio event loop
            loop = asyncio.get_running_loop()

            def run_ydl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(request.url, download=True)
                    # Resolve final filename
                    if info:
                        actual_filename = ydl.prepare_filename(info)
                        return actual_filename, info
                    return None, None

            actual_filepath, info_dict = await loop.run_in_executor(None, run_ydl)

            if actual_filepath and os.path.exists(actual_filepath):
                task.filepath = actual_filepath
                task.filename = os.path.basename(actual_filepath)
            elif not task.filename:
                # Check downloads directory for any newly created file
                files = sorted(
                    Path(settings.DOWNLOADS_DIR).iterdir(),
                    key=os.path.getmtime,
                    reverse=True,
                )
                if files:
                    task.filepath = str(files[0])
                    task.filename = files[0].name

            task.status = "completed"
            task.progress_percent = 100.0
            task.completed_at = datetime.datetime.now(datetime.timezone.utc)
            task.logs.append("Task successfully completed.")
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
            # Clean up isolated cookie file after task execution
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
