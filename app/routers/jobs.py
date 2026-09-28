import datetime
import hashlib
import mimetypes
import os
from pathlib import Path
import secrets
from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
import httpx

from app.config import settings
from app.schemas import (
    ContainerInfoResponse,
    FileItemResponse,
    JobCreateRequest,
    JobResponse,
    SingleUseTokenResponse,
)
from app.services.docker_manager import docker_manager
from app.services.job_service import job_service

router = APIRouter(prefix="/api/v1", tags=["Jobs & Downloads"])


# ============================================================================
# Security & Authentication Helpers
# ============================================================================

def extract_token_from_request(
    authorization: Optional[str] = None,
    custom_header: Optional[str] = None,
    query_token: Optional[str] = None,
) -> Optional[str]:
    if custom_header:
        return custom_header.strip()
    if query_token:
        return query_token.strip()
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() in ["bearer", "token", "worker"]:
            return parts[1].strip()
        elif len(parts) == 1:
            return parts[0].strip()
    return None


def require_admin_key(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
) -> bool:
    if not settings.ADMIN_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin operations are disabled because ADMIN_KEY is not configured on this worker.",
        )
    candidate = None
    if x_admin_key:
        candidate = x_admin_key.strip()
    elif authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() in ["bearer", "admin"]:
            candidate = parts[1].strip()
        elif len(parts) == 1:
            candidate = parts[0].strip()

    if candidate and secrets.compare_digest(candidate, settings.ADMIN_KEY):
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing admin key.",
    )


def is_admin_authorized(
    x_admin_key: Optional[str] = None,
    authorization: Optional[str] = None,
) -> bool:
    if not settings.ADMIN_KEY:
        return False
    candidate = None
    if x_admin_key:
        candidate = x_admin_key.strip()
    elif authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() in ["bearer", "admin"]:
            candidate = parts[1].strip()
        elif len(parts) == 1:
            candidate = parts[0].strip()
    return bool(candidate and secrets.compare_digest(candidate, settings.ADMIN_KEY))


def require_orchestrator_or_admin(
    authorization: Optional[str] = Header(None),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
) -> bool:
    if is_admin_authorized(x_admin_key, authorization):
        return True

    from app.services.announcer import announcer
    # Verify orchestrator worker auth token
    if announcer.auth_token:
        candidate = None
        if authorization:
            parts = authorization.split()
            if len(parts) == 2 and parts[0].lower() in ["worker", "bearer"]:
                candidate = parts[1].strip()
            elif len(parts) == 1:
                candidate = parts[0].strip()

        if candidate and secrets.compare_digest(candidate, announcer.auth_token):
            return True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized orchestrator request.",
        )
    return True


# ============================================================================
# Single-Use Token Handshake
# ============================================================================

@router.post(
    "/tokens/single-use",
    response_model=SingleUseTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Issue a single-use auth token for job creation",
    description="Called by the orchestrator after a client chooses this worker. Requires orchestrator worker auth token."
)
async def create_single_use_token(
    _: bool = Depends(require_orchestrator_or_admin),
):
    token = job_service.generate_single_use_token(expires_in=300)
    return SingleUseTokenResponse(token=token, expires_in=300)


# ============================================================================
# Jobs Endpoints
# ============================================================================

@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a yt-dlp download job (Single-Use Token Required)",
    description="Client creates a job using the single-use token obtained via Orchestrator handshake. Returns job status and tracking token."
)
async def create_job(
    request: JobCreateRequest,
    authorization: Optional[str] = Header(None),
    x_single_use_token: Optional[str] = Header(None, alias="X-Single-Use-Token"),
    token: Optional[str] = Query(None, description="Single-use token"),
):
    single_use_token = extract_token_from_request(authorization, x_single_use_token, token)
    if not job_service.validate_and_consume_single_use_token(single_use_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or already-used single-use authorization token.",
        )
    return await job_service.create_job(request)


@router.get(
    "/jobs",
    response_model=List[JobResponse],
    dependencies=[Depends(require_admin_key)],
    summary="List all download jobs (Admin Key Required)",
    description="Listing all jobs is restricted to administrators and requires the worker ADMIN_KEY."
)
async def list_jobs():
    return job_service.list_jobs()


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get job status & progress (Tracking Token or Admin Key Required)"
)
async def get_job(
    job_id: str,
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required.",
        )

    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )
    return job


@router.get(
    "/jobs/{job_id}/stream",
    summary="Stream real-time job progress & completion via Server-Sent Events (SSE)"
)
@router.get(
    "/jobs/{job_id}/events",
    summary="Stream real-time job progress & completion via Server-Sent Events (SSE)"
)
async def stream_job_events(
    job_id: str,
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required to stream events.",
        )

    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )

    import json

    async def event_generator():
        last_json = None
        while True:
            current_job = job_service.get_job(job_id)
            if not current_job:
                break

            data_str = json.dumps(current_job.model_dump(mode="json"))
            if data_str != last_json:
                last_json = data_str
                yield f"data: {data_str}\n\n"

            if current_job.status in ["completed", "failed", "cancelled"]:
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/jobs/{job_id}/cancel",
    summary="Cancel a running download job (Tracking Token or Admin Key Required)"
)
async def cancel_job(
    job_id: str,
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required.",
        )

    success = await job_service.cancel_job(job_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not cancel job {job_id}."
        )
    return {"status": "success", "message": f"Job {job_id} cancelled."}


@router.get(
    "/jobs/{job_id}/files",
    response_model=List[FileItemResponse],
    summary="List all generated files for a job (Tracking Token or Admin Key Required)",
    description="Returns table of all files generated by the download job with their 16-character hex file IDs and direct download URLs."
)
async def list_job_files(
    job_id: str,
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required.",
        )

    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )

    return job.files


@router.get(
    "/jobs/{job_id}/files/{file_id}",
    response_model=FileItemResponse,
    summary="Get metadata for a specific file by its hex file ID (Tracking Token or Admin Key Required)"
)
async def get_job_file_info(
    job_id: str,
    file_id: str,
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required.",
        )

    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )

    matching = [f for f in job.files if f.file_id == file_id or f.filename == file_id]
    if not matching:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with ID or name '{file_id}' not found for job {job_id}."
        )

    return matching[0]


@router.get(
    "/jobs/{job_id}/files/{file_id}/download",
    summary="Download specific file by hex ID (Tracking Token or Admin Key Required)"
)
async def download_job_file_by_id(
    job_id: str,
    file_id: str,
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required to download.",
        )

    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )
    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job {job_id} is not completed (current status: {job.status})."
        )

    target_filename = None
    target_id = file_id
    if job.files:
        matching = [f for f in job.files if f.file_id == file_id or f.filename == file_id]
        if matching:
            target_filename = matching[0].filename
            target_id = matching[0].file_id

    if not target_filename:
        target_filename = job.filename or f"file_{file_id}"

    return await _stream_file_response(
        identifier=target_id,
        filename=target_filename,
        container_id=job.container_id,
        job_id=job_id,
    )


@router.get(
    "/jobs/{job_id}/download",
    summary="Download finished media file (Tracking Token or Admin Key Required; Invalidates Token on Finish)"
)
async def download_job_file(
    job_id: str,
    file_id: Optional[str] = Query(None, description="Optional specific hex file ID from the job's files table"),
    token: Optional[str] = Query(None, description="Tracking token returned during job creation"),
    x_tracking_token: Optional[str] = Header(None, alias="X-Tracking-Token"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(None),
):
    provided_token = extract_token_from_request(authorization, x_tracking_token, token)
    is_admin = is_admin_authorized(x_admin_key, authorization)

    if not is_admin and not job_service.validate_tracking_token(job_id, provided_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid job tracking token (or Admin Key) is required to download.",
        )

    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )
    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job {job_id} is not completed (current status: {job.status})."
        )

    target_filename = job.filename
    target_id = file_id

    if file_id and job.files:
        matching = [f for f in job.files if f.file_id == file_id]
        if matching:
            target_filename = matching[0].filename
            target_id = matching[0].file_id

    if not target_filename and job.files:
        target_filename = job.files[0].filename
        target_id = job.files[0].file_id

    if not target_filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No files generated by this job."
        )

    return await _stream_file_response(
        identifier=target_id or target_filename,
        filename=target_filename,
        container_id=job.container_id,
        job_id=job_id,
    )


# ============================================================================
# Admin Container Management Endpoints
# ============================================================================

@router.get(
    "/containers",
    response_model=List[ContainerInfoResponse],
    dependencies=[Depends(require_admin_key)],
    summary="List active runner containers (Admin Key Required)"
)
async def list_containers():
    containers = await docker_manager.list_containers()
    result = []
    for c in containers:
        result.append(
            ContainerInfoResponse(
                container_id=c.container_id,
                name=c.name,
                status="draining" if c.is_draining else "running",
                ip_address=c.host_or_ip,
                endpoint_url=c.base_url,
                idle_seconds=c.idle_seconds,
                remaining_idle_seconds=c.remaining_idle_seconds,
                lifetime_seconds=c.lifetime_seconds,
                remaining_lifetime_seconds=c.remaining_lifetime_seconds,
                file_retention_remaining_seconds=c.file_retention_remaining_seconds,
                is_draining=c.is_draining,
                can_accept_jobs=c.can_accept_jobs,
                active_jobs=c.active_jobs_count,
                last_activity=datetime.datetime.fromtimestamp(
                    c.last_activity, tz=datetime.timezone.utc
                ),
            )
        )
    return result


@router.delete(
    "/containers/{container_id}",
    dependencies=[Depends(require_admin_key)],
    summary="Manually stop and remove runner container (Admin Key Required)"
)
async def remove_container(container_id: str):
    removed = await docker_manager.stop_and_remove_container(container_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Container {container_id} not found or could not be removed."
        )
    return {"status": "success", "message": f"Container {container_id} removed."}


# ============================================================================
# Internal File Streaming Helpers
# ============================================================================

async def _stream_file_response(
    identifier: str,
    filename: Optional[str] = None,
    container_id: Optional[str] = None,
    job_id: Optional[str] = None,
):
    # 1. Direct container lookup if known
    if container_id:
        runner = docker_manager.get_container(container_id)
        if runner:
            runner.touch()
            client = httpx.AsyncClient(timeout=None)
            req = client.build_request("GET", f"{runner.base_url}/files/{identifier}/download")
            try:
                r = await client.send(req, stream=True)
                if r.status_code == 200:
                    headers = dict(r.headers)
                    if filename and "content-disposition" not in [k.lower() for k in headers]:
                        headers["content-disposition"] = f'attachment; filename="{filename}"'

                    async def cleanup():
                        await client.aclose()
                        if job_id:
                            all_downloaded = job_service.record_file_download(job_id, identifier)
                            if all_downloaded and container_id:
                                if job_service.are_all_container_jobs_downloaded(container_id):
                                    runner = docker_manager.get_container(container_id)
                                    if runner and runner.active_jobs_count == 0:
                                        await docker_manager.stop_and_remove_container(container_id)

                    return StreamingResponse(
                        r.aiter_raw(),
                        status_code=200,
                        headers=headers,
                        background=BackgroundTask(cleanup),
                    )
            except Exception as e:
                logger.warning(f"Failed to stream from runner container {container_id}: {e}")
            await client.aclose()

    # 2. Fallback: Search all active runner containers
    containers = await docker_manager.list_containers()
    for c in containers:
        try:
            c.touch()
            client = httpx.AsyncClient(timeout=None)
            req = client.build_request("GET", f"{c.base_url}/files/{identifier}/download")
            r = await client.send(req, stream=True)
            if r.status_code == 200:
                headers = dict(r.headers)
                if filename and "content-disposition" not in [k.lower() for k in headers]:
                    headers["content-disposition"] = f'attachment; filename="{filename}"'

                async def cleanup():
                    await client.aclose()
                    if job_id:
                        all_downloaded = job_service.record_file_download(job_id, identifier)
                        target_cid = container_id or c.container_id
                        if all_downloaded and target_cid:
                            if job_service.are_all_container_jobs_downloaded(target_cid):
                                runner = docker_manager.get_container(target_cid)
                                if runner and runner.active_jobs_count == 0:
                                    await docker_manager.stop_and_remove_container(target_cid)

                return StreamingResponse(
                    r.aiter_raw(),
                    status_code=200,
                    headers=headers,
                    background=BackgroundTask(cleanup),
                )
            await client.aclose()
        except Exception:
            pass

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"File '{identifier}' could not be located on any active runner container."
    )

