# yt-dlp Runner Image & HTTP Supervisor

A self-contained Docker container equipped with Python, FFmpeg, yt-dlp, and a FastAPI HTTP supervisor.

## Capabilities

1. **Plugin Installation**:
   - `POST /plugins/install`
   - Installs yt-dlp plugin packages or extractor extensions dynamically via `pip`.
2. **yt-dlp Downloads with Isolated Cookie Support**:
   - `POST /download`
   - Accepts download URL, format options, custom arguments, and raw cookie content.
   - Saves cookies strictly to an isolated directory (`/app/cookies/`) separated from the visible downloads directory (`/app/downloads/`).
3. **File Streaming**:
   - `GET /files`: List files in `/app/downloads`.
   - `GET /files/{filename}`: Stream finished audio/video files.
4. **Activity Monitoring**:
   - `GET /activity`: Reports active task count and idle duration (used by Worker for 20-minute inactivity auto-teardown).
