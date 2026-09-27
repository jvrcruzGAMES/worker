import asyncio
import datetime
import logging
import uuid
from typing import Dict, List, Optional
import httpx

from app.config import settings
from app.schemas import (
    JobCreateRequest,
    JobResponse,
    PluginInstallRequest,
    PluginInstallResponse,
)
from app.services.docker_manager import RunnerContainerRecord, docker_manager

logger = logging.getLogger("worker.job_service")


class JobService:
    def __init__(self):
        self.jobs: Dict[str, JobResponse] = {}
        self._sync_tasks: Dict[str, asyncio.Task] = {}

    def get_active_jobs_count(self) -> int:
        return sum(
            1 for j in self.jobs.values()
            if j.status in ["pending", "starting_container", "downloading"]
        )

    async def create_job(self, request: JobCreateRequest) -> JobResponse:
        job_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)

        job = JobResponse(
            job_id=job_id,
            url=request.url,
            status="starting_container",
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
            async with httpx.AsyncClient(timeout=10.0) as client:
                payload = {
                    "url": request.url,
                    "cookie_content": request.cookie_content,
                    "custom_args": request.custom_args,
                    "output_template": request.output_template,
                    "format_selection": request.format_selection,
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
                await asyncio.sleep(1.5)
                runner.touch()

                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{runner.base_url}/download/{job.task_id}")
                    if resp.status_code != 200:
                        continue

                    status_data = resp.json()
                    job.progress_percent = status_data.get("progress_percent", 0.0)
                    job.downloaded_bytes = status_data.get("downloaded_bytes", 0)
                    job.total_bytes = status_data.get("total_bytes")
                    job.speed_bytes_per_sec = status_data.get("speed_bytes_per_sec")
                    job.eta_seconds = status_data.get("eta_seconds")
                    job.filename = status_data.get("filename")
                    
                    if status_data.get("logs"):
                        job.logs = status_data["logs"]

                    current_status = status_data.get("status")
                    if current_status in ["completed", "failed", "cancelled"]:
                        job.status = current_status
                        job.error = status_data.get("error")
                        job.completed_at = datetime.datetime.now(datetime.timezone.utc)
                        if job.filename:
                            job.download_url = f"/api/v1/files/{job.filename}"
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

    async def install_plugins(self, request: PluginInstallRequest) -> PluginInstallResponse:
        runner = None
        if request.container_id:
            runner = docker_manager.get_container(request.container_id)
        
        if not runner:
            runner = await docker_manager.get_or_create_runner()

        runner.touch()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{runner.base_url}/plugins/install",
                json={"packages": request.packages, "upgrade": True},
            )
            data = resp.json()
            return PluginInstallResponse(
                success=data.get("success", False),
                container_id=runner.container_id,
                packages=request.packages,
                stdout=data.get("stdout", ""),
                stderr=data.get("stderr", ""),
            )


job_service = JobService()
