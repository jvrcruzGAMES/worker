# Mithril Worker

The Worker service self-announces its presence, base URL, and health endpoint to the Orchestrator, provides health check endpoints, and emits periodic heartbeats to maintain its active status in the registry.

## Key Features

- **Automatic Announcement**:
  - Automatically announces itself (`worker_id`, `base_url`, `health_endpoint`, `tags`, `metadata`) to the Orchestrator on startup with retry backoff.
- **Heartbeat Loop**:
  - Emits background heartbeat pings every 10s.
- **Graceful Shutdown**:
  - Automatically deregisters from Orchestrator upon container stop/SIGTERM.
- **Health & Info Endpoints**:
  - `GET /health`: Health status & uptime.
  - `GET /info` or `GET /api/v1/info`: Worker identity and discovery configuration.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WORKER_ID` | Auto-generated | Unique identifier for worker |
| `WORKER_NAME` | `Mithril Worker` | Human readable worker name |
| `WORKER_PORT` | `8001` | Worker listening port |
| `WORKER_BASE_URL` | `http://localhost:8001` | Public or cluster reachable base URL |
| `HEALTH_ENDPOINT` | `/health` | Path for health checks |
| `ORCHESTRATOR_URL`| `http://localhost:8000` | URL of the Orchestrator |
| `HEARTBEAT_INTERVAL_SECONDS` | `10` | Frequency of heartbeats |
| `AUTO_ANNOUNCE` | `true` | Whether to self-register with orchestrator |

## Quick Start (Docker)

```bash
docker compose up --build
```
