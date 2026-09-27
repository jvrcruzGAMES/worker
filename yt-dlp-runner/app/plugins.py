import asyncio
import logging
import sys
from typing import List, Tuple

from app.schemas import PluginInstallRequest, PluginInstallResponse

logger = logging.getLogger("yt_dlp_runner.plugins")


class PluginManager:
    @staticmethod
    async def install_plugins(request: PluginInstallRequest) -> PluginInstallResponse:
        packages = [pkg.strip() for pkg in request.packages if pkg.strip()]
        if not packages:
            return PluginInstallResponse(
                success=False,
                packages=[],
                stdout="",
                stderr="No valid package names provided.",
                installed_packages=[],
            )

        cmd = [sys.executable, "-m", "pip", "install"]
        if request.upgrade:
            cmd.append("--upgrade")
        cmd.extend(packages)

        logger.info(f"Running pip install: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await process.communicate()
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        success = process.returncode == 0
        if success:
            logger.info(f"Successfully installed plugins: {packages}")
        else:
            logger.error(f"Failed to install plugins: {packages}. stderr: {stderr}")

        return PluginInstallResponse(
            success=success,
            packages=packages,
            stdout=stdout,
            stderr=stderr,
            installed_packages=packages if success else [],
        )


plugin_manager = PluginManager()
