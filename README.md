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
- **Container Images (Prebuilt on GitHub Container Registry)**:
  - **`ghcr.io/jvrcruzgames/worker:latest`**: Official, signed prebuilt worker image.
  - **`ghcr.io/jvrcruzgames/yt-dlp-runner:latest`**: Official prebuilt runner image for dynamic yt-dlp child containers.
- **Required Sidecar Services (in same network)**:
  - **`bgutil-provider`** (`brainicism/bgutil-ytdlp-pot-provider:latest` on port `4416`): Generates Proof-of-Origin (PO) tokens to bypass YouTube's "Sign in to confirm you're not a bot" detection.
  - **`flaresolverr`** (`flaresolverr/flaresolverr:latest` on port `8191`): Solves Cloudflare and anti-bot challenges for protected media sources.

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

# Docker Configuration (Worker automatically uses the official hardcoded runner image: ghcr.io/jvrcruzgames/yt-dlp-runner:latest)
DOCKER_NETWORK=mithril-network

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

Deploy the worker using official prebuilt images from GitHub Container Registry:

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
      - "34416:4416"
    networks:
      - mithril-net

  # FlareSolverr service instance required by the worker and yt-dlp runner
  flaresolverr:
    image: flaresolverr/flaresolverr:latest
    container_name: mithril-worker-flaresolverr
    restart: unless-stopped
    ports:
      - "38191:8191"
    environment:
      - LOG_LEVEL=info
      - LOG_HTML=false
      - CAPTCHA_SOLVER=none
      - PROXY=${FLARESOLVERR_PROXY:-}
    networks:
      - mithril-net

  # Official Prebuilt Mithril Worker
  worker:
    image: ghcr.io/jvrcruzgames/worker:latest
    container_name: mithril-worker
    restart: unless-stopped
    ports:
      - "8001:8001"
    environment:
      - ORCHESTRATOR_URL=https://dl.jvrcruz.games
      - WORKER_BASE_URL=https://worker.yourdomain.com
      - ADMIN_KEY=your-secure-admin-secret-key
      - DOCKER_NETWORK=mithril-network
      - FLARESOLVERR_URL=http://flaresolverr:8191/v1
      - BGUTIL_POT_PROVIDER_URL=http://bgutil-provider:4416
      - INACTIVITY_TIMEOUT_SECONDS=1200
      - AUTO_ANNOUNCE=true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      - flaresolverr
      - bgutil-provider
    networks:
      - mithril-net

networks:
  mithril-net:
    name: mithril-network
    driver: bridge
```

Pull the prebuilt runner image and start the worker stack:
```bash
# 1. Pull the prebuilt yt-dlp child runner image
docker pull ghcr.io/jvrcruzgames/yt-dlp-runner:latest

# 2. Launch the worker and sidecar stack
docker compose up -d
```

---

## Authentication & Envelope Encryption Workflow

Mithril uses an end-to-end secure handshake protecting credentials against reverse proxies and rogue intermediaries:

1. **Discovery & Worker Selection (Orchestrator Authority)**:
   - The client queries the Orchestrator (`GET /api/v1/workers/discover`) with Mithril+ authentication.
   - The client selects a worker (`POST /api/v1/workers/select`).
   - The **Orchestrator acts as the sole authority**: it validates the client's Mithril+ license, logs metrics, and **issues a signed, single-use authentication token** bound to the chosen worker (`single_use_token`) along with the worker's X25519 `public_key`.

2. **Application-Layer Envelope Encryption (Reverse-Proxy Immunity)**:
   - To prevent reverse proxies (e.g. Cloudflare, Nginx, or corporate proxies) from snooping on sensitive YouTube cookies or credentials:
   - The client generates an ephemeral X25519 keypair and performs ECDH with the worker's `public_key`.
   - The client derives a 32-byte symmetric key via HKDF-SHA256 and encrypts sensitive fields (`cookie_content`, `custom_args`, `plugins`) using `ChaCha20-Poly1305`.
   - The client submits `encrypted_credentials` inside `POST /api/v1/jobs`. Intermediaries only see encrypted ciphertext.

3. **Job Creation & Token Verification**:
   - The worker verifies the Orchestrator's cryptographic signature on the single-use token and enforces replay protection (`jti`).
   - The worker decrypts `encrypted_credentials` in-memory using its private key and forwards the job to the isolated child runner.
   - The worker issues a **Job Tracking Token** back to the client.

4. **Tracking & Download Stream**:
   - The client polls job progress (`GET /api/v1/jobs/{job_id}`) using the tracking token (`Authorization: Bearer <tracking_token>`).
   - Once completed, the client streams the file via `GET /api/v1/jobs/{job_id}/download`.
   - **Upon stream completion, the tracking token is automatically invalidated.**

---

## API Endpoints Overview

### Public & Client Endpoints (Token Authenticated)

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Worker health check, active container counts, and X25519 `public_key` |
| `GET` | `/info` | None | Worker metadata, version, capabilities, and X25519 `public_key` |
| `POST` | `/api/v1/jobs` | Orchestrator Single-Use Token | Create a download job with plaintext or envelope-encrypted credentials |
| `GET` | `/api/v1/jobs/{job_id}` | Tracking Token / Admin | Poll download progress, status, and file metadata |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Tracking Token / Admin | Cancel an active download |
| `GET` | `/api/v1/jobs/{job_id}/download` | Tracking Token / Admin | Stream finished download (invalidates tracking token on finish) |

### Admin Endpoints (Requires `ADMIN_KEY`)

Pass `X-Admin-Key: <ADMIN_KEY>` or `Authorization: Bearer <ADMIN_KEY>`.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/jobs` | `ADMIN_KEY` | List all jobs currently held on this worker |
| `GET` | `/api/v1/containers` | `ADMIN_KEY` | Inspect active runner containers and inactivity countdowns |
| `DELETE` | `/api/v1/containers/{container_id}` | `ADMIN_KEY` | Manually terminate and clean up a runner container |


