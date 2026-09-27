# yt-dlp Runner Image & HTTP Supervisor

A self-contained Docker container equipped with Python, FFmpeg, Deno, yt-dlp, and a FastAPI HTTP supervisor.

## Bundled Dependencies & Plugins

- **Core**: Python 3.11, FFmpeg, Deno JS runtime, `curl_cffi` (impersonation client)
- **yt-dlp Extensions**:
  - `bgutil-ytdlp-pot-provider`: PO token provider for YouTube BOT protection bypassing.
  - `yt-dlp-ejs`: Embedded JavaScript challenge solver.
  - `yt-dlp`: Latest media extractor engine.
- **Supervisor**: FastAPI HTTP API for asynchronous job dispatch, isolated cookie injection, plugin installation, and file streaming.

## Supervisor Capabilities

1. **Plugin Installation**:
   - `POST /plugins/install`
   - Installs yt-dlp plugin packages or extractor extensions dynamically via `pip`.
2. **yt-dlp Downloads with Isolated Cookie Support**:
   - `POST /download`
   - Accepts download URL, format options, custom arguments, and raw cookie content.
   - Saves cookies strictly to an isolated directory (`/app/cookies/`) separated from the visible download directory (`/app/downloads/`).
3. **File Streaming**:
   - `GET /files`: List files in `/app/downloads`.
   - `GET /files/{filename}`: Stream finished audio/video files.
4. **Activity Monitoring**:
   - `GET /activity`: Reports active task count and idle duration (used by Worker for 20-minute inactivity auto-teardown).
