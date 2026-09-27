import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_downloads_dir() -> str:
    if os.getenv("DOWNLOADS_DIR"):
        return os.getenv("DOWNLOADS_DIR")
    if os.path.exists("/app") or os.getenv("IN_DOCKER"):
        return "/app/downloads"
    return "./downloads"


def _default_cookies_dir() -> str:
    if os.getenv("COOKIES_DIR"):
        return os.getenv("COOKIES_DIR")
    if os.path.exists("/app") or os.getenv("IN_DOCKER"):
        return "/app/cookies"
    return "./cookies"


class RunnerSettings(BaseSettings):
    APP_NAME: str = "yt-dlp Runner Supervisor"
    APP_VERSION: str = "0.1.0"
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    DOWNLOADS_DIR: str = _default_downloads_dir()
    COOKIES_DIR: str = _default_cookies_dir()

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def ensure_directories(self):
        try:
            Path(self.DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)
            Path(self.COOKIES_DIR).mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fallback for restricted local test environments
            self.DOWNLOADS_DIR = "./downloads"
            self.COOKIES_DIR = "./cookies"
            Path(self.DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)
            Path(self.COOKIES_DIR).mkdir(parents=True, exist_ok=True)


settings = RunnerSettings()
settings.ensure_directories()
