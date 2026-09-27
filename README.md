# Mithril Worker

The Worker is a self-announcing orchestrator node that manages dynamic `yt-dlp` execution containers via an asynchronous jobs system, installs Python plugins, isolates cookie credentials, streams finished downloads, and automatically destroys child containers after 20 minutes of inactivity.

## Features

- **Orchestrator Managed ID & Worker Token Authentication**:
  - Automatically announces itself on startup to the orchestrator.
  - The orchestrator generates and manages the `worker_id` and assigns a worker authorization token.
  - Subsequent heartbeat signals and unregister calls automatically pass `Authorization: Worker <token>`.
- **Dynamic Child Container Lifecycle**:
  - Automatically spins up child `yt-dlp-runner` containers (Python + FFmpeg + yt-dlp + HTTP supervisor) on demand.
  - Monitors activity and automatically stops/removes containers after **20 minutes of inactivity** (configurable via `INACTIVITY_TIMEOUT_SECONDS=1200`).
- **Isolated Cookie Handling**:
  - Securely receives raw Netscape cookies from clients and writes them to `/app/cookies/`, completely isolated from the visible download folder (`/app/downloads/`).
- **yt-dlp Python Plugins**:
  - `POST /api/v1/plugins/install` allows dynamic pip installation of custom yt-dlp plugin packages inside the child runner container.
- **Job & Download Management**:
  - `POST /api/v1/jobs`: Create download job.
  - `GET /api/v1/jobs/{job_id}`: Track progress (% completed, download speed, ETA, logs).
  - `POST /api/v1/jobs/{job_id}/cancel`: Cancel active download.
  - `GET /api/v1/jobs/{job_id}/download`: Stream completed media file.
  - `GET /api/v1/files`: List downloaded media files.
  - `GET /api/v1/files/{filename}`: Stream file.
  - `GET /api/v1/containers`: Inspect running child containers and inactivity countdowns.

## API Overview

| Method | Route | Description |
|---|---|---|
| `GET` | `/health` | Worker health status, active jobs, and runner count |
| `GET` | `/info` | Worker metadata, base URL, capabilities |
| `POST` | `/api/v1/jobs` | Submit a yt-dlp download job |
| `GET` | `/api/v1/jobs` | List all jobs |
| `GET` | `/api/v1/jobs/{job_id}` | Get status & logs of job |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Cancel active job |
| `GET` | `/api/v1/jobs/{job_id}/download` | Stream media file for completed job |
| `POST` | `/api/v1/plugins/install` | Install Python yt-dlp plugin |
| `GET` | `/api/v1/containers` | List active runner containers & idle timers |
| `DELETE`| `/api/v1/containers/{container_id}` | Terminate runner container manually |
| `GET` | `/api/v1/files` | List files in download storage |
| `GET` | `/api/v1/files/{filename}` | Stream file from storage |
