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

    def _read_git_head(self, git_path: Path) -> Optional[str]:
        """
        Robustly resolves a git commit SHA from a .git folder or submodule .git file.
        Handles ref pointers, packed-refs, and gitdir pointers.
        """
        if not git_path.exists():
            return None

        # 1. Handle submodule .git file pointer ("gitdir: ../.git/modules/...")
        if git_path.is_file():
            try:
                content = git_path.read_text().strip()
                if content.startswith("gitdir:"):
                    rel_dir = content[len("gitdir:"):].strip()
                    resolved = (git_path.parent / rel_dir).resolve()
                    return self._read_git_head(resolved)
                elif len(content) in (40, 64):
                    return content
            except Exception:
                return None

        # 2. Handle standard .git directory
        head_file = git_path / "HEAD"
        if not head_file.is_file():
            return None

        try:
            head_content = head_file.read_text().strip()
            if head_content.startswith("ref:"):
                ref_path = head_content[4:].strip()
                ref_file = git_path / ref_path
                if ref_file.is_file():
                    return ref_file.read_text().strip()
                # Check packed-refs if branch ref file does not exist directly
                packed_refs_file = git_path / "packed-refs"
                if packed_refs_file.is_file():
                    for line in packed_refs_file.read_text().splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and not line.startswith("^"):
                            parts = line.split(" ", 1)
                            if len(parts) == 2 and parts[1].strip() == ref_path:
                                return parts[0].strip()
            elif len(head_content) in (40, 64):
                return head_content
        except Exception:
            pass

        return None

    def get_commit_sha(self) -> str:
        """
        Discovers the current git commit SHA for the worker codebase.
        """
        if settings.GIT_COMMIT_SHA:
            return settings.GIT_COMMIT_SHA.strip()

        if self._cached_commit:
            return self._cached_commit

        # 1. Check static commit file if baked into Docker container
        for commit_file in [self._root_dir / "COMMIT_SHA", self._root_dir / ".commit_sha"]:
            if commit_file.is_file():
                try:
                    c = commit_file.read_text().strip()
                    if len(c) in (40, 64):
                        self._cached_commit = c
                        return self._cached_commit
                except Exception:
                    pass

        # 2. Try git rev-parse HEAD via CLI
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

        # 3. Try reading .git directly (handles submodules, ref pointers, packed-refs)
        resolved_commit = self._read_git_head(self._root_dir / ".git")
        if resolved_commit:
            self._cached_commit = resolved_commit
            return self._cached_commit

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

        # 1. Check static commit file if baked into container
        for commit_file in [
            runner_dir / "COMMIT_SHA",
            runner_dir / ".commit_sha",
            self._root_dir / "RUNNER_COMMIT_SHA",
            self._root_dir / ".runner_commit_sha",
        ]:
            if commit_file.is_file():
                try:
                    c = commit_file.read_text().strip()
                    if len(c) in (40, 64):
                        return c
                except Exception:
                    pass

        # 2. Try git CLI from runner_dir or root_dir
        for cwd, cmd in [
            (str(runner_dir), ["git", "rev-parse", "HEAD"]),
            (str(self._root_dir), ["git", "rev-parse", "HEAD:yt-dlp-runner"]),
        ]:
            try:
                res = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
            except Exception:
                pass

        # 3. Try reading submodule .git file or directory directly
        for candidate_path in [
            runner_dir / ".git",
            self._root_dir / ".git" / "modules" / "yt-dlp-runner",
            self._root_dir.parent / ".git" / "modules" / "worker" / "modules" / "yt-dlp-runner",
            self._root_dir.parent / ".git" / "modules" / "yt-dlp-runner",
        ]:
            resolved = self._read_git_head(candidate_path)
            if resolved:
                return resolved

        return "dev"

    def get_runner_image_metadata(self) -> tuple[Optional[str], Optional[str]]:
        """
        Returns (runner_image_ref, runner_image_digest) from environment if available.
        """
        runner_image = getattr(settings, "RUNNER_IMAGE", "ghcr.io/jvrcruzgames/yt-dlp-runner:latest")
        runner_digest = getattr(settings, "RUNNER_IMAGE_DIGEST", None)
        return runner_image, runner_digest


worker_integrity = WorkerIntegrityService()
