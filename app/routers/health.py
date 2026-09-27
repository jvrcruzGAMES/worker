import time
from fastapi import APIRouter
from app.config import settings
from app.schemas import HealthStatusResponse
from app.services.docker_manager import docker_manager
from app.services.job_service import job_service

router = APIRouter(tags=["Health"])

START_TIME = time.time()


@router.get("/health", response_model=HealthStatusResponse, summary="Worker Health Check")
async def health_check():
    uptime = round(time.time() - START_TIME, 2)
    containers = await docker_manager.list_containers()
    active_jobs = job_service.get_active_jobs_count()
    return HealthStatusResponse(
        status="healthy",
        worker_id=settings.WORKER_ID,
        worker_name=settings.WORKER_NAME,
        uptime_seconds=uptime,
        version=settings.WORKER_VERSION,
        active_runner_containers=len(containers),
        active_jobs=active_jobs,
    )
