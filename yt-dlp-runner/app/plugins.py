import asyncio
import logging
import re
import sys
from typing import List

from app.schemas import PluginInstallRequest, PluginInstallResponse

logger = logging.getLogger("yt_dlp_runner.plugins")


def normalize_plugin_spec(spec: str) -> str:
    """
    Normalizes a plugin specifier or Git repository URL into the proper format expected by pip.
    e.g.:
      - 'https://github.com/owner/repo' -> 'git+https://github.com/owner/repo'
      - 'http://gitlab.com/owner/repo.git' -> 'git+http://gitlab.com/owner/repo.git'
      - 'github.com/owner/repo' -> 'git+https://github.com/owner/repo'
      - 'git@github.com:owner/repo.git' -> 'git+ssh://git@github.com/owner/repo.git'
      - 'git+https://github.com/owner/repo' -> 'git+https://github.com/owner/repo'
      - 'bgutil-ytdlp-pot-provider>=2.0' -> 'bgutil-ytdlp-pot-provider>=2.0'
    """
    pkg = spec.strip()
    if not pkg:
        return ""

    # Already starts with a pip-compatible VCS prefix
    if pkg.startswith("git+") or pkg.startswith("hg+") or pkg.startswith("svn+") or pkg.startswith("bzr+"):
        return pkg

    # SSH git format (e.g. git@github.com:owner/repo.git -> git+ssh://git@github.com/owner/repo.git)
    if pkg.startswith("git@"):
        if ":" in pkg:
            user_host, path = pkg.split(":", 1)
            path = path.lstrip("/")
            return f"git+ssh://{user_host}/{path}"
        return f"git+ssh://{pkg}"

    # Full HTTP or HTTPS URLs (e.g. https://github.com/owner/repo or http://custom-git/repo.git)
    if pkg.startswith("https://") or pkg.startswith("http://"):
        return f"git+{pkg}"

    # Shorthand Git forge domains (e.g. github.com/owner/repo)
    known_forges = ["github.com/", "gitlab.com/", "bitbucket.org/", "codeberg.org/"]
    if any(pkg.lower().startswith(forge) for forge in known_forges):
        return f"git+https://{pkg}"

    # Plain PyPI package name / version specifier
    return pkg


class PluginManager:
    @staticmethod
    async def install_plugins(request: PluginInstallRequest) -> PluginInstallResponse:
        raw_packages = [pkg.strip() for pkg in request.packages if pkg.strip()]
        if not raw_packages:
            return PluginInstallResponse(
                success=False,
                packages=[],
                stdout="",
                stderr="No valid package names provided.",
                installed_packages=[],
            )

        # Normalize any Git repo URLs to git+... format expected by pip
        normalized_packages = [normalize_plugin_spec(pkg) for pkg in raw_packages]

        cmd = [sys.executable, "-m", "pip", "install"]
        if request.upgrade:
            cmd.append("--upgrade")
        cmd.extend(normalized_packages)

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
            logger.info(f"Successfully installed plugins: {normalized_packages}")
        else:
            logger.error(f"Failed to install plugins: {normalized_packages}. stderr: {stderr}")

        return PluginInstallResponse(
            success=success,
            packages=normalized_packages,
            stdout=stdout,
            stderr=stderr,
            installed_packages=normalized_packages if success else [],
        )


plugin_manager = PluginManager()
