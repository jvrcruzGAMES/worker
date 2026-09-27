import datetime
import os
from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
import httpx

from app.config import settings
from app.schemas import (
    ContainerInfoResponse,
    FileItemResponse,
    JobCreateRequest,
    JobResponse,
    PluginInstallRequest,
    PluginInstallResponse,
)
from app.services.docker_manager import docker_manager
from app.services.job_service import job_service

router = APIRouter(prefix="/api/v1", tags=["Jobs & Downloads"])


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a yt-dlp download job",
    description="Spins up child yt-dlp container (if not already running), isolates cookie file, and starts download."
)
async def create_job(request: JobCreateRequest):
    return await job_service.create_job(request)


@router.get(
    "/jobs",
    response_model=List[JobResponse],
    summary="List all download jobs"
)
async def list_jobs():
    return job_service.list_jobs()


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get job status and progress"
)
async def get_job(job_id: str):
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )
    return job


@router.post(
    "/jobs/{job_id}/cancel",
    summary="Cancel a running download job"
)
async def cancel_job(job_id: str):
    success = await job_service.cancel_job(job_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not cancel job {job_id}."
        )
    return {"status": "success", "message": f"Job {job_id} cancelled."}


@router.get(
    "/jobs/{job_id}/download",
    summary="Download finished media file for a job"
)
async def download_job_file(job_id: str):
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found."
        )
    if job.status != "completed" or not job.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job {job_id} is not completed (current status: {job.status})."
        )

    # 1. First check local shared directory
    local_file = Path(settings.LOCAL_DOWNLOADS_PATH) / job.filename
    if local_file.is_file() and local_file.exists():
        return FileResponse(
            path=str(local_file),
            filename=job.filename,
            media_type="application/octet-stream",
        )

    # 2. Otherwise proxy from runner container
    if job.container_id:
        runner = docker_manager.get_container(job.container_id)
        if runner:
            runner.touch()
            client = httpx.AsyncClient(timeout=None)
            req = client.build_request("GET", f"{runner.base_url}/files/{job.filename}")
            r = await client.send(req, stream=True)
            return StreamingResponse(
                r.aiter_raw(),
                status_code=r.status_code,
                headers=dict(r.headers),
                background=client.aclose,
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Media file '{job.filename}' could not be located."
    )


@router.post(
    "/plugins/install",
    response_model=PluginInstallResponse,
    summary="Install Python plugin in yt-dlp runner container"
)
async def install_plugin(request: PluginInstallRequest):
    return await job_service.install_plugins(request)


@router.get(
    "/containers",
    response_model=List[ContainerInfoResponse],
    summary="List active runner containers and inactivity countdown"
)
async def list_containers():
    containers = await docker_manager.list_containers()
    result = []
    for c in containers:
        result.append(
            ContainerInfoResponse(
                container_id=c.container_id,
                name=c.name,
                status="running",
                ip_address=c.host_or_ip,
                endpoint_url=c.base_url,
                idle_seconds=c.idle_seconds,
                remaining_idle_seconds=c.remaining_idle_seconds,
                active_jobs=c.active_jobs_count,
                last_activity=datetime.datetime.fromtimestamp(
                    c.last_activity, tz=datetime.timezone.utc
                ),
            )
        )
    return result


@router.delete(
    "/containers/{container_id}",
    summary="Manually stop and remove runner container"
)
async def remove_container(container_id: str):
    removed = await docker_manager.stop_and_remove_container(container_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Container {container_id} not found or could not be removed."
        )
    return {"status": "success", "message": f"Container {container_id} removed."}


@router.get(
    "/files",
    response_model=List[FileItemResponse],
    summary="List downloaded files"
)
async def list_files():
    downloads_path = Path(settings.LOCAL_DOWNLOADS_PATH)
    files = []
    if downloads_path.exists():
        for f in downloads_path.iterdir():
            if f.is_file():
                stat = f.stat()
                files.append(
                    FileItemResponse(
                        filename=f.name,
                        size_bytes=stat.st_size,
                        modified_at=datetime.datetime.fromtimestamp(
                            stat.st_mtime, tz=datetime.timezone.utc
                        ),
                        download_url=f"/api/v1/files/{f.name}",
                    )
                )
    return files


@router.get(
    "/files/{filename}",
    summary="Stream downloaded media file"
)
async def stream_file(filename: str):
    downloads_path = Path(settings.LOCAL_DOWNLOADS_PATH)
    file_path = downloads_path / filename
    if file_path.is_file() and file_path.exists():
        return FileResponse(
            path=str(file_path),
            filename=filename,
            media_type="application/octet-stream",
        )

    # Search through active runner containers
    containers = await docker_manager.list_containers()
    for c in containers:
        try:
            c.touch()
            client = httpx.AsyncClient(timeout=None)
            req = client.build_request("GET", f"{c.base_url}/files/{filename}")
            r = await client.send(req, stream=True)
            if r.status_code == 200:
                return StreamingResponse(
                    r.aiter_raw(),
                    status_code=200,
                    headers=dict(r.headers),
                    background=client.aclose,
                )
            await client.aclose()
        except Exception:
            pass

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"File '{filename}' not found."
    )
