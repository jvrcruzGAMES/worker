from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HealthStatusResponse(BaseModel):
    status: str
    worker_id: str
    worker_name: str
    uptime_seconds: float
    version: str


class WorkerInfoResponse(BaseModel):
    worker_id: str
    worker_name: str
    base_url: str
    health_endpoint: str
    orchestrator_url: str
    tags: List[str]
    metadata: Dict[str, Any]
    version: str
    uptime_seconds: float
