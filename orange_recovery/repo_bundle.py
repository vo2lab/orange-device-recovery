"""Validation and installation for Orange dispenser Python script ZIP bundles."""

from __future__ import annotations

import os
import pwd
import shutil
import stat
import tempfile
import time
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
    installed_files: list[str] | None = None
    backup_dir: str = ""

    def as_response(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "repo_bundle_valid": self.repo_bundle_valid,
            "runtime_bundle_valid": self.repo_bundle_valid,
            "installed": self.installed,
            "message": self.message,
        }
        if self.error:
            payload["error"] = self.error
        if self.output:
            payload["output"] = self.output[-4000:]
        if self.installed_files is not None:
            payload["installed_files"] = self.installed_files
            payload["installed_count"] = len(self.installed_files)
        if self.backup_dir:
            payload["backup_dir"] = self.backup_dir
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
    paths = [PurePosixPath(name).parts for name in names if PurePosixPath(name).parts]
    if not paths or any(len(parts) < 2 for parts in paths):
        return ""
    first = paths[0][0]
    return first if all(parts[0] == first for parts in paths) else ""


class RepoBundleInstaller:
    def __init__(self, config: RecoveryConfig):
        self.config = config

    def install(self, package_path: str) -> RepoBundleResult:
        py_files: list[tuple[Path, str]] = []
        extract_parent: Path | None = None
        try:
            py_files, extract_parent = self._extract_and_validate(package_path)
            installed_files = [name for _, name in py_files]
            if self.config.network.dry_run:
                return RepoBundleResult(
                    ok=True,
                    repo_bundle_valid=True,
                    installed=False,
                    message=f"Orangelite Python script bundle validated. Dry run did not copy {len(installed_files)} file(s).",
                    installed_files=installed_files,
                )

            target_root = Path(self.config.repair.orangelite_root)
            backup_dir = Path(self.config.paths.backup_dir) / "orangelite-scripts" / time.strftime("%Y%m%d%H%M%S")
            self._copy_python_files(py_files, target_root, backup_dir)

            return RepoBundleResult(
                ok=True,
                repo_bundle_valid=True,
                installed=True,
                message=(
                    "Orangelite Python scripts installed. Reconnect this phone to normal Wi-Fi now; "
                    "the recovery hotspot will disconnect shortly."
                ),
                installed_files=installed_files,
                backup_dir=str(backup_dir),
            )
        except RepoBundleError as exc:
            return RepoBundleResult(
                ok=False,
                repo_bundle_valid=False,
                installed=False,
                message="Orangelite Python script bundle is invalid.",
                error=exc.reason,
            )
        finally:
            if extract_parent is not None:
                shutil.rmtree(extract_parent, ignore_errors=True)

    def _extract_and_validate(self, package_path: str) -> tuple[list[tuple[Path, str]], Path]:
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

                root_name = _common_root(names)
                relative_names: list[str] = []
                seen_names: set[str] = set()
                for name in names:
                    rel_name = self._relative_script_name(name, root_name)
                    if not rel_name:
                        continue
                    if "/" in rel_name or "\\" in rel_name:
                        raise RepoBundleError("nested_file_rejected")
                    if not rel_name.endswith(".py"):
                        raise RepoBundleError("non_python_file_rejected")
                    if PurePosixPath(rel_name).name != rel_name:
                        raise RepoBundleError("unsafe_zip_path")
                    if rel_name in seen_names:
                        raise RepoBundleError("duplicate_file")
                    seen_names.add(rel_name)
                    relative_names.append(rel_name)

                if not relative_names:
                    raise RepoBundleError("python_files_missing")

                zf.extractall(extract_parent)

            source_root = extract_parent / root_name if root_name else extract_parent
            py_files = [(source_root / rel_name, rel_name) for rel_name in relative_names]
            if not all(source.exists() and source.is_file() for source, _ in py_files):
                raise RepoBundleError("source_missing")

            return py_files, extract_parent
        except Exception:
            shutil.rmtree(extract_parent, ignore_errors=True)
            raise

    def _relative_script_name(self, name: str, root_name: str) -> str:
        if root_name and name.startswith(root_name + "/"):
            return name[len(root_name) + 1:]
        return name

    def _copy_python_files(self, py_files: list[tuple[Path, str]], target_root: Path, backup_dir: Path) -> None:
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_root.is_dir():
            raise RepoBundleError("target_root_not_directory")

        for source, rel_name in py_files:
            target = target_root / rel_name
            if target.exists() and not target.is_file():
                raise RepoBundleError("target_not_file")

            owner = self._target_owner(target)
            if target.exists():
                backup_path = backup_dir / rel_name
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_path)

            tmp_target = target.with_name(target.name + ".orange-recovery.tmp")
            shutil.copy2(source, tmp_target)
            os.chmod(tmp_target, 0o644)
            os.replace(tmp_target, target)
            if owner is not None:
                try:
                    os.chown(target, owner[0], owner[1])
                except OSError:
                    pass

    def _target_owner(self, target: Path) -> tuple[int, int] | None:
        try:
            if target.exists():
                stat_result = target.stat()
                return stat_result.st_uid, stat_result.st_gid
            pi_user = pwd.getpwnam("pi")
            return pi_user.pw_uid, pi_user.pw_gid
        except (KeyError, OSError):
            return None
