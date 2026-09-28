import asyncio
import datetime
import logging
import socket
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

    def _get_worker_networks(self, client) -> List[str]:
        """Detect the Docker network(s) the current worker container is connected to."""
        networks = []
        try:
            hostname = socket.gethostname()
            self_c = None
            try:
                self_c = client.containers.get(hostname)
            except Exception:
                for c in client.containers.list():
                    if c.id.startswith(hostname) or c.name.lstrip("/") == hostname or hostname in c.name:
                        self_c = c
                        break
            if self_c:
                net_map = self_c.attrs.get("NetworkSettings", {}).get("Networks", {})
                networks = list(net_map.keys())
                logger.info(f"Worker detected active Docker networks: {networks}")
        except Exception as e:
            logger.debug(f"Could not auto-detect worker network: {e}")

        if not networks:
            networks = [settings.DOCKER_NETWORK]
        return networks

    def _resolve_runner_image(self, client) -> str:
        """
        Resolves the runner image to use. Checks if present locally, attempts
        pulling if needed, and falls back to local candidates if the registry denies access.
        """
        target = settings.RUNNER_IMAGE

        # 1. Check if configured target image already exists in local Docker engine
        try:
            client.images.get(target)
            return target
        except Exception:
            pass

        # 2. Try pulling from registry
        try:
            logger.info(f"Runner image '{target}' not found locally. Attempting to pull from registry...")
            client.images.pull(target)
            return target
        except Exception as e:
            logger.warning(
                f"Failed to pull image '{target}' from registry ({e}). Checking local fallback images..."
            )

        # 3. Check for any locally available runner images
        candidates = [
            "ghcr.io/jvrcruzgames/yt-dlp-runner:latest",
            "mithril-yt-dlp-runner:latest",
            "yt-dlp-runner:latest",
            "yt-dlp-runner",
        ]
        for img in candidates:
            try:
                client.images.get(img)
                logger.info(f"Using local runner image '{img}'.")
                return img
            except Exception:
                continue

        return target

    async def _spawn_runner_container(self) -> RunnerContainerRecord:
        short_id = uuid.uuid4().hex[:8]
        container_name = f"mithril-yt-dlp-runner-{short_id}"
        client = self._get_docker_client()

        if client:
            try:
                runner_image = self._resolve_runner_image(client)
                logger.info(f"Spawning child yt-dlp container '{container_name}' from image '{runner_image}'...")

                worker_networks = self._get_worker_networks(client)
                primary_network = worker_networks[0]

                # Volume configuration
                volumes = {
                    settings.SHARED_DOWNLOADS_VOLUME: {"bind": "/app/downloads", "mode": "rw"},
                    settings.SHARED_COOKIES_VOLUME: {"bind": "/app/cookies", "mode": "rw"},
                }

                container = client.containers.run(
                    image=runner_image,
                    name=container_name,
                    detach=True,
                    network=primary_network,
                    volumes=volumes,
                    environment={
                        "DOWNLOADS_DIR": "/app/downloads",
                        "COOKIES_DIR": "/app/cookies",
                        "FLARESOLVERR_URL": settings.FLARESOLVERR_URL,
                        "FLARESOLVERR_PROXY": settings.FLARESOLVERR_PROXY or "",
                        "BGUTIL_POT_PROVIDER_URL": settings.BGUTIL_POT_PROVIDER_URL or "",
                        "POT_PROVIDER_URL": settings.BGUTIL_POT_PROVIDER_URL or "",
                    },
                    restart_policy={"Name": "no"},
                )

                # Attach to any additional networks worker belongs to
                for extra_net in worker_networks[1:]:
                    try:
                        net_obj = client.networks.get(extra_net)
                        net_obj.connect(container)
                    except Exception as e:
                        logger.debug(f"Could not connect container to extra network '{extra_net}': {e}")

                port = settings.RUNNER_PORT
                record = RunnerContainerRecord(
                    container_id=container.id,
                    name=container_name,
                    host_or_ip=container_name,
                    port=port,
                )
                self._containers[container.id] = record

                # Wait for container IP & supervisor health check (up to 20 seconds)
                ready = False
                for attempt in range(40):
                    await asyncio.sleep(0.5)
                    try:
                        container.reload()
                        net_map = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                        
                        # Try to resolve IP on shared network
                        discovered_ip = None
                        for net_name in worker_networks:
                            if net_name in net_map and net_map[net_name].get("IPAddress"):
                                discovered_ip = net_map[net_name]["IPAddress"]
                                break
                        
                        if not discovered_ip:
                            for n_data in net_map.values():
                                if n_data.get("IPAddress"):
                                    discovered_ip = n_data["IPAddress"]
                                    break

                        # Check container health using discovered IP first
                        if discovered_ip:
                            record.host_or_ip = discovered_ip
                            record.base_url = f"http://{discovered_ip}:{port}"
                            if await self._is_container_healthy(record):
                                ready = True
                                break

                        # Fall back to testing container name DNS
                        record.host_or_ip = container_name
                        record.base_url = f"http://{container_name}:{port}"
                        if await self._is_container_healthy(record):
                            ready = True
                            break

                    except Exception as poll_err:
                        logger.debug(f"Polling runner container readiness: {poll_err}")

                if not ready:
                    container_status = "unknown"
                    container_logs = ""
                    try:
                        container.reload()
                        container_status = container.status
                        container_logs = container.logs(tail=30).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    
                    self._containers.pop(container.id, None)
                    try:
                        container.stop(timeout=2)
                        container.remove(v=False, force=True)
                    except Exception:
                        pass

                    raise RuntimeError(
                        f"Runner container '{container_name}' failed health check at {record.base_url} (status={container_status}). Logs:\n{container_logs}"
                    )

                logger.info(f"Container {container_name} ({container.id[:12]}) ready at {record.base_url}")
                return record

            except Exception as e:
                logger.error(f"Error spinning up docker container: {e}", exc_info=True)
                if client:
                    raise
                return self._create_simulated_record(container_name)
        else:
            return self._create_simulated_record(container_name)

    def _create_simulated_record(self, name: str) -> RunnerContainerRecord:
        sim_id = f"sim-{uuid.uuid4().hex[:8]}"
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
