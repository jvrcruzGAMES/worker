import pytest
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.main import app


@pytest.mark.asyncio
async def test_worker_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["worker_id"] == settings.WORKER_ID
        assert data["uptime_seconds"] >= 0


@pytest.mark.asyncio
async def test_worker_info():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_id"] == settings.WORKER_ID
        assert data["base_url"] == settings.WORKER_BASE_URL
        assert data["health_endpoint"] == settings.HEALTH_ENDPOINT
        assert data["orchestrator_url"] == settings.ORCHESTRATOR_URL
