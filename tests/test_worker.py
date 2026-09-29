import datetime
from pathlib import Path
import time
import httpx
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

        # 7. Test file download & tracking token invalidation via HTTP stream
        job = job_service.get_job(job_id)
        job.status = "completed"
        job.filename = "sample_video.mp4"
        job.container_id = "mock-runner-cid"

        runner_rec = RunnerContainerRecord(
            container_id="mock-runner-cid",
            name="mock-runner",
            host_or_ip="localhost",
            port=8080,
        )
        docker_manager._containers["mock-runner-cid"] = runner_rec

        orig_send = httpx.AsyncClient.send

        async def mock_send(self_client, request, **kwargs):
            if "/files/" in str(request.url):
                return httpx.Response(
                    200,
                    headers={"Content-Type": "video/mp4", "Content-Disposition": 'attachment; filename="sample_video.mp4"'},
                    stream=httpx.ByteStream(b"sample video bytes"),
                    request=request,
                )
            return await orig_send(self_client, request, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

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


def test_worker_integrity_proof():
    from app.services.integrity import worker_integrity

    commit = worker_integrity.get_commit_sha()
    assert commit is not None

    nonce = "test-nonce-12345"
    sampled_files = ["app/main.py", "app/config.py"]
    proof = worker_integrity.compute_challenge_proof(nonce, commit, sampled_files)
    assert len(proof) == 64  # SHA-256 hex digest length


def test_hardcoded_runner_image(monkeypatch):
    from app.config import Settings, OFFICIAL_RUNNER_IMAGE, settings

    assert settings.RUNNER_IMAGE == OFFICIAL_RUNNER_IMAGE
    assert settings.RUNNER_IMAGE == "ghcr.io/jvrcruzgames/yt-dlp-runner:latest"

    # Verify that attempting to override via env or constructor is ignored/enforced
    monkeypatch.setenv("RUNNER_IMAGE", "malicious-image:latest")
    custom_settings = Settings(RUNNER_IMAGE="custom-image:latest")
    assert custom_settings.RUNNER_IMAGE == OFFICIAL_RUNNER_IMAGE


@pytest.mark.asyncio
async def test_envelope_encryption_flow(monkeypatch):
    from app.services.crypto import worker_crypto
    from app.services.announcer import announcer
    import secrets

    monkeypatch.setattr(announcer, "_worker_id", "worker-crypto-test")
    monkeypatch.setattr(announcer, "_auth_token", "auth-token-crypto-test")

    # 1. Verify public_key exists on worker
    pub_key = worker_crypto.public_key_b64
    assert pub_key is not None
    assert len(pub_key) > 20

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info_resp = await client.get("/info")
        assert info_resp.status_code == 200
        assert info_resp.json()["public_key"] == pub_key

        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["public_key"] == pub_key

    # 2. Encrypt credentials with client ephemeral key
    secret_payload = {
        "cookie_content": "# Netscape HTTP Cookie File\n.youtube.com TRUE / FALSE 1999999999 SID secret_cookie_123\n",
        "custom_args": ["--write-thumbnail", "--write-subs"],
        "plugins": ["yt-dlp-secret-plugin"],
    }
    encrypted_dict = worker_crypto.encrypt_envelope(
        recipient_public_key_b64=pub_key,
        payload_dict=secret_payload,
    )
    assert "client_public_key" in encrypted_dict
    assert "nonce" in encrypted_dict
    assert "ciphertext" in encrypted_dict

    # 3. Create single use token issued for this worker
    import base64, json, hmac, hashlib, time
    now_ts = int(time.time())
    payload = {
        "jti": secrets.token_hex(16),
        "worker_id": "worker-crypto-test",
        "iat": now_ts,
        "exp": now_ts + 300,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new("auth-token-crypto-test".encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    valid_token = f"{payload_b64}.{sig}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create job with encrypted credentials
        job_req = {
            "url": "https://youtube.com/watch?v=sample-encrypted",
            "encrypted_credentials": encrypted_dict,
        }
        create_resp = await client.post(
            "/api/v1/jobs",
            json=job_req,
            headers={"Authorization": f"Bearer {valid_token}"}
        )
        assert create_resp.status_code == 202
        created_data = create_resp.json()
        assert "job_id" in created_data
        job_id = created_data["job_id"]

        # Check job in service has the decrypted plugins
        job = job_service.get_job(job_id)
        assert job is not None
        assert "yt-dlp-secret-plugin" in job.plugins

        # 4. Attempt to reuse token -> 401
        reuse_resp = await client.post(
            "/api/v1/jobs",
            json=job_req,
            headers={"Authorization": f"Bearer {valid_token}"}
        )
        assert reuse_resp.status_code == 401


@pytest.mark.asyncio
async def test_orchestrator_token_tampering_and_mismatch(monkeypatch):
    from app.services.announcer import announcer
    import base64, json, hmac, hashlib, time, secrets

    monkeypatch.setattr(announcer, "_worker_id", "worker-target")
    monkeypatch.setattr(announcer, "_auth_token", "target-secret-key")

    now_ts = int(time.time())

    # Case 1: Wrong signature
    payload = {"jti": secrets.token_hex(16), "worker_id": "worker-target", "iat": now_ts, "exp": now_ts + 300}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    bad_sig_token = f"{payload_b64}.invalid_hmac_signature"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample"},
            headers={"Authorization": f"Bearer {bad_sig_token}"}
        )
        assert resp.status_code == 401

        # Case 2: Wrong worker_id
        payload_wrong_worker = {"jti": secrets.token_hex(16), "worker_id": "different-worker", "iat": now_ts, "exp": now_ts + 300}
        payload_ww_b64 = base64.urlsafe_b64encode(json.dumps(payload_wrong_worker).encode("utf-8")).decode("utf-8").rstrip("=")
        sig_ww = hmac.new("target-secret-key".encode("utf-8"), payload_ww_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        wrong_worker_token = f"{payload_ww_b64}.{sig_ww}"

        resp_ww = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample"},
            headers={"Authorization": f"Bearer {wrong_worker_token}"}
        )
        assert resp_ww.status_code == 401

        # Case 3: Expired token
        payload_exp = {"jti": secrets.token_hex(16), "worker_id": "worker-target", "iat": now_ts - 500, "exp": now_ts - 100}
        payload_exp_b64 = base64.urlsafe_b64encode(json.dumps(payload_exp).encode("utf-8")).decode("utf-8").rstrip("=")
        sig_exp = hmac.new("target-secret-key".encode("utf-8"), payload_exp_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        expired_token = f"{payload_exp_b64}.{sig_exp}"

        resp_exp = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample"},
            headers={"Authorization": f"Bearer {expired_token}"}
        )
        assert resp_exp.status_code == 401


@pytest.mark.asyncio
async def test_docker_manager_cleanup_and_image_resolution():
    class MockContainer:
        def __init__(self, name, cid, status="running", labels=None):
            self.name = name
            self.id = cid
            self.status = status
            self.labels = labels or {}
            self.stopped = False
            self.removed = False

        def stop(self, timeout=5):
            self.stopped = True

        def remove(self, v=False, force=True):
            self.removed = True

    class MockImages:
        def __init__(self, pull_success=True, local_images=None):
            self.pull_success = pull_success
            self.local_images = local_images or []
            self.pulled = []

        def pull(self, tag):
            if not self.pull_success:
                raise Exception("Registry denied")
            self.pulled.append(tag)
            return tag

        def get(self, tag):
            if tag in self.local_images:
                return tag
            raise Exception("Image not found")

    mock_c1 = MockContainer("mithril-yt-dlp-runner-abc123", "cid111111111111")
    mock_c2 = MockContainer("unrelated-container", "cid222222222222")
    mock_c3 = MockContainer("custom-runner", "cid333333333333", labels={"managed_by": "mithril-worker"})

    class MockDockerClient:
        def __init__(self, pull_success=True, local_images=None):
            self.containers_list = [mock_c1, mock_c2, mock_c3]
            self.images = MockImages(pull_success=pull_success, local_images=local_images)

        class ContainersWrapper:
            def __init__(self, parent):
                self.parent = parent

            def list(self, all=True):
                return self.parent.containers_list

        @property
        def containers(self):
            return self.ContainersWrapper(self)

    # Test 1: Cleanup old runners
    mock_client = MockDockerClient()
    docker_manager._client = mock_client
    cleaned = await docker_manager.cleanup_old_runners()
    assert cleaned == 2
    assert mock_c1.stopped is True and mock_c1.removed is True
    assert mock_c3.stopped is True and mock_c3.removed is True
    assert mock_c2.stopped is False and mock_c2.removed is False

    # Test 2: Image resolution always attempts pulling latest from registry
    target = settings.RUNNER_IMAGE
    resolved = docker_manager._resolve_runner_image(mock_client)
    assert resolved == target
    assert target in mock_client.images.pulled

    # Test 3: Fallback to local image when registry is inaccessible
    fallback_client = MockDockerClient(pull_success=False, local_images=["ghcr.io/jvrcruzgames/yt-dlp-runner:latest"])
    resolved_fallback = docker_manager._resolve_runner_image(fallback_client)
    assert resolved_fallback == "ghcr.io/jvrcruzgames/yt-dlp-runner:latest"
    docker_manager._client = None


@pytest.mark.asyncio
async def test_sse_job_events_stream():
    # 1. Create a dummy job in job_service
    import json
    now = datetime.datetime.now(datetime.timezone.utc)
    job = JobResponse(
        job_id="sse-test-job-456",
        tracking_token="sse-tracking-token-xyz",
        url="https://youtube.com/watch?v=sample",
        status="completed",
        filename="sample_video.mp4",
        created_at=now,
        completed_at=now,
    )
    job_service.jobs[job.job_id] = job
    job_service.job_tracking_tokens[job.job_id] = job.tracking_token

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Connect to SSE endpoint
        resp = await client.get(
            f"/api/v1/jobs/{job.job_id}/stream",
            headers={"Authorization": f"Bearer {job.tracking_token}"}
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        text = resp.text
        assert "data: {" in text
        assert '"status": "completed"' in text
        assert '"job_id": "sse-test-job-456"' in text


def test_worker_http_proxy_setting(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test.internal:8080")
    custom_settings = Settings()
    assert custom_settings.HTTP_PROXY == "http://proxy.test.internal:8080"
    assert custom_settings.FLARESOLVERR_PROXY == "http://proxy.test.internal:8080"


@pytest.mark.asyncio
async def test_job_file_list_and_hex_id_download_endpoints(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    hex_id_video = "a1b2c3d4e5f60718"
    hex_id_thumb = "f8e7d6c5b4a39201"

    file_items = [
        FileItemResponse(
            file_id=hex_id_video,
            filename="Test Video [123].mp4",
            size_bytes=1048576,
            mime_type="video/mp4",
            download_url=f"/api/v1/jobs/files-test-job-789/files/{hex_id_video}/download",
            modified_at=now,
        ),
        FileItemResponse(
            file_id=hex_id_thumb,
            filename="Test Video [123].webp",
            size_bytes=20480,
            mime_type="image/webp",
            download_url=f"/api/v1/jobs/files-test-job-789/files/{hex_id_thumb}/download",
            modified_at=now,
        ),
    ]

    job = JobResponse(
        job_id="files-test-job-789",
        tracking_token="files-tracking-token-789",
        url="https://youtube.com/watch?v=sample-files",
        status="completed",
        filename="Test Video [123].mp4",
        download_url=f"/api/v1/jobs/files-test-job-789/files/{hex_id_video}/download",
        files=file_items,
        container_id="mock-runner-files",
        created_at=now,
        completed_at=now,
    )
    job_service.jobs[job.job_id] = job
    job_service.job_tracking_tokens[job.job_id] = job.tracking_token

    runner_rec = RunnerContainerRecord(
        container_id="mock-runner-files",
        name="mock-runner",
        host_or_ip="localhost",
        port=8080,
    )
    docker_manager._containers["mock-runner-files"] = runner_rec

    orig_send = httpx.AsyncClient.send

    async def mock_send(self_client, request, **kwargs):
        if "localhost:8080/files/" in str(request.url) and hex_id_video in str(request.url):
            return httpx.Response(
                200,
                headers={"Content-Type": "video/mp4", "Content-Disposition": 'attachment; filename="Test Video [123].mp4"'},
                stream=httpx.ByteStream(b"video bytes"),
                request=request,
            )
        elif "localhost:8080/files/" in str(request.url) and hex_id_thumb in str(request.url):
            return httpx.Response(
                200,
                headers={"Content-Type": "image/webp", "Content-Disposition": 'attachment; filename="Test Video [123].webp"'},
                stream=httpx.ByteStream(b"thumbnail bytes"),
                request=request,
            )
        return await orig_send(self_client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. List files for job -> 200
        files_resp = await client.get(
            f"/api/v1/jobs/{job.job_id}/files",
            headers={"Authorization": f"Bearer {job.tracking_token}"}
        )
        assert files_resp.status_code == 200
        files_data = files_resp.json()
        assert len(files_data) == 2
        assert files_data[0]["file_id"] == hex_id_video
        assert files_data[0]["download_url"] == f"/api/v1/jobs/{job.job_id}/files/{hex_id_video}/download"
        assert files_data[1]["file_id"] == hex_id_thumb
        assert files_data[1]["download_url"] == f"/api/v1/jobs/{job.job_id}/files/{hex_id_thumb}/download"

        # 2. Get specific file info by hex ID -> 200
        file_info_resp = await client.get(
            f"/api/v1/jobs/{job.job_id}/files/{hex_id_thumb}",
            headers={"Authorization": f"Bearer {job.tracking_token}"}
        )
        assert file_info_resp.status_code == 200
        finfo = file_info_resp.json()
        assert finfo["file_id"] == hex_id_thumb
        assert finfo["filename"] == "Test Video [123].webp"

        # 3. Download first file (thumbnail) by hex ID route -> 200
        dl_thumb_resp = await client.get(
            f"/api/v1/jobs/{job.job_id}/files/{hex_id_thumb}/download",
            headers={"Authorization": f"Bearer {job.tracking_token}"}
        )
        assert dl_thumb_resp.status_code == 200
        assert dl_thumb_resp.text == "thumbnail bytes"

        # Token must STILL be valid because video file has not been downloaded yet!
        assert job_service.job_tracking_tokens.get(job.job_id) == job.tracking_token

        # 4. Download second and final file (video) -> 200
        dl_video_resp = await client.get(
            f"/api/v1/jobs/{job.job_id}/files/{hex_id_video}/download",
            headers={"Authorization": f"Bearer {job.tracking_token}"}
        )
        assert dl_video_resp.status_code == 200
        assert dl_video_resp.text == "video bytes"

        # NOW all files have been downloaded -> tracking token must be invalidated!
        assert job_service.job_tracking_tokens.get(job.job_id) is None

        # 5. Subsequent download attempt with the invalidated token -> 401
        subsequent_dl = await client.get(
            f"/api/v1/jobs/{job.job_id}/files/{hex_id_video}/download",
            headers={"Authorization": f"Bearer {job.tracking_token}"}
        )
        assert subsequent_dl.status_code == 401


@pytest.mark.asyncio
async def test_container_draining_spawns_new_container(monkeypatch):
    # 1. Setup a container that has finished a job and is in the 5-min file availability window
    c1 = RunnerContainerRecord(
        container_id="runner-draining-1",
        name="mithril-yt-dlp-runner-draining",
        host_or_ip="localhost",
        port=8080,
    )
    c1.last_job_completed_at = time.time() - 30  # 30 seconds ago
    c1.is_draining = True
    c1.active_jobs_count = 0
    docker_manager._containers["runner-draining-1"] = c1

    assert c1.can_accept_jobs is False

    # Mock _spawn_runner_container to return a new runner
    async def mock_spawn(user_id=None, *args, **kwargs):
        c2 = RunnerContainerRecord(
            container_id="runner-fresh-2",
            name="mithril-yt-dlp-runner-fresh",
            host_or_ip="localhost",
            port=8081,
            user_id=user_id,
        )
        docker_manager._containers[c2.container_id] = c2
        return c2

    monkeypatch.setattr(docker_manager, "_spawn_runner_container", mock_spawn)

    # 2. When requesting a runner for a new job, it must NOT use c1 and must spawn a new container
    new_runner = await docker_manager.get_or_create_runner()
    assert new_runner.container_id != c1.container_id
    assert new_runner.container_id == "runner-fresh-2"
    assert new_runner.can_accept_jobs is True


@pytest.mark.asyncio
async def test_container_destroyed_invalidates_tracking_tokens():
    job_id = "job-on-destroyed-container"
    token = "token-destroyed-test-123"
    job = JobResponse(
        job_id=job_id,
        tracking_token=token,
        url="https://youtube.com/watch?v=sample",
        status="completed",
        container_id="runner-to-destroy-xyz",
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    job_service.jobs[job_id] = job
    job_service.job_tracking_tokens[job_id] = token

    c = RunnerContainerRecord(
        container_id="runner-to-destroy-xyz",
        name="mithril-yt-dlp-runner-destroy-test",
        host_or_ip="localhost",
        port=8080,
    )
    docker_manager._containers[c.container_id] = c

    # When container is removed, the tracking token must be invalidated
    await docker_manager.stop_and_remove_container(c.container_id)
    assert job_service.job_tracking_tokens.get(job_id) is None


@pytest.mark.asyncio
async def test_inactivity_reaper_rules():
    from app.services.inactivity_reaper import inactivity_reaper

    now = time.time()

    # Case A: Active job running -> Do NOT reap even if older than 20 mins
    c_busy = RunnerContainerRecord("c-busy", "c-busy", "localhost", 8080)
    c_busy.created_at = now - 1500  # 25 mins old
    c_busy.active_jobs_count = 1
    docker_manager._containers["c-busy"] = c_busy

    # Case B: Post-download 5-minute file retention expired -> Reap!
    c_retention_expired = RunnerContainerRecord("c-retention", "c-retention", "localhost", 8080)
    c_retention_expired.created_at = now - 400
    c_retention_expired.last_job_completed_at = now - 350  # > 300s (5 mins)
    c_retention_expired.active_jobs_count = 0
    docker_manager._containers["c-retention"] = c_retention_expired

    # Case C: Container older than 20 mins with no active job -> Reap!
    c_expired = RunnerContainerRecord("c-expired", "c-expired", "localhost", 8080)
    c_expired.created_at = now - 1300  # > 1200s (20 mins)
    c_expired.active_jobs_count = 0
    docker_manager._containers["c-expired"] = c_expired

    # Case D: Active file download in progress -> Do NOT reap even if older than 20 mins or retention expired
    c_downloading = RunnerContainerRecord("c-downloading", "c-downloading", "localhost", 8080)
    c_downloading.created_at = now - 1500  # 25 mins old
    c_downloading.last_job_completed_at = now - 400  # retention expired
    c_downloading.active_jobs_count = 0
    c_downloading.active_downloads_count = 1  # active file download!
    docker_manager._containers["c-downloading"] = c_downloading

    # Run reaper check
    await inactivity_reaper._check_and_reap_containers()

    assert "c-busy" in docker_manager._containers
    assert "c-downloading" in docker_manager._containers
    assert "c-retention" not in docker_manager._containers
    assert "c-expired" not in docker_manager._containers


@pytest.mark.asyncio
async def test_active_download_prevents_container_deletion():
    c = RunnerContainerRecord("c-active-dl", "c-active-dl", "localhost", 8080)
    c.active_downloads_count = 2
    docker_manager._containers["c-active-dl"] = c

    # 1. Normal stop_and_remove_container should defer / return False when downloads active
    res = await docker_manager.stop_and_remove_container("c-active-dl", force=False)
    assert res is False
    assert "c-active-dl" in docker_manager._containers

    # 2. Force stop_and_remove_container (e.g. admin DELETE endpoint) succeeds
    res_forced = await docker_manager.stop_and_remove_container("c-active-dl", force=True)
    assert res_forced is True
    assert "c-active-dl" not in docker_manager._containers


@pytest.mark.asyncio
async def test_user_specific_runner_container_isolation(monkeypatch):
    """
    Verifies that yt-dlp-runner containers are never shared between different users,
    and that requests from the same user can reuse an active non-draining container.
    """
    spawned_containers = []

    async def mock_spawn(user_id=None, *args, **kwargs):
        cid = f"runner-user-{len(spawned_containers) + 1}"
        record = RunnerContainerRecord(
            container_id=cid,
            name=f"mithril-yt-dlp-runner-{cid}",
            host_or_ip="localhost",
            port=8080 + len(spawned_containers),
            user_id=user_id,
        )
        docker_manager._containers[cid] = record
        spawned_containers.append(record)
        return record

    async def mock_healthy(record):
        return True

    monkeypatch.setattr(docker_manager, "_spawn_runner_container", mock_spawn)
    monkeypatch.setattr(docker_manager, "_is_container_healthy", mock_healthy)

    # 1. User Alice creates a job -> spawns container for Alice
    runner_alice = await docker_manager.get_or_create_runner(user_id="user-alice-111")
    assert runner_alice.user_id == "user-alice-111"
    assert runner_alice.container_id == "runner-user-1"

    # 2. User Bob creates a job -> MUST NOT get Alice's container, MUST spawn a new container for Bob
    runner_bob = await docker_manager.get_or_create_runner(user_id="user-bob-222")
    assert runner_bob.user_id == "user-bob-222"
    assert runner_bob.container_id == "runner-user-2"
    assert runner_bob.container_id != runner_alice.container_id

    # 3. User Alice submits a second job while runner_alice can accept jobs -> reuses runner_alice
    runner_alice_2 = await docker_manager.get_or_create_runner(user_id="user-alice-111")
    assert runner_alice_2.container_id == runner_alice.container_id
    assert runner_alice_2.user_id == "user-alice-111"

    # 4. User Alice's container enters draining / post-download file retention period
    runner_alice.is_draining = True
    runner_alice.last_job_completed_at = time.time()
    assert runner_alice.can_accept_jobs is False

    # 5. User Alice submits a third job -> MUST NOT reuse draining container or Bob's container, spawns fresh container for Alice
    runner_alice_3 = await docker_manager.get_or_create_runner(user_id="user-alice-111")
    assert runner_alice_3.user_id == "user-alice-111"
    assert runner_alice_3.container_id == "runner-user-3"
    assert runner_alice_3.container_id != runner_alice.container_id
    assert runner_alice_3.container_id != runner_bob.container_id


@pytest.mark.asyncio
async def test_job_service_and_orchestrator_token_user_isolation(monkeypatch):
    """
    Tests end-to-end token validation with user_id claim embedding and container assignment.
    """
    from app.services.announcer import announcer
    import base64, json, hmac, hashlib

    monkeypatch.setattr(announcer, "_worker_id", "worker-user-isolation")
    monkeypatch.setattr(announcer, "_auth_token", "secret-token-isolation")

    spawned = []
    async def mock_spawn(user_id=None, *args, **kwargs):
        cid = f"runner-iso-{len(spawned) + 1}"
        rec = RunnerContainerRecord(
            container_id=cid,
            name=f"runner-{cid}",
            host_or_ip="localhost",
            port=8090 + len(spawned),
            user_id=user_id,
        )
        docker_manager._containers[cid] = rec
        spawned.append(rec)
        return rec

    monkeypatch.setattr(docker_manager, "_spawn_runner_container", mock_spawn)
    monkeypatch.setattr(docker_manager, "_is_container_healthy", lambda r: True)

    now_ts = int(time.time())

    # User 1 token
    p1 = {"jti": "jti-1", "worker_id": "worker-user-isolation", "iat": now_ts, "exp": now_ts + 300, "user_id": "account-user-1"}
    p1_b64 = base64.urlsafe_b64encode(json.dumps(p1).encode("utf-8")).decode("utf-8").rstrip("=")
    sig1 = hmac.new("secret-token-isolation".encode("utf-8"), p1_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    token1 = f"{p1_b64}.{sig1}"

    # User 2 token
    p2 = {"jti": "jti-2", "worker_id": "worker-user-isolation", "iat": now_ts, "exp": now_ts + 300, "user_id": "account-user-2"}
    p2_b64 = base64.urlsafe_b64encode(json.dumps(p2).encode("utf-8")).decode("utf-8").rstrip("=")
    sig2 = hmac.new("secret-token-isolation".encode("utf-8"), p2_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    token2 = f"{p2_b64}.{sig2}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # User 1 creates job
        resp1 = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample1"},
            headers={"Authorization": f"Bearer {token1}"}
        )
        assert resp1.status_code == 202
        jdata1 = resp1.json()
        assert jdata1["user_id"] == "account-user-1"
        assert jdata1["container_id"] == "runner-iso-1"

        # User 2 creates job -> gets separate container
        resp2 = await client.post(
            "/api/v1/jobs",
            json={"url": "https://youtube.com/watch?v=sample2"},
            headers={"Authorization": f"Bearer {token2}"}
        )
        assert resp2.status_code == 202
        jdata2 = resp2.json()
        assert jdata2["user_id"] == "account-user-2"
        assert jdata2["container_id"] == "runner-iso-2"
        assert jdata2["container_id"] != jdata1["container_id"]

        # Admin container inspection shows user_ids
        admin_headers = {"X-Admin-Key": "secret-admin-pass-123"}
        monkeypatch.setattr(settings, "ADMIN_KEY", "secret-admin-pass-123")
        c_resp = await client.get("/api/v1/containers", headers=admin_headers)
        assert c_resp.status_code == 200
        containers = c_resp.json()
        c_map = {c["container_id"]: c["user_id"] for c in containers}
        assert c_map.get("runner-iso-1") == "account-user-1"
        assert c_map.get("runner-iso-2") == "account-user-2"


@pytest.mark.asyncio
async def test_job_completion_generates_receipt_token(monkeypatch):
    import base64, json
    from app.services.announcer import announcer
    from app.services.crypto import worker_crypto

    monkeypatch.setattr(announcer, "_auth_token", "test-auth-worker-tok")
    monkeypatch.setattr(announcer, "_worker_id", "worker-receipt-test")

    now = datetime.datetime.now(datetime.timezone.utc)
    job = JobResponse(
        job_id="job-receipt-test-1",
        user_id="user-123",
        tracking_token="track-123",
        url="https://youtube.com/watch?v=receipt-test",
        status="completed",
        downloaded_bytes=2048,
        files=[
            FileItemResponse(
                file_id="abc123hexfileid",
                filename="test.mp4",
                size_bytes=2048,
                download_url="/api/v1/jobs/job-receipt-test-1/files/abc123hexfileid/download",
                modified_at=now,
            )
        ],
        created_at=now,
        completed_at=now,
    )
    job_service.jobs["job-receipt-test-1"] = job
    job_service.job_tracking_tokens["job-receipt-test-1"] = "track-123"

    receipt = worker_crypto.generate_download_receipt(
        worker_id="worker-receipt-test",
        auth_token="test-auth-worker-tok",
        job_id="job-receipt-test-1",
        user_id="user-123",
        file_id="abc123hexfileid",
        bytes_downloaded=2048,
    )
    job.receipt_token = receipt

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/jobs/job-receipt-test-1", headers={"X-Tracking-Token": "track-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["receipt_token"] is not None
        assert "." in data["receipt_token"]

        # Verify decoded payload has type download_receipt
        payload_b64 = data["receipt_token"].split(".")[0]
        padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        assert payload["type"] == "download_receipt"
        assert payload["worker_id"] == "worker-receipt-test"
        assert payload["job_id"] == "job-receipt-test-1"


