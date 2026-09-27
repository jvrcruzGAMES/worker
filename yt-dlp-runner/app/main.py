import datetime
import logging
import os
import time
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from contextlib import asynccontextmanager
from app.config import settings
from app.downloader import downloader_manager
from app.flaresolverr_proxy import flaresolverr_proxy
from app.plugins import plugin_manager
from app.schemas import (
    ActivityResponse,
    DownloadRequest,
    DownloadTaskResponse,
    FileInfo,
    PluginInstallRequest,
    PluginInstallResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("yt_dlp_runner")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting yt-dlp runner supervisor...")
    await flaresolverr_proxy.start()
    yield
    logger.info("Stopping yt-dlp runner supervisor...")
    await flaresolverr_proxy.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["System"])
async def health():
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "active_tasks": downloader_manager.get_active_tasks_count(),
    }


@app.get("/activity", response_model=ActivityResponse, tags=["System"])
async def check_activity():
    """Returns activity status and idle duration for auto-cleanup by worker"""
    now = time.time()
    active_tasks = downloader_manager.get_active_tasks_count()
    is_busy = active_tasks > 0
    idle_seconds = 0.0 if is_busy else round(now - downloader_manager.last_activity_time, 2)
    return ActivityResponse(
        is_busy=is_busy,
        active_tasks_count=active_tasks,
        last_activity_timestamp=downloader_manager.last_activity_time,
        idle_seconds=idle_seconds,
        total_downloads=downloader_manager.total_completed_downloads,
    )


@app.post("/plugins/install", response_model=PluginInstallResponse, tags=["Plugins"])
async def install_plugin(request: PluginInstallRequest):
    """Installs yt-dlp plugin packages via pip inside the runner container"""
    downloader_manager.touch_activity()
    return await plugin_manager.install_plugins(request)


@app.post("/download", response_model=DownloadTaskResponse, status_code=status.HTTP_202_ACCEPTED, tags=["Downloads"])
async def trigger_download(request: DownloadRequest):
    """Starts a yt-dlp download task in the background, returning task and file metadata"""
    return await downloader_manager.start_download(request)


@app.get("/download/{task_id}", response_model=DownloadTaskResponse, tags=["Downloads"])
async def get_download_status(task_id: str):
    """Gets the status, progress, logs and table of generated files for a download task"""
    task = downloader_manager.get_task(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found."
        )
    return task


@app.post("/download/{task_id}/cancel", tags=["Downloads"])
async def cancel_download(task_id: str):
    """Cancels an active download task"""
    cancelled = downloader_manager.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task {task_id} is not currently running or cannot be cancelled."
        )
    return {"status": "cancelled", "task_id": task_id}


@app.get("/files", response_model=List[FileInfo], tags=["Files"])
async def list_files():
    """Lists all files in downloads directory with their hex IDs and download URLs"""
    return downloader_manager.list_all_files()


@app.get("/files/{file_id_or_name}/download", tags=["Files"])
@app.get("/files/{file_id_or_name}", tags=["Files"])
async def stream_file(file_id_or_name: str):
    """Streams / downloads a specific file by its hex file ID or filename"""
    file_path = downloader_manager.get_file_by_id_or_name(file_id_or_name)
    if not file_path or not file_path.is_file() or not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with ID or name '{file_id_or_name}' not found."
        )

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@app.delete("/files/{file_id_or_name}", tags=["Files"])
async def delete_file(file_id_or_name: str):
    """Deletes a file by its hex ID or filename"""
    file_path = downloader_manager.get_file_by_id_or_name(file_id_or_name)
    if not file_path or not file_path.is_file() or not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with ID or name '{file_id_or_name}' not found."
        )
    os.remove(file_path)
    downloader_manager.file_registry.pop(file_id_or_name, None)
    return {"status": "deleted", "identifier": file_id_or_name, "filename": file_path.name}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
    )
