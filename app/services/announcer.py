import asyncio
import logging
from typing import Dict
import httpx

from app.config import settings

logger = logging.getLogger("worker.announcer")


class OrchestratorAnnouncer:
    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None
        self._is_registered = False

    @property
    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if settings.MITHRIL_TOKEN:
            headers["Authorization"] = f"Bearer {settings.MITHRIL_TOKEN}"
        return headers

    def start(self):
        if not settings.AUTO_ANNOUNCE:
            logger.info("AUTO_ANNOUNCE is disabled. Skipping orchestrator registration.")
            return
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._lifecycle_loop())
            logger.info(
                f"Announcer started. Targeting Orchestrator at '{settings.ORCHESTRATOR_URL}'"
            )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        # Attempt graceful deregistration
        if self._is_registered:
            await self._unregister()
        logger.info("Announcer stopped.")

    async def _lifecycle_loop(self):
        # 1. Registration loop (retry until registered or stopped)
        while self._running and not self._is_registered:
            try:
                registered = await self._announce()
                if registered:
                    self._is_registered = True
                    logger.info("Successfully announced worker to orchestrator.")
                    break
            except Exception as e:
                logger.warning(
                    f"Announcement to {settings.ORCHESTRATOR_URL} failed: {e}. Retrying in {settings.ANNOUNCE_RETRY_INTERVAL_SECONDS}s..."
                )
            await asyncio.sleep(settings.ANNOUNCE_RETRY_INTERVAL_SECONDS)

        # 2. Heartbeat loop
        while self._running:
            await asyncio.sleep(settings.HEARTBEAT_INTERVAL_SECONDS)
            try:
                await self._send_heartbeat()
            except Exception as e:
                logger.warning(f"Heartbeat to {settings.ORCHESTRATOR_URL} failed: {e}")

    async def _announce(self) -> bool:
        announce_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/announce"
        payload = {
            "worker_id": settings.WORKER_ID,
            "name": settings.WORKER_NAME,
            "base_url": settings.WORKER_BASE_URL,
            "health_endpoint": settings.HEALTH_ENDPOINT,
            "status": "online",
            "metadata": settings.metadata_dict,
            "tags": settings.WORKER_TAGS,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(announce_url, json=payload, headers=self._headers)
            if resp.status_code in [200, 201]:
                return True
            else:
                logger.warning(
                    f"Orchestrator returned status {resp.status_code} on announce: {resp.text}"
                )
                return False

    async def _send_heartbeat(self):
        heartbeat_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/heartbeat"
        payload = {
            "worker_id": settings.WORKER_ID,
            "status": "online",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(heartbeat_url, json=payload, headers=self._headers)
            if resp.status_code == 404:
                logger.warning("Orchestrator forgot this worker. Re-announcing...")
                await self._announce()
            elif resp.status_code != 200:
                logger.warning(f"Orchestrator heartbeat returned {resp.status_code}: {resp.text}")

    async def _unregister(self):
        unregister_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/unregister"
        payload = {
            "worker_id": settings.WORKER_ID,
        }
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(unregister_url, json=payload, headers=self._headers)
                logger.info("Gracefully unregistered from orchestrator.")
        except Exception as e:
            logger.warning(f"Could not unregister from orchestrator during shutdown: {e}")


announcer = OrchestratorAnnouncer()
