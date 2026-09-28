import asyncio
import datetime
import logging
import secrets
import time
import uuid
from typing import Dict, List, Optional, Tuple
import httpx

from app.config import settings
from app.schemas import (
    FileItemResponse,
    JobCreateRequest,
    JobResponse,
)
from app.services.docker_manager import RunnerContainerRecord, docker_manager

logger = logging.getLogger("worker.job_service")


class JobService:
    def __init__(self):
        self.jobs: Dict[str, JobResponse] = {}
        self._sync_tasks: Dict[str, asyncio.Task] = {}
        # Maps file_id (hex) or filename -> (filename, container_id)
        self.file_map: Dict[str, Tuple[str, Optional[str]]] = {}
        # Single-use tokens for job creation: token -> expiry timestamp
        self.single_use_tokens: Dict[str, float] = {}
        # Job tracking tokens: job_id -> tracking_token
        self.job_tracking_tokens: Dict[str, str] = {}

    def generate_single_use_token(self, expires_in: int = 300) -> str:
        self._cleanup_expired_tokens()
        token = secrets.token_urlsafe(32)
        self.single_use_tokens[token] = time.time() + expires_in
        return token

    def validate_and_consume_single_use_token(self, token: Optional[str]) -> bool:
        self._cleanup_expired_tokens()
        if not token:
            return False
        expiry = self.single_use_tokens.pop(token, None)
        if expiry is None:
            return False
        return time.time() <= expiry

    def _cleanup_expired_tokens(self):
        now = time.time()
        expired = [t for t, exp in self.single_use_tokens.items() if exp < now]
        for t in expired:
            self.single_use_tokens.pop(t, None)

    def validate_tracking_token(self, job_id: str, token: Optional[str]) -> bool:
        if not token or not job_id:
            return False
        expected = self.job_tracking_tokens.get(job_id)
        return expected is not None and secrets.compare_digest(expected, token)

    def invalidate_tracking_token(self, job_id: str):
        self.job_tracking_tokens.pop(job_id, None)
        if job_id in self.jobs:
            self.jobs[job_id].tracking_token = None

    def get_active_jobs_count(self) -> int:
        return sum(
            1 for j in self.jobs.values()
            if j.status in ["pending", "starting_container", "installing_plugins", "downloading"]
        )

    async def create_job(self, request: JobCreateRequest) -> JobResponse:
        job_id = str(uuid.uuid4())
        tracking_token = secrets.token_urlsafe(32)
        self.job_tracking_tokens[job_id] = tracking_token
        now = datetime.datetime.now(datetime.timezone.utc)

        job = JobResponse(
            job_id=job_id,
            tracking_token=tracking_token,
            url=request.url,
            status="starting_container",
            plugins=request.plugins or [],
            created_at=now,
            logs=[f"Job created at {now.isoformat()}"],
        )
        self.jobs[job_id] = job


        # Provision or retrieve runner container
        try:
            runner = await docker_manager.get_or_create_runner()
            job.container_id = runner.container_id
            job.container_name = runner.name
            job.logs.append(f"Assigned to runner container: {runner.name}")
            runner.active_jobs_count += 1
            runner.touch()

            # Forward download request to runner HTTP supervisor
            async_task = asyncio.create_task(
                self._dispatch_and_monitor(job_id, runner, request)
            )
            self._sync_tasks[job_id] = async_task

        except Exception as e:
            logger.error(f"Failed to start job {job_id}: {e}", exc_info=True)
            job.status = "failed"
            job.error = str(e)
            job.completed_at = datetime.datetime.now(datetime.timezone.utc)
            job.logs.append(f"Job dispatch error: {e}")

        return job

    async def _dispatch_and_monitor(
        self, job_id: str, runner: RunnerContainerRecord, request: JobCreateRequest
    ):
        job = self.jobs[job_id]
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = {
                    "url": request.url,
                    "cookie_content": request.cookie_content,
                    "custom_args": request.custom_args,
                    "output_template": request.output_template,
                    "format_selection": request.format_selection,
                    "plugins": request.plugins or [],
                }
                resp = await client.post(f"{runner.base_url}/download", json=payload)
                if resp.status_code not in [200, 202]:
                    raise RuntimeError(
                        f"Supervisor returned status {resp.status_code}: {resp.text}"
                    )

                task_data = resp.json()
                task_id = task_data["task_id"]
                job.task_id = task_id
                job.status = "downloading"
                job.logs.append(f"Supervisor task started with ID: {task_id}")

            # Poll status until finished
            while True:
                await asyncio.sleep(1.0)
                runner.touch()

                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{runner.base_url}/download/{job.task_id}")
                    if resp.status_code != 200:
                        continue

                    status_data = resp.json()
                    current_status = status_data.get("status")
                    if current_status:
                        job.status = current_status

                    job.progress_percent = status_data.get("progress_percent", 0.0)
                    job.downloaded_bytes = status_data.get("downloaded_bytes", 0)
                    job.total_bytes = status_data.get("total_bytes")
                    job.speed_bytes_per_sec = status_data.get("speed_bytes_per_sec")
                    job.eta_seconds = status_data.get("eta_seconds")
                    job.filename = status_data.get("filename")
                    
                    if status_data.get("logs"):
                        job.logs = status_data["logs"]

                    if current_status in ["completed", "failed", "cancelled"]:
                        job.error = status_data.get("error")
                        job.completed_at = datetime.datetime.now(datetime.timezone.utc)

                        # Process table of generated files
                        raw_files = status_data.get("files", [])
                        file_items: List[FileItemResponse] = []
                        for rf in raw_files:
                            fid = rf["file_id"]
                            fname = rf["filename"]
                            # Register in worker file map
                            self.file_map[fid] = (fname, runner.container_id)
                            self.file_map[fname] = (fname, runner.container_id)
                            file_items.append(
                                FileItemResponse(
                                    file_id=fid,
                                    filename=fname,
                                    size_bytes=rf["size_bytes"],
                                    mime_type=rf.get("mime_type", "application/octet-stream"),
                                    download_url=f"/api/v1/jobs/{job_id}/download?file_id={fid}",
                                    modified_at=datetime.datetime.fromisoformat(
                                        rf["modified_at"]
                                    ) if isinstance(rf["modified_at"], str) else rf["modified_at"],
                                )
                            )

                        job.files = file_items
                        if file_items:
                            job.download_url = file_items[0].download_url
                            if not job.filename:
                                job.filename = file_items[0].filename
                        elif job.filename:
                            job.download_url = f"/api/v1/jobs/{job_id}/download"

                        break

        except Exception as e:
            logger.error(f"Error during job monitoring for {job_id}: {e}", exc_info=True)
            job.status = "failed"
            job.error = str(e)
            job.completed_at = datetime.datetime.now(datetime.timezone.utc)
            job.logs.append(f"Monitor error: {e}")
        finally:
            runner.active_jobs_count = max(0, runner.active_jobs_count - 1)
            runner.touch()

    async def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or not job.container_id or not job.task_id:
            return False

        runner = docker_manager.get_container(job.container_id)
        if not runner:
            return False

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{runner.base_url}/download/{job.task_id}/cancel")
                if resp.status_code == 200:
                    job.status = "cancelled"
                    job.logs.append("Cancelled by user request.")
                    return True
        except Exception as e:
            logger.warning(f"Error cancelling job {job_id}: {e}")

        return False

    def get_job(self, job_id: str) -> Optional[JobResponse]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> List[JobResponse]:
        return list(self.jobs.values())


job_service = JobService()

