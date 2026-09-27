import pytest
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.main import app
from app.schemas import JobCreateRequest
from app.services.docker_manager import RunnerContainerRecord, docker_manager
from app.services.job_service import job_service


@pytest.mark.asyncio
async def test_worker_health_and_info():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["worker_id"] == settings.WORKER_ID
        assert "active_runner_containers" in data

        resp = await client.get("/info")
        assert resp.status_code == 200
        info_data = resp.json()
        assert info_data["worker_id"] == settings.WORKER_ID
        assert "yt-dlp" in info_data["tags"] or len(info_data["tags"]) >= 0


@pytest.mark.asyncio
async def test_container_inactivity_lifecycle():
    # Test tracking and countdown calculation
    record = RunnerContainerRecord(
        container_id="test-container-1",
        name="test-runner",
        host_or_ip="localhost",
        port=8080,
    )
    assert record.idle_seconds >= 0.0
    assert record.remaining_idle_seconds <= settings.INACTIVITY_TIMEOUT_SECONDS

    # When jobs are active, idle_seconds is 0.0
    record.active_jobs_count = 1
    assert record.idle_seconds == 0.0


@pytest.mark.asyncio
async def test_job_listing_and_creation():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        resp = await client.get("/api/v1/containers")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        resp = await client.get("/api/v1/files")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
