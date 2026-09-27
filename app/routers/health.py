import time
from fastapi import APIRouter
from app.config import settings
from app.schemas import HealthStatusResponse

router = APIRouter(tags=["Health"])

START_TIME = time.time()


@router.get("/health", response_model=HealthStatusResponse, summary="Worker Health Check")
async def health_check():
    uptime = round(time.time() - START_TIME, 2)
    return HealthStatusResponse(
        status="healthy",
        worker_id=settings.WORKER_ID,
        worker_name=settings.WORKER_NAME,
        uptime_seconds=uptime,
        version=settings.WORKER_VERSION,
    )
