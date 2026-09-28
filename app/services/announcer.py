import asyncio
import logging
from typing import Dict, Optional
import httpx

from app.config import settings

from app.services.integrity import worker_integrity

logger = logging.getLogger("worker.announcer")


class OrchestratorAnnouncer:
    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None
        self._is_registered = False
        self._worker_id: Optional[str] = None
        self._auth_token: Optional[str] = None

    @property
    def worker_id(self) -> Optional[str]:
        return self._worker_id

    @property
    def auth_token(self) -> Optional[str]:
        return self._auth_token

    @property
    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Worker {self._auth_token}"
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
        if self._is_registered and self._auth_token:
            await self._unregister()
        logger.info("Announcer stopped.")

    async def _lifecycle_loop(self):
        # 1. Registration loop (retry until registered or stopped)
        while self._running and not self._is_registered:
            try:
                registered = await self._announce()
                if registered:
                    self._is_registered = True
                    logger.info(
                        f"Successfully announced worker to orchestrator. Assigned Worker ID: '{self._worker_id}'"
                    )
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
        commit_sha = worker_integrity.get_commit_sha()
        challenge_id: Optional[str] = None
        proof: Optional[str] = None

        async with httpx.AsyncClient(timeout=8.0) as client:
            # Step 1: Request integrity challenge if enabled
            if settings.INTEGRITY_CHECK_ENABLED:
                challenge_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/integrity/challenge"
                challenge_req = {
                    "commit_sha": commit_sha,
                    "base_url": settings.WORKER_BASE_URL,
                    "version": settings.WORKER_VERSION,
                }
                chal_resp = await client.post(challenge_url, json=challenge_req, headers={"Accept": "application/json"})
                if chal_resp.status_code != 200:
                    logger.error(
                        f"Integrity challenge request rejected (HTTP {chal_resp.status_code}): {chal_resp.text}"
                    )
                    return False
                
                chal_data = chal_resp.json()
                challenge_id = chal_data.get("challenge_id")
                nonce = chal_data.get("nonce")
                sampled_files = chal_data.get("sampled_files", [])

                if challenge_id != "disabled" and nonce and sampled_files:
                    proof = worker_integrity.compute_challenge_proof(
                        nonce=nonce,
                        commit_sha=commit_sha,
                        sampled_files=sampled_files,
                    )

            # Step 2: Submit announcement with challenge proof and container image attestation
            image_ref, image_digest = worker_integrity.get_image_metadata()
            runner_commit_sha = worker_integrity.get_runner_commit_sha()
            runner_image_ref, runner_image_digest = worker_integrity.get_runner_image_metadata()

            announce_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/announce"
            payload = {
                "name": settings.WORKER_NAME,
                "base_url": settings.WORKER_BASE_URL,
                "health_endpoint": settings.HEALTH_ENDPOINT,
                "status": "online",
                "metadata": settings.metadata_dict,
                "tags": settings.WORKER_TAGS,
                "challenge_id": challenge_id,
                "proof": proof,
                "commit_sha": commit_sha,
                "image_ref": image_ref,
                "image_digest": image_digest,
                "runner_commit_sha": runner_commit_sha,
                "runner_image_ref": runner_image_ref,
                "runner_image_digest": runner_image_digest,
            }
            resp = await client.post(announce_url, json=payload, headers={"Accept": "application/json"})
            if resp.status_code in [200, 201]:
                data = resp.json()
                self._worker_id = data.get("worker_id") or data.get("id")
                self._auth_token = data.get("token")
                return True
            else:
                logger.warning(
                    f"Orchestrator returned status {resp.status_code} on announce: {resp.text}"
                )
                return False

    async def _send_heartbeat(self):
        if not self._auth_token:
            await self._announce()
            return

        heartbeat_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/heartbeat"
        payload = {
            "status": "online",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(heartbeat_url, json=payload, headers=self._headers)
            if resp.status_code == 401 or resp.status_code == 404:
                logger.warning("Orchestrator auth expired or rejected. Re-announcing...")
                self._auth_token = None
                self._is_registered = False
                await self._announce()
            elif resp.status_code != 200:
                logger.warning(f"Orchestrator heartbeat returned {resp.status_code}: {resp.text}")

    async def _unregister(self):
        if not self._auth_token:
            return

        unregister_url = f"{settings.ORCHESTRATOR_URL}/api/v1/workers/unregister"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(unregister_url, headers=self._headers)
                logger.info("Gracefully unregistered from orchestrator.")
        except Exception as e:
            logger.warning(f"Could not unregister from orchestrator during shutdown: {e}")


announcer = OrchestratorAnnouncer()

