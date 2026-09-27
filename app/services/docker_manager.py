import asyncio
import datetime
import logging
import time
import uuid
from typing import Dict, List, Optional, Tuple
import httpx

from app.config import settings

logger = logging.getLogger("worker.docker_manager")

try:
    import docker
    from docker.errors import DockerException, NotFound
    HAS_DOCKER = True
except ImportError:
    HAS_DOCKER = False
    docker = None
    NotFound = Exception
    DockerException = Exception


class RunnerContainerRecord:
    def __init__(self, container_id: str, name: str, host_or_ip: str, port: int):
        self.container_id = container_id
        self.name = name
        self.host_or_ip = host_or_ip
        self.port = port
        self.base_url = f"http://{host_or_ip}:{port}"
        self.last_activity: float = time.time()
        self.active_jobs_count: int = 0
        self.created_at: float = time.time()

    def touch(self):
        self.last_activity = time.time()

    @property
    def idle_seconds(self) -> float:
        if self.active_jobs_count > 0:
            return 0.0
        return round(time.time() - self.last_activity, 2)

    @property
    def remaining_idle_seconds(self) -> float:
        remaining = settings.INACTIVITY_TIMEOUT_SECONDS - self.idle_seconds
        return max(0.0, round(remaining, 2))


class DockerManager:
    def __init__(self):
        self._client = None
        self._containers: Dict[str, RunnerContainerRecord] = {}
        self._lock = asyncio.Lock()

    def _get_docker_client(self):
        if not HAS_DOCKER:
            return None
        if self._client is None:
            try:
                if settings.DOCKER_HOST:
                    self._client = docker.DockerClient(base_url=settings.DOCKER_HOST)
                else:
                    self._client = docker.from_env()
            except Exception as e:
                logger.warning(f"Could not connect to Docker daemon: {e}. Running in simulation/mock mode.")
                self._client = None
        return self._client

    async def get_or_create_runner(self) -> RunnerContainerRecord:
        async with self._lock:
            # 1. Check if we already have an active healthy runner
            for record in list(self._containers.values()):
                if await self._is_container_healthy(record):
                    record.touch()
                    return record
                else:
                    # Clean up unresponsive runner from internal tracking
                    self._containers.pop(record.container_id, None)

            # 2. Spin up a new child container
            return await self._spawn_runner_container()

    async def _is_container_healthy(self, record: RunnerContainerRecord) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{record.base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def _spawn_runner_container(self) -> RunnerContainerRecord:
        short_id = uuid.uuid4().hex[:8]
        container_name = f"mithril-yt-dlp-runner-{short_id}"
        client = self._get_docker_client()

        if client:
            try:
                logger.info(f"Spawning child yt-dlp container '{container_name}' from image '{settings.RUNNER_IMAGE}'...")

                # Volume configuration
                volumes = {
                    settings.SHARED_DOWNLOADS_VOLUME: {"bind": "/app/downloads", "mode": "rw"},
                    settings.SHARED_COOKIES_VOLUME: {"bind": "/app/cookies", "mode": "rw"},
                }

                container = client.containers.run(
                    image=settings.RUNNER_IMAGE,
                    name=container_name,
                    detach=True,
                    network=settings.DOCKER_NETWORK,
                    volumes=volumes,
                    environment={
                        "DOWNLOADS_DIR": "/app/downloads",
                        "COOKIES_DIR": "/app/cookies",
                        "FLARESOLVERR_URL": settings.FLARESOLVERR_URL,
                        "FLARESOLVERR_PROXY": settings.FLARESOLVERR_PROXY or "",
                    },
                    restart_policy={"Name": "no"},
                )

                # Wait for container to be assigned IP on network or use container_name on docker network
                host = container_name
                port = settings.RUNNER_PORT
                record = RunnerContainerRecord(
                    container_id=container.id,
                    name=container_name,
                    host_or_ip=host,
                    port=port,
                )
                self._containers[container.id] = record

                # Wait for supervisor to be ready (up to 15 seconds)
                ready = False
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    if await self._is_container_healthy(record):
                        ready = True
                        break

                if not ready:
                    logger.warning(f"Container {container_name} started but supervisor health check timed out. Proceeding anyway.")

                logger.info(f"Container {container_name} ({container.id[:12]}) ready at {record.base_url}")
                return record

            except Exception as e:
                logger.error(f"Error spinning up docker container: {e}", exc_info=True)
                # Fallback to simulated local record for tests or environments without docker socket
                return self._create_simulated_record(container_name)
        else:
            return self._create_simulated_record(container_name)

    def _create_simulated_record(self, name: str) -> RunnerContainerRecord:
        sim_id = f"sim-{uuid.uuid4().hex[:8]}"
        # If in testing/dev without docker daemon, point to localhost runner port or mock
        record = RunnerContainerRecord(
            container_id=sim_id,
            name=name,
            host_or_ip="localhost",
            port=settings.RUNNER_PORT,
        )
        self._containers[sim_id] = record
        return record

    async def list_containers(self) -> List[RunnerContainerRecord]:
        return list(self._containers.values())

    def get_container(self, container_id: str) -> Optional[RunnerContainerRecord]:
        return self._containers.get(container_id)

    async def stop_and_remove_container(self, container_id: str) -> bool:
        async with self._lock:
            record = self._containers.pop(container_id, None)
            client = self._get_docker_client()

            if client and record and not record.container_id.startswith("sim-"):
                try:
                    logger.info(f"Stopping and deleting inactive container {record.name} ({container_id[:12]})...")
                    c = client.containers.get(container_id)
                    c.stop(timeout=5)
                    c.remove(v=False, force=True)
                    logger.info(f"Container {record.name} successfully deleted.")
                    return True
                except NotFound:
                    return True
                except Exception as e:
                    logger.error(f"Failed to remove container {container_id}: {e}")
                    return False
            return record is not None


docker_manager = DockerManager()
