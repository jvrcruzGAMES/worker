import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("yt_dlp_runner.flaresolverr_proxy")


class FlareSolverrProxyService:
    def __init__(self):
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self.proxy_port = getattr(settings, "MITMPROXY_PORT", 8192)
        self.proxy_url = f"http://127.0.0.1:{self.proxy_port}"

    async def start(self):
        if self._running:
            return

        addon_path = Path(__file__).parent / "flaresolverr_addon.py"
        mitmdump_bin = shutil.which("mitmdump")

        if not mitmdump_bin:
            logger.warning("mitmdump binary not found in PATH. FlareSolverr proxy will not be started locally.")
            return

        cmd = [
            mitmdump_bin,
            "-p", str(self.proxy_port),
            "-s", str(addon_path),
            "--set", "block_global=false",
            "--set", "ssl_insecure=true",
            "--quiet",
        ]

        try:
            logger.info(f"Starting mitmproxy FlareSolverr solver on port {self.proxy_port}...")
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._running = True
            logger.info(f"FlareSolverr proxy active at {self.proxy_url}")
        except Exception as e:
            logger.warning(f"Could not start mitmproxy: {e}")

    async def stop(self):
        if self._process and self._running:
            try:
                self._process.terminate()
                await self._process.wait()
            except Exception:
                pass
            self._running = False
            logger.info("FlareSolverr proxy stopped.")

    @property
    def is_active(self) -> bool:
        return self._running and self._process is not None


flaresolverr_proxy = FlareSolverrProxyService()
