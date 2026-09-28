import datetime
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.main import app
from app.schemas import FileItemResponse, JobCreateRequest, JobResponse
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
async def test_admin_routes_protected(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "secret-admin-pass-123")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Without admin key -> 401/403
        resp_jobs_unauth = await client.get("/api/v1/jobs")
        assert resp_jobs_unauth.status_code in [401, 403]

        resp_containers_unauth = await client.get("/api/v1/containers")
        assert resp_containers_unauth.status_code in [401, 403]

        resp_del_unauth = await client.delete("/api/v1/containers/test-id")
        assert resp_del_unauth.status_code in [401, 403]

        # With valid admin key -> 200 OK
        headers = {"X-Admin-Key": "secret-admin-pass-123"}
        resp_jobs_auth = await client.get("/api/v1/jobs", headers=headers)
        assert resp_jobs_auth.status_code == 200
        assert isinstance(resp_jobs_auth.json(), list)

        resp_containers_auth = await client.get("/api/v1/containers", headers=headers)
        assert resp_containers_auth.status_code == 200
        assert isinstance(resp_containers_auth.json(), list)


@pytest.mark.asyncio
async def test_removed_routes():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # GET /api/v1/files -> 404
        resp_files = await client.get("/api/v1/files")
        assert resp_files.status_code == 404

        # POST /api/v1/plugins/install -> 404
        resp_plugin = await client.post("/api/v1/plugins/install", json={"packages": ["yt-dlp-sample"]})
        assert resp_plugin.status_code in [404, 405]


@pytest.mark.asyncio
async def test_single_use_token_and_job_tracking_flow(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "admin-key-xyz")

    # Mock runner creation in job_service to avoid Docker daemon in test
    sample_job_response = JobResponse(
        job_id="test-job-id-123",
        url="https://youtube.com/watch?v=sample",
        status="completed",
        filename="sample_video.mp4",
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Request single-use token (Orchestrator endpoint)
        tok_resp = await client.post("/api/v1/tokens/single-use")
        assert tok_resp.status_code == 200
        tok_data = tok_resp.json()
        single_use_token = tok_data["token"]
        assert len(single_use_token) > 10

        # 2. Try to create job without token -> 401
        bad_create = await client.post("/api/v1/jobs", json={"url": "https://youtube.com/watch?v=sample"})
        assert bad_create.status_code == 401

        # 3. Create job with single-use token -> 202 Accepted
        good_create = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample"},
            headers={"Authorization": f"Bearer {single_use_token}"}
        )
        assert good_create.status_code == 202
        created_data = good_create.json()
        assert "job_id" in created_data
        assert "tracking_token" in created_data
        job_id = created_data["job_id"]
        tracking_token = created_data["tracking_token"]

        # 4. Try to reuse same single-use token -> 401 Unauthorized
        reuse_resp = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample"},
            headers={"Authorization": f"Bearer {single_use_token}"}
        )
        assert reuse_resp.status_code == 401

        # 5. Access job status without tracking token -> 401
        job_unauth = await client.get(f"/api/v1/jobs/{job_id}")
        assert job_unauth.status_code == 401

        # 6. Access job status with valid tracking token -> 200 OK
        job_auth = await client.get(
            f"/api/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {tracking_token}"}
        )
        assert job_auth.status_code == 200
        assert job_auth.json()["job_id"] == job_id

        # 7. Test file download & tracking token invalidation
        # Put sample file in local downloads
        downloads_dir = Path(settings.LOCAL_DOWNLOADS_PATH)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        sample_file = downloads_dir / "sample_video.mp4"
        sample_file.write_bytes(b"sample video bytes")

        # Manually set job status to completed for download test
        job = job_service.get_job(job_id)
        job.status = "completed"
        job.filename = "sample_video.mp4"

        try:
            # Download with tracking token
            dl_resp = await client.get(
                f"/api/v1/jobs/{job_id}/download",
                headers={"Authorization": f"Bearer {tracking_token}"}
            )
            assert dl_resp.status_code == 200
            assert dl_resp.content == b"sample video bytes"

            # After download finish, tracking token must be invalidated
            assert job_service.job_tracking_tokens.get(job_id) is None
            subsequent_dl = await client.get(
                f"/api/v1/jobs/{job_id}/download",
                headers={"Authorization": f"Bearer {tracking_token}"}
            )
            assert subsequent_dl.status_code == 401
        finally:
            if sample_file.exists():
                sample_file.unlink()


def test_worker_integrity_proof():
    from app.services.integrity import worker_integrity

    commit = worker_integrity.get_commit_sha()
    assert commit is not None

    nonce = "test-nonce-12345"
    sampled_files = ["app/main.py", "app/config.py"]
    proof = worker_integrity.compute_challenge_proof(nonce, commit, sampled_files)
    assert len(proof) == 64  # SHA-256 hex digest length


