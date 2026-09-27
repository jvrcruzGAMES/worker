import os
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.main import app


@pytest.mark.asyncio
async def test_supervisor_health_and_activity():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

        resp = await client.get("/activity")
        assert resp.status_code == 200
        act = resp.json()
        assert act["is_busy"] is False
        assert act["idle_seconds"] >= 0


@pytest.mark.asyncio
async def test_cookie_directory_isolation(tmp_path):
    # Verify cookies dir and downloads dir are distinct
    assert settings.COOKIES_DIR != settings.DOWNLOADS_DIR
    assert not settings.COOKIES_DIR.startswith(settings.DOWNLOADS_DIR)


@pytest.mark.asyncio
async def test_file_listing_and_streaming():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create a test file in downloads directory
        test_file = Path(settings.DOWNLOADS_DIR) / "test_media.mp4"
        test_file.write_bytes(b"dummy video data")

        try:
            resp = await client.get("/files")
            assert resp.status_code == 200
            files = [f["filename"] for f in resp.json()]
            assert "test_media.mp4" in files

            # Stream the file
            stream_resp = await client.get("/files/test_media.mp4")
            assert stream_resp.status_code == 200
            assert stream_resp.content == b"dummy video data"
        finally:
            if test_file.exists():
                test_file.unlink()
