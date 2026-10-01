import time
from fastapi import APIRouter
from app.config import settings
from app.routers.health import START_TIME
from app.schemas import WorkerInfoResponse
from app.services.announcer import announcer
from app.services.crypto import worker_crypto

router = APIRouter(tags=["Discovery & Info"])


@router.get("/info", response_model=WorkerInfoResponse, summary="Worker Information")
@router.get("/api/v1/info", response_model=WorkerInfoResponse, include_in_schema=False)
async def worker_info():
    uptime = round(time.time() - START_TIME, 2)
    return WorkerInfoResponse(
        worker_id=announcer.worker_id or "unregistered",
        worker_name=settings.WORKER_NAME,
        base_url=settings.WORKER_BASE_URL,
        health_endpoint=settings.HEALTH_ENDPOINT,
        orchestrator_url=settings.ORCHESTRATOR_URL,
        tags=settings.WORKER_TAGS,
        metadata=settings.metadata_dict,
        version=settings.WORKER_VERSION,
        public_key=worker_crypto.public_key_b64,
        payout_key=settings.PAYOUT_KEY,
        uptime_seconds=uptime,
    )
