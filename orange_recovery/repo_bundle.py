"""Validation and installation for Orange recovery source ZIP bundles."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import RecoveryConfig


class RepoBundleError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RepoBundleResult:
    ok: bool
    repo_bundle_valid: bool
    message: str
    installed: bool = False
    error: str = ""
    output: str = ""

    def as_response(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "repo_bundle_valid": self.repo_bundle_valid,
            "installed": self.installed,
            "message": self.message,
        }
        if self.error:
            payload["error"] = self.error
        if self.output:
            payload["output"] = self.output[-4000:]
        return payload


def _safe_zip_name(name: str) -> bool:
    if not name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    return ".." not in parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _common_root(names: list[str]) -> str:
    first_parts = [PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts]
    if not first_parts:
        return ""
    first = first_parts[0]
    return first if all(part == first for part in first_parts) else ""


class RepoBundleInstaller:
    def __init__(self, config: RecoveryConfig):
        self.config = config

    def install(self, package_path: str) -> RepoBundleResult:
        source_root: Path | None = None
        extract_parent: Path | None = None
        try:
            source_root, extract_parent = self._extract_and_validate(package_path)
            if self.config.network.dry_run:
                return RepoBundleResult(
                    ok=True,
                    repo_bundle_valid=True,
                    installed=False,
                    message="Recovery repo bundle validated. Dry run did not install it.",
                )

            completed = subprocess.run(
                ["bash", "install.sh"],
                cwd=str(source_root),
                env={**os.environ, "ORANGE_RECOVERY_CONFIG": "/etc/orange-recovery/config.yaml"},
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode != 0:
                return RepoBundleResult(
                    ok=False,
                    repo_bundle_valid=True,
                    installed=False,
                    message="Recovery repo bundle installer failed.",
                    error="installer_failed",
                    output=output,
                )

            return RepoBundleResult(
                ok=True,
                repo_bundle_valid=True,
                installed=True,
                message="Recovery repo bundle installed.",
                output=output,
            )
        except RepoBundleError as exc:
            return RepoBundleResult(
                ok=False,
                repo_bundle_valid=False,
                installed=False,
                message="Recovery repo bundle is invalid.",
                error=exc.reason,
            )
        finally:
            if extract_parent is not None:
                shutil.rmtree(extract_parent, ignore_errors=True)

    def _extract_and_validate(self, package_path: str) -> tuple[Path, Path]:
        path = Path(package_path)
        if not path.exists() or not path.is_file():
            raise RepoBundleError("package_not_found")
        if not zipfile.is_zipfile(path):
            raise RepoBundleError("invalid_zip")

        max_bytes = self.config.api.max_upload_mb * 1024 * 1024
        if path.stat().st_size > max_bytes:
            raise RepoBundleError("package_too_large")

        extract_parent = Path(tempfile.mkdtemp(prefix="orange-recovery-repo-"))
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                names = [info.filename for info in infos if not info.is_dir()]
                total_uncompressed = 0

                for info in infos:
                    if not _safe_zip_name(info.filename):
                        raise RepoBundleError("unsafe_zip_path")
                    if _is_symlink(info):
                        raise RepoBundleError("zip_symlink_rejected")
                    total_uncompressed += int(info.file_size)
                    if info.compress_size > 0 and info.file_size > 1024 * 1024 and info.file_size / info.compress_size > 200:
                        raise RepoBundleError("zip_bomb_rejected")

                if total_uncompressed > max_bytes * 10:
                    raise RepoBundleError("zip_bomb_rejected")

                zf.extractall(extract_parent)

            root_name = _common_root(names)
            source_root = extract_parent / root_name if root_name else extract_parent
            required = [
                source_root / "install.sh",
                source_root / "orange_recovery" / "api_server.py",
                source_root / "orange_recovery" / "recovery_controller.py",
            ]
            if not all(item.exists() and item.is_file() for item in required):
                raise RepoBundleError("repo_files_missing")

            return source_root, extract_parent
        except Exception:
            shutil.rmtree(extract_parent, ignore_errors=True)
            raise
