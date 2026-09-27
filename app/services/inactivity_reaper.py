import asyncio
import logging
import httpx

from app.config import settings
from app.services.docker_manager import docker_manager

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
                f"Inactivity reaper started. Containers idle for > {settings.INACTIVITY_TIMEOUT_SECONDS}s (20 mins) will be deleted."
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
        for runner in containers:
            # 1. If local active jobs count > 0, container is busy
            if runner.active_jobs_count > 0:
                runner.touch()
                continue

            # 2. Check remote activity from runner HTTP supervisor
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{runner.base_url}/activity")
                    if resp.status_code == 200:
                        activity_data = resp.json()
                        if activity_data.get("is_busy", False):
                            runner.touch()
                            continue
                        # If remote supervisor reports a more recent activity, sync it
                        remote_idle = activity_data.get("idle_seconds", runner.idle_seconds)
                        if remote_idle < runner.idle_seconds:
                            runner.touch()
            except Exception:
                # If container is unreachable, it may be dead already
                pass

            # 3. Check if idle timeout (20 mins) has been exceeded
            if runner.idle_seconds >= settings.INACTIVITY_TIMEOUT_SECONDS:
                logger.info(
                    f"Runner container '{runner.name}' ({runner.container_id[:12]}) has been inactive for "
                    f"{runner.idle_seconds:.1f}s (exceeds {settings.INACTIVITY_TIMEOUT_SECONDS}s / 20 mins). "
                    f"Reaping container..."
                )
                await docker_manager.stop_and_remove_container(runner.container_id)


inactivity_reaper = InactivityReaperService()
