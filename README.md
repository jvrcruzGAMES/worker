# Mithril Worker

The **Mithril Worker** is an orchestrator node designed to handle on-demand media downloads via `yt-dlp`. It dynamically spins up isolated runner containers, handles challenge resolution via FlareSolverr, protects credentials (cookies), streams completed downloads, and shuts down idle runner containers after 20 minutes of inactivity.

The official orchestrator for Mithril is hosted at: **`https://dl.jvrcruz.games/`**

---

## Setup Instructions

### 1. Prerequisites & Container Dependencies

To run a Mithril Worker, the following services and permissions are required:
- **Docker Engine & Docker Compose**: Installed and running on the host system.
- **Docker Socket Access** (`/var/run/docker.sock`): The worker requires access to the Docker daemon to dynamically spawn, monitor, and clean up isolated child runner containers.
- **Network Access**: The worker must be reachable externally via HTTPS (e.g. via Nginx, Caddy, or Cloudflare Tunnel) and have outbound access to communicate with the Mithril Orchestrator (`https://dl.jvrcruz.games/`).
- **Required Sidecar Services (in same network)**:
  - **`bgutil-provider`** (`brainicism/bgutil-ytdlp-pot-provider:latest` on port `4416`): Generates Proof-of-Origin (PO) tokens to bypass YouTube's "Sign in to confirm you're not a bot" detection.
  - **`flaresolverr`** (`flaresolverr/flaresolverr:latest` on port `8191`): Solves Cloudflare and anti-bot challenges for protected media sources.
  - **`yt-dlp-runner-base`** (`mithril-yt-dlp-runner:latest`): Pre-built base image for dynamic yt-dlp child containers.

---

### 2. Environment Configuration

Create a `.env` file or provide environment variables to the worker container:

```env
# Official Mithril Orchestrator
ORCHESTRATOR_URL=https://dl.jvrcruz.games

# Public HTTPS URL of this Worker (Orchestrator strictly requires HTTPS and checks reachability)
WORKER_BASE_URL=https://worker.yourdomain.com
WORKER_NAME=My Community Mithril Worker
WORKER_PORT=8001

# Worker Admin Key (Required for managing containers and listing all jobs)
ADMIN_KEY=your-secure-admin-secret-key

# Sidecar & External Provider Configuration
FLARESOLVERR_URL=http://flaresolverr:8191/v1
BGUTIL_POT_PROVIDER_URL=http://bgutil-provider:4416

# Docker and Runner Container Configuration
RUNNER_IMAGE=mithril-yt-dlp-runner:latest
DOCKER_NETWORK=mithril-network
SHARED_DOWNLOADS_VOLUME=mithril-downloads
SHARED_COOKIES_VOLUME=mithril-cookies

# Inactivity Timeout for child containers (20 minutes default)
INACTIVITY_TIMEOUT_SECONDS=1200

# Automated Orchestrator Announcement & Heartbeats
AUTO_ANNOUNCE=true
HEARTBEAT_INTERVAL_SECONDS=10
INTEGRITY_CHECK_ENABLED=true
```

> [!IMPORTANT]
> The Orchestrator requires all workers to provide an **HTTPS `WORKER_BASE_URL`** (e.g. `https://worker.yourdomain.com` via Nginx, Caddy, Cloudflare Tunnel, or Traefik) and performs an active reachability check on `/health` before accepting registration.

---

### 3. Docker Compose Example

```yaml
version: "3.8"

services:
  # YouTube POT Token Provider required by yt-dlp child runner to bypass bot detection
  bgutil-provider:
    image: brainicism/bgutil-ytdlp-pot-provider:latest
    container_name: mithril-worker-bgutil-provider
    restart: unless-stopped
    init: true
    ports:
      - "4416:4416"
    networks:
      - mithril-net

  # FlareSolverr service instance required by the worker and yt-dlp runner
  flaresolverr:
    image: flaresolverr/flaresolverr:latest
    container_name: mithril-worker-flaresolverr
    restart: unless-stopped
    ports:
      - "8191:8191"
    environment:
      - LOG_LEVEL=info
      - LOG_HTML=false
      - CAPTCHA_SOLVER=none
      - PROXY=${FLARESOLVERR_PROXY:-}
    networks:
      - mithril-net

  # Pre-build runner image so worker can spin up child container instances dynamically
  yt-dlp-runner-base:
    build:
      context: ./yt-dlp-runner
      dockerfile: Dockerfile
    image: mithril-yt-dlp-runner:latest
    restart: "no"
    entrypoint: ["echo", "yt-dlp runner base image built successfully."]

  worker:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: mithril-worker
    restart: unless-stopped
    ports:
      - "8001:8001"
    environment:
      - ORCHESTRATOR_URL=https://dl.jvrcruz.games
      - WORKER_BASE_URL=https://worker.yourdomain.com
      - ADMIN_KEY=your-secure-admin-secret-key
      - DOCKER_NETWORK=mithril-network
      - RUNNER_IMAGE=mithril-yt-dlp-runner:latest
      - FLARESOLVERR_URL=http://flaresolverr:8191/v1
      - BGUTIL_POT_PROVIDER_URL=http://bgutil-provider:4416
      - SHARED_DOWNLOADS_VOLUME=mithril-downloads
      - SHARED_COOKIES_VOLUME=mithril-cookies
      - INACTIVITY_TIMEOUT_SECONDS=1200
      - AUTO_ANNOUNCE=true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - mithril-downloads:/app/downloads
      - mithril-cookies:/app/cookies
    depends_on:
      - flaresolverr
      - bgutil-provider
      - yt-dlp-runner-base
    networks:
      - mithril-net

volumes:
  mithril-downloads:
    name: mithril-downloads
  mithril-cookies:
    name: mithril-cookies

networks:
  mithril-net:
    name: mithril-network
    driver: bridge
```

Run the worker and all dependencies:
```bash
docker compose up -d --build
```

---

## Authentication & Handshake Workflow

Mithril uses a secure three-tier handshake:

1. **Discovery**:
   - The client queries the Orchestrator (`GET /api/v1/workers/discover`) with Mithril+ authentication.
2. **Worker Selection & Single-Use Token**:
   - The client decides which worker to use and sends its choice to the Orchestrator (`POST /api/v1/workers/select`).
   - The Orchestrator logs worker selection metrics (monthly and all-time counts) and requests a single-use auth token from the chosen worker.
   - The Orchestrator forwards this single-use token back to the client.
3. **Job Creation**:
   - The client submits the download job (`POST /api/v1/jobs`) to the worker, providing the single-use token in the `Authorization: Bearer <single_use_token>` header.
   - The worker validates and **immediately consumes** the single-use token, generates a unique **Job Tracking Token**, and starts the download.
4. **Tracking & Download Stream**:
   - The client polls job progress (`GET /api/v1/jobs/{job_id}`) or cancels (`POST /api/v1/jobs/{job_id}/cancel`) using the tracking token (`Authorization: Bearer <tracking_token>`).
   - Once completed, the client streams the file via `GET /api/v1/jobs/{job_id}/download`.
   - **Upon stream completion, the tracking token is automatically invalidated.**

---

## API Endpoints Overview

### Public & Client Endpoints (Token Authenticated)

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Worker health check and active container counts |
| `GET` | `/info` | None | Worker metadata, version, and capabilities |
| `POST` | `/api/v1/jobs` | Single-Use Token | Create a download job (returns tracking token) |
| `GET` | `/api/v1/jobs/{job_id}` | Tracking Token / Admin | Poll download progress, status, and file metadata |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Tracking Token / Admin | Cancel an active download |
| `GET` | `/api/v1/jobs/{job_id}/download` | Tracking Token / Admin | Stream finished download (invalidates tracking token on finish) |

### Orchestrator Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/tokens/single-use` | Worker Token | Issues a single-use token for a selected client |

### Admin Endpoints (Requires `ADMIN_KEY`)

Pass `X-Admin-Key: <ADMIN_KEY>` or `Authorization: Bearer <ADMIN_KEY>`.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/jobs` | `ADMIN_KEY` | List all jobs currently held on this worker |
| `GET` | `/api/v1/containers` | `ADMIN_KEY` | Inspect active runner containers and inactivity countdowns |
| `DELETE` | `/api/v1/containers/{container_id}` | `ADMIN_KEY` | Manually terminate and clean up a runner container |

