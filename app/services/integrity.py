import hashlib
import hmac
import logging
import os
from pathlib import Path
import subprocess
from typing import Dict, List, Optional

from app.config import settings

logger = logging.getLogger("worker.integrity")


class WorkerIntegrityService:
    def __init__(self):
        self._cached_commit: Optional[str] = None
        self._root_dir = Path(__file__).resolve().parent.parent.parent  # worker/ root directory

    def get_commit_sha(self) -> str:
        """
        Discovers the current git commit SHA for the worker codebase.
        """
        if settings.GIT_COMMIT_SHA:
            return settings.GIT_COMMIT_SHA.strip()

        if self._cached_commit:
            return self._cached_commit

        # Try git rev-parse HEAD
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._root_dir),
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                self._cached_commit = res.stdout.strip()
                return self._cached_commit
        except Exception:
            pass

        # Try reading .git directly
        git_head_file = self._root_dir / ".git" / "HEAD"
        if git_head_file.is_file():
            try:
                head_content = git_head_file.read_text().strip()
                if head_content.startswith("ref:"):
                    ref_path = head_content[4:].strip()
                    ref_file = self._root_dir / ".git" / ref_path
                    if ref_file.is_file():
                        self._cached_commit = ref_file.read_text().strip()
                        return self._cached_commit
                elif len(head_content) in (40, 64):
                    self._cached_commit = head_content
                    return self._cached_commit
            except Exception:
                pass

        # Fallback to default / dev commit
        return "dev"

    @staticmethod
    def calculate_git_blob_sha(content: bytes) -> str:
        """
        Calculates the standard git object blob SHA-1 hash for binary content.
        Formula: sha1("blob " + len(content) + "\0" + content)
        """
        header = f"blob {len(content)}\0".encode("utf-8")
        return hashlib.sha1(header + content).hexdigest()

    def read_and_hash_file(self, relative_path: str) -> Optional[str]:
        """
        Reads local source file and calculates its git blob SHA.
        """
        # Normalize relative path (strip leading slashes or 'worker/' prefix)
        clean_path = relative_path.lstrip("/")
        if clean_path.startswith("worker/"):
            clean_path = clean_path[len("worker/"):]

        file_path = self._root_dir / clean_path
        if not file_path.is_file():
            logger.warning(f"Integrity check file not found locally: {file_path}")
            return None

        content = file_path.read_bytes()
        return self.calculate_git_blob_sha(content)

    def compute_challenge_proof(
        self, nonce: str, commit_sha: str, sampled_files: List[str]
    ) -> str:
        """
        Computes the HMAC-SHA256 integrity proof over the sampled files and nonce.
        """
        file_hashes: Dict[str, str] = {}
        for f in sampled_files:
            blob_sha = self.read_and_hash_file(f)
            if not blob_sha:
                blob_sha = "0000000000000000000000000000000000000000"
            file_hashes[f] = blob_sha

        canonical_parts = [f"{k}={file_hashes[k]}" for k in sorted(file_hashes.keys())]
        canonical_payload = f"{commit_sha}:" + ":".join(canonical_parts)

        proof = hmac.new(
            nonce.encode("utf-8"),
            canonical_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return proof

    def get_image_metadata(self) -> tuple[Optional[str], Optional[str]]:
        """
        Returns (image_ref, image_digest) from environment if available.
        """
        return settings.IMAGE_REF, settings.IMAGE_DIGEST

    def get_runner_commit_sha(self) -> str:
        """
        Discovers the current git commit SHA for the yt-dlp runner submodule / image.
        """
        if getattr(settings, "RUNNER_GIT_COMMIT_SHA", None):
            return settings.RUNNER_GIT_COMMIT_SHA.strip()

        runner_dir = self._root_dir / "yt-dlp-runner"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(runner_dir),
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

        # Try reading submodule git file or .git/modules/yt-dlp-runner/HEAD
        git_head_file = runner_dir / ".git" / "HEAD"
        if git_head_file.is_file():
            try:
                head_content = git_head_file.read_text().strip()
                if head_content.startswith("ref:"):
                    ref_path = head_content[4:].strip()
                    ref_file = runner_dir / ".git" / ref_path
                    if ref_file.is_file():
                        return ref_file.read_text().strip()
                elif len(head_content) in (40, 64):
                    return head_content
            except Exception:
                pass

        return "dev"

    def get_runner_image_metadata(self) -> tuple[Optional[str], Optional[str]]:
        """
        Returns (runner_image_ref, runner_image_digest) from environment if available.
        """
        runner_image = getattr(settings, "RUNNER_IMAGE", "ghcr.io/jvrcruzgames/yt-dlp-runner:latest")
        runner_digest = getattr(settings, "RUNNER_IMAGE_DIGEST", None)
        return runner_image, runner_digest


worker_integrity = WorkerIntegrityService()
