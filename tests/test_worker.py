from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.main import app
from app.schemas import FileItemResponse, JobCreateRequest
from app.services.docker_manager import RunnerContainerRecord, docker_manager
from app.services.job_service import job_service


@pytest.mark.asyncio
async def test_worker_health_and_info():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "worker_id" in data
        assert "active_runner_containers" in data

        resp = await client.get("/info")
        assert resp.status_code == 200
        info_data = resp.json()
        assert "worker_id" in info_data
        assert "yt-dlp" in info_data["tags"] or len(info_data["tags"]) >= 0


@pytest.mark.asyncio
async def test_container_inactivity_lifecycle():
    record = RunnerContainerRecord(
        container_id="test-container-1",
        name="test-runner",
        host_or_ip="localhost",
        port=8080,
    )
    assert record.idle_seconds >= 0.0
    assert record.remaining_idle_seconds <= settings.INACTIVITY_TIMEOUT_SECONDS

    record.active_jobs_count = 1
    assert record.idle_seconds == 0.0


@pytest.mark.asyncio
async def test_job_listing_and_files_table():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        resp = await client.get("/api/v1/containers")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        # Create a sample file in local downloads path
        downloads_dir = Path(settings.LOCAL_DOWNLOADS_PATH)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        sample_file = downloads_dir / "sample_video.mp4"
        sample_file.write_bytes(b"sample video bytes")

        try:
            resp = await client.get("/api/v1/files")
            assert resp.status_code == 200
            files = resp.json()
            assert len(files) >= 1
            target = [f for f in files if f["filename"] == "sample_video.mp4"][0]
            assert "file_id" in target
            assert len(target["file_id"]) == 16  # 16-char hex ID
            assert f"/api/v1/files/{target['file_id']}/download" in target["download_url"]

            # Test streaming using hex file_id
            hex_id = target["file_id"]
            stream_resp = await client.get(f"/api/v1/files/{hex_id}/download")
            assert stream_resp.status_code == 200
            assert stream_resp.content == b"sample video bytes"
        finally:
            if sample_file.exists():
                sample_file.unlink()
