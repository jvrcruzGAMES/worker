import asyncio
import logging
import time
import httpx

from app.config import settings
from app.services.docker_manager import docker_manager
from app.services.job_service import job_service

logger = logging.getLogger("worker.inactivity_reaper")


class InactivityReaperService:
    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._reaper_loop())
            logger.info(
                f"Inactivity reaper started. Max container lifetime: {settings.MAX_CONTAINER_LIFETIME_SECONDS}s (20m), "
                f"File retention window: {settings.FILE_AVAILABLE_WINDOW_SECONDS}s (5m)."
            )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Inactivity reaper stopped.")

    async def _reaper_loop(self):
        while self._running:
            try:
                await self._check_and_reap_containers()
            except Exception as e:
                logger.error(f"Error during inactivity reap loop: {e}", exc_info=True)
            await asyncio.sleep(settings.INACTIVITY_CHECK_INTERVAL_SECONDS)

    async def _check_and_reap_containers(self):
        containers = await docker_manager.list_containers()
        now = time.time()
        for runner in containers:
            # 1. If active jobs count > 0 or client is actively downloading a file, container must stay alive
            if runner.active_jobs_count > 0 or runner.active_downloads_count > 0:
                runner.touch()
                continue

            # 2. Check remote activity from runner HTTP supervisor
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{runner.base_url}/activity")
                    if resp.status_code == 200:
                        activity_data = resp.json()
                        if activity_data.get("is_busy", False) or activity_data.get("active_tasks_count", 0) > 0:
                            runner.touch()
                            continue
                        remote_idle = activity_data.get("idle_seconds", runner.idle_seconds)
                        if remote_idle < runner.idle_seconds:
                            runner.touch()
            except Exception:
                pass

            # 3. Check if all completed files for this container have been downloaded (and no downloads in flight)
            if (
                runner.last_job_completed_at is not None
                and runner.active_downloads_count == 0
                and job_service.are_all_container_jobs_downloaded(runner.container_id)
            ):
                logger.info(
                    f"Runner container '{runner.name}' ({runner.container_id[:12]}) has all files downloaded. "
                    f"Reaping container immediately..."
                )
                await docker_manager.stop_and_remove_container(runner.container_id)
                continue

            # 4. Check if the 5-minute after-download file available retention window has expired
            if runner.last_job_completed_at is not None:
                elapsed_since_completion = now - runner.last_job_completed_at
                if elapsed_since_completion >= settings.FILE_AVAILABLE_WINDOW_SECONDS:
                    logger.info(
                        f"Runner container '{runner.name}' ({runner.container_id[:12]}) after-download retention window expired "
                        f"({elapsed_since_completion:.1f}s >= {settings.FILE_AVAILABLE_WINDOW_SECONDS}s / 5 mins). Reaping container..."
                    )
                    await docker_manager.stop_and_remove_container(runner.container_id)
                    continue

            # 5. Check if max container lifetime (20 mins) has been reached
            if runner.lifetime_seconds >= settings.MAX_CONTAINER_LIFETIME_SECONDS:
                logger.info(
                    f"Runner container '{runner.name}' ({runner.container_id[:12]}) has exceeded max lifetime "
                    f"({runner.lifetime_seconds:.1f}s >= {settings.MAX_CONTAINER_LIFETIME_SECONDS}s / 20 mins). Reaping container..."
                )
                await docker_manager.stop_and_remove_container(runner.container_id)
                continue

            # 6. Check standard idle timeout (20 mins idle without any jobs)
            if runner.idle_seconds >= settings.INACTIVITY_TIMEOUT_SECONDS:
                logger.info(
                    f"Runner container '{runner.name}' ({runner.container_id[:12]}) has been inactive for "
                    f"{runner.idle_seconds:.1f}s. Reaping container..."
                )
                await docker_manager.stop_and_remove_container(runner.container_id)


inactivity_reaper = InactivityReaperService()
