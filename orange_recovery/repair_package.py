"""Repair package validation and application."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import RecoveryConfig
from .service_control import ServiceControl


class RepairPackageError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ValidationResult:
    ok: bool
    package_valid: bool
    requires_confirmation: bool
    manifest: dict[str, Any]
    path: str
    error: str = ""

    def as_response(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "package_valid": self.package_valid,
            "requires_confirmation": self.requires_confirmation,
            "manifest": self.manifest,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def _safe_zip_name(name: str) -> bool:
    if not name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    return ".." not in parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _normalise_target(path: str) -> str:
    return os.path.abspath(path)


def _target_allowed(target: str, prefixes: list[str]) -> bool:
    normal = _normalise_target(target)
    for prefix in prefixes:
        prefix_normal = _normalise_target(prefix)
        if normal == prefix_normal.rstrip("/") or normal.startswith(prefix_normal.rstrip("/") + "/"):
            return True
    return False


def _parse_checksums(raw: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            raise RepairPackageError("invalid_checksums")
        digest = parts[0].lower()
        filename = parts[-1].lstrip("*")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RepairPackageError("invalid_checksums")
        checksums[filename] = digest
    return checksums


def _local_model() -> str:
    for model_path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            model = Path(model_path).read_bytes().replace(b"\x00", b"").decode("utf-8", "ignore").lower()
        except OSError:
            continue
        if "raspberry pi 5" in model:
            return "rpi5"
        if "raspberry pi" in model:
            return "rpi"
        if "rock" in model or "rk35" in model:
            return "rockpi"
    return "unknown"


class RepairPackageManager:
    def __init__(self, config: RecoveryConfig, service_control: ServiceControl | None = None):
        self.config = config
        self.service_control = service_control or ServiceControl(
            config.services.normal_service_name,
            dry_run=config.network.dry_run,
        )
        self.logger = logging.getLogger("orange_recovery.repair")

    def validate_package(self, package_path: str) -> ValidationResult:
        try:
            manifest = self._validate_or_raise(package_path)
            return ValidationResult(
                ok=True,
                package_valid=True,
                requires_confirmation=bool(manifest.get("requires_confirmation", True)),
                manifest=manifest,
                path=package_path,
            )
        except RepairPackageError as exc:
            return ValidationResult(
                ok=False,
                package_valid=False,
                requires_confirmation=False,
                manifest={},
                path=package_path,
                error=exc.reason,
            )

    def _validate_or_raise(self, package_path: str) -> dict[str, Any]:
        path = Path(package_path)
        if not path.exists() or not path.is_file():
            raise RepairPackageError("package_not_found")

        max_bytes = self.config.repair.max_zip_mb * 1024 * 1024
        if path.stat().st_size > max_bytes:
            raise RepairPackageError("package_too_large")
        if not zipfile.is_zipfile(path):
            raise RepairPackageError("invalid_zip")

        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                raise RepairPackageError("manifest_missing")
            if "checksums.sha256" not in names:
                raise RepairPackageError("checksums_missing")

            total_uncompressed = 0
            for info in zf.infolist():
                if not _safe_zip_name(info.filename):
                    raise RepairPackageError("unsafe_zip_path")
                if _is_symlink(info):
                    raise RepairPackageError("zip_symlink_rejected")
                total_uncompressed += int(info.file_size)
                if info.compress_size > 0 and info.file_size > 1024 * 1024 and info.file_size / info.compress_size > 200:
                    raise RepairPackageError("zip_bomb_rejected")
            if total_uncompressed > max_bytes * 10:
                raise RepairPackageError("zip_bomb_rejected")

            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception as exc:
                raise RepairPackageError("manifest_invalid") from exc
            if not isinstance(manifest, dict):
                raise RepairPackageError("manifest_invalid")

            if manifest.get("package_type") != "orange_repair":
                raise RepairPackageError("invalid_package_type")

            target_machine = str(manifest.get("target_machine") or "").strip()
            if self.config.repair.require_machine_match and target_machine not in {self.config.machine_id, "*"}:
                raise RepairPackageError("wrong_machine")

            allowed_models = manifest.get("allowed_models")
            model = _local_model()
            if isinstance(allowed_models, list) and allowed_models and model != "unknown":
                allowed = {str(item).strip().lower() for item in allowed_models}
                if model not in allowed:
                    raise RepairPackageError("unsupported_model")

            actions = manifest.get("actions", [])
            if not isinstance(actions, list):
                raise RepairPackageError("invalid_actions")
            if not self.config.repair.allow_script_execution:
                for action in actions:
                    action_text = str(action).lower()
                    if "script" in action_text or "execute" in action_text:
                        raise RepairPackageError("script_execution_disabled")

            files = manifest.get("files", [])
            if not isinstance(files, list) or not files:
                raise RepairPackageError("files_missing")

            checksums = _parse_checksums(zf.read("checksums.sha256").decode("utf-8"))
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    raise RepairPackageError("invalid_file_entry")
                source = str(file_entry.get("source") or "")
                target = str(file_entry.get("target") or "")
                if not _safe_zip_name(source) or source not in names:
                    raise RepairPackageError("source_missing")
                if not target or not _target_allowed(target, self.config.repair.allowed_target_prefixes):
                    raise RepairPackageError("target_not_allowed")
                expected = checksums.get(source)
                if not expected:
                    raise RepairPackageError("checksum_missing")
                actual = hashlib.sha256(zf.read(source)).hexdigest()
                if actual.lower() != expected.lower():
                    raise RepairPackageError("checksum_mismatch")
                mode = str(file_entry.get("mode") or "0644")
                try:
                    int(mode, 8)
                except ValueError as exc:
                    raise RepairPackageError("invalid_file_mode") from exc

        return manifest

    def apply_package(self, package_path: str, validation: ValidationResult | None = None) -> dict[str, Any]:
        validation = validation or self.validate_package(package_path)
        if not validation.ok:
            return {"ok": False, "state": "FAILED", "message": validation.error or "Package validation failed."}

        backup_dir = Path(self.config.paths.backup_dir) / time.strftime("%Y%m%d%H%M%S")
        rollback_records: list[dict[str, Any]] = []
        dry_run = self.config.network.dry_run

        with zipfile.ZipFile(package_path) as zf, tempfile.TemporaryDirectory(prefix="orange-repair-") as tmp_dir:
            for file_entry in validation.manifest.get("files", []):
                source = str(file_entry["source"])
                target = Path(str(file_entry["target"]))
                mode = int(str(file_entry.get("mode") or "0644"), 8)
                extracted = Path(tmp_dir) / source
                extracted.parent.mkdir(parents=True, exist_ok=True)
                extracted.write_bytes(zf.read(source))

                record: dict[str, Any] = {"target": str(target), "existed": target.exists()}
                if target.exists():
                    backup_path = backup_dir / str(target).lstrip("/")
                    record["backup"] = str(backup_path)
                    if not dry_run:
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup_path)
                rollback_records.append(record)

                self.logger.info("installing repair file %s -> %s", source, target)
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp_target = target.with_name(target.name + ".orange-recovery.tmp")
                    shutil.copyfile(extracted, tmp_target)
                    os.chmod(tmp_target, mode)
                    os.replace(tmp_target, target)

        rollback_path = Path(self.config.paths.state_dir) / "last-rollback.json"
        rollback_path.parent.mkdir(parents=True, exist_ok=True)
        rollback_path.write_text(json.dumps({"created_at": int(time.time()), "records": rollback_records}, indent=2), encoding="utf-8")
        os.chmod(rollback_path, 0o600)

        actions = [str(action) for action in validation.manifest.get("actions", [])]
        if "restart_orange_service" in actions:
            self.service_control.restart()

        return {"ok": True, "state": "COMPLETE", "message": "Repair applied.", "rollback": str(rollback_path)}

    def rollback(self) -> dict[str, Any]:
        rollback_path = Path(self.config.paths.state_dir) / "last-rollback.json"
        if not rollback_path.exists():
            return {"ok": False, "state": "FAILED", "message": "No rollback record is available."}
        payload = json.loads(rollback_path.read_text(encoding="utf-8"))
        records = payload.get("records")
        if not isinstance(records, list):
            return {"ok": False, "state": "FAILED", "message": "Rollback record is invalid."}

        dry_run = self.config.network.dry_run
        for record in reversed(records):
            target = Path(str(record.get("target") or ""))
            backup = str(record.get("backup") or "")
            existed = bool(record.get("existed"))
            if existed and backup:
                self.logger.info("rolling back %s from %s", target, backup)
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
            elif target.exists():
                self.logger.info("removing created repair file %s", target)
                if not dry_run:
                    target.unlink()
        return {"ok": True, "state": "COMPLETE", "message": "Rollback complete."}
