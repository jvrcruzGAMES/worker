import os
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from app.args_sanitizer import sanitize_yt_dlp_args
from app.config import settings
from app.main import app
from app.plugins import normalize_plugin_spec


def test_sanitize_yt_dlp_args():
    client_args = [
        "--proxy", "http://client-proxy:8080",
        "--write-subs",
        "--sub-lang", "en",
        "--outtmpl", "%(title)s.mp4",
        "-o", "custom.mp4",
        "--proxy=http://client-proxy-2:8080",
        "--write-thumbnail",
        "-f", "best",
        "--cookies", "/tmp/client_cookie.txt",
        "--exec", "rm -rf /",
        "--dump-json",
    ]

    sanitized, stripped = sanitize_yt_dlp_args(client_args)

    # Allowed arguments should be preserved
    assert "--write-subs" in sanitized
    assert "--sub-lang" in sanitized
    assert "en" in sanitized
    assert "--write-thumbnail" in sanitized
    assert "-f" in sanitized
    assert "best" in sanitized

    # Disallowed arguments must be stripped
    assert "--proxy" not in sanitized
    assert "http://client-proxy:8080" not in sanitized
    assert "--proxy=http://client-proxy-2:8080" not in sanitized
    assert "--outtmpl" not in sanitized
    assert "-o" not in sanitized
    assert "custom.mp4" not in sanitized
    assert "--cookies" not in sanitized
    assert "/tmp/client_cookie.txt" not in sanitized
    assert "--exec" not in sanitized
    assert "--dump-json" not in sanitized

    assert len(stripped) > 0


def test_normalize_plugin_spec():
    # 1. Standard package names should not be altered
    assert normalize_plugin_spec("bgutil-ytdlp-pot-provider") == "bgutil-ytdlp-pot-provider"
    assert normalize_plugin_spec("yt-dlp-ejs>=0.8.0") == "yt-dlp-ejs>=0.8.0"

    # 2. HTTPS Git URLs
    assert normalize_plugin_spec("https://github.com/user/plugin-repo") == "git+https://github.com/user/plugin-repo"
    assert normalize_plugin_spec("https://github.com/user/plugin-repo.git") == "git+https://github.com/user/plugin-repo.git"
    assert normalize_plugin_spec("https://gitlab.com/user/plugin-repo.git@v1.0.0") == "git+https://gitlab.com/user/plugin-repo.git@v1.0.0"

    # 3. HTTP Git URLs
    assert normalize_plugin_spec("http://git.internal/repo.git") == "git+http://git.internal/repo.git"

    # 4. SSH Git URLs
    assert normalize_plugin_spec("git@github.com:user/plugin-repo.git") == "git+ssh://git@github.com/user/plugin-repo.git"

    # 5. Shorthand forge domain without protocol
    assert normalize_plugin_spec("github.com/user/plugin-repo") == "git+https://github.com/user/plugin-repo"
    assert normalize_plugin_spec("gitlab.com/user/plugin-repo") == "git+https://gitlab.com/user/plugin-repo"

    # 6. Already formatted VCS URLs
    assert normalize_plugin_spec("git+https://github.com/user/repo") == "git+https://github.com/user/repo"
    assert normalize_plugin_spec("git+ssh://git@github.com/user/repo.git") == "git+ssh://git@github.com/user/repo.git"


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
async def test_file_listing_and_hex_streaming():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create two test files in downloads directory (e.g. video + subtitle)
        test_video = Path(settings.DOWNLOADS_DIR) / "test_video.mp4"
        test_sub = Path(settings.DOWNLOADS_DIR) / "test_video.en.vtt"
        test_video.write_bytes(b"dummy video data")
        test_sub.write_bytes(b"WEBVTT subtitle data")

        try:
            resp = await client.get("/files")
            assert resp.status_code == 200
            files = resp.json()
            assert len(files) >= 2

            video_item = [f for f in files if f["filename"] == "test_video.mp4"][0]
            assert "file_id" in video_item
            assert len(video_item["file_id"]) == 16
            assert f"/files/{video_item['file_id']}/download" == video_item["download_url"]

            sub_item = [f for f in files if f["filename"] == "test_video.en.vtt"][0]
            assert "file_id" in sub_item
            assert sub_item["file_id"] != video_item["file_id"]

            # Stream video using hex ID
            stream_video = await client.get(f"/files/{video_item['file_id']}/download")
            assert stream_video.status_code == 200
            assert stream_video.content == b"dummy video data"

            # Stream subtitle using hex ID
            stream_sub = await client.get(f"/files/{sub_item['file_id']}/download")
            assert stream_sub.status_code == 200
            assert stream_sub.content == b"WEBVTT subtitle data"
        finally:
            if test_video.exists():
                test_video.unlink()
            if test_sub.exists():
                test_sub.unlink()


def test_pot_provider_extractor_args():
    import yt_dlp
    pot_url = "http://bgutil-server:4416"
    ydl_opts = {}
    extractor_args = ydl_opts.setdefault("extractor_args", {})
    for ext_name in ["youtubepot-bgutilhttp", "youtubepot-bgutil", "youtubepot"]:
        ext_dict = extractor_args.setdefault(ext_name, {})
        ext_dict["base_url"] = [pot_url]
    yt_dict = extractor_args.setdefault("youtube", {})
    yt_dict["getpot_bgutil_baseurl"] = [pot_url]

    assert ydl_opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == ["http://bgutil-server:4416"]
    assert ydl_opts["extractor_args"]["youtube"]["getpot_bgutil_baseurl"] == ["http://bgutil-server:4416"]

