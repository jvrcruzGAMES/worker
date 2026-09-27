import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import health, info, jobs
from app.services.announcer import announcer
from app.services.inactivity_reaper import inactivity_reaper

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting Worker [{settings.WORKER_ID}]...")
    announcer.start()
    inactivity_reaper.start()
    yield
    logger.info(f"Stopping Worker [{settings.WORKER_ID}]...")
    await inactivity_reaper.stop()
    await announcer.stop()


app = FastAPI(
    title=f"{settings.WORKER_NAME} ({settings.WORKER_ID})",
    version=settings.WORKER_VERSION,
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

app.include_router(jobs.router)
app.include_router(health.router)
app.include_router(info.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.WORKER_HOST,
        port=settings.WORKER_PORT,
        reload=settings.DEBUG,
    )
