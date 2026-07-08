"""State machine and orchestration for Orange local recovery."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .client_detection import client_connected
from .config import RecoveryConfig
from .diagnostics import make_diagnostics_zip
from .display import DisplayAnnouncer
from .hotspot import RecoveryHotspot
from .network_manager import NetworkManager
from .repair_package import RepairPackageManager, ValidationResult
from .repo_bundle import RepoBundleInstaller
from .security import generate_session_token
from .service_control import ServiceControl


IDLE = "IDLE"
QR_TRIGGER_RECEIVED = "QR_TRIGGER_RECEIVED"
STARTING_HOTSPOT = "STARTING_HOTSPOT"
WAITING_FOR_CLIENT = "WAITING_FOR_CLIENT"
CLIENT_CONNECTED = "CLIENT_CONNECTED"
API_ACTIVE = "API_ACTIVE"
WAITING_FOR_UPLOAD = "WAITING_FOR_UPLOAD"
PACKAGE_UPLOADED = "PACKAGE_UPLOADED"
VALIDATING_PACKAGE = "VALIDATING_PACKAGE"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
APPLYING_REPAIR = "APPLYING_REPAIR"
WRITING_RESULT = "WRITING_RESULT"
RESTORING_NETWORK = "RESTORING_NETWORK"
COMPLETE = "COMPLETE"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"


class RecoveryController:
    def __init__(self, config: RecoveryConfig, dry_run: bool | None = None):
        if dry_run is not None:
            config.network.dry_run = dry_run
            if dry_run:
                config.api.prefer_fastapi = False
        self.config = config
        self.logger = logging.getLogger("orange_recovery.controller")
        self.network = NetworkManager(config)
        self.hotspot = RecoveryHotspot(config, self.network)
        self.service_control = ServiceControl(config.services.normal_service_name, dry_run=config.network.dry_run)
        self.package_manager = RepairPackageManager(config, self.service_control)
        self.repo_bundle_installer = RepoBundleInstaller(config)
        self.display = DisplayAnnouncer()
        self.lock = threading.RLock()
        self.state = IDLE
        self.progress_step = ""
        self.progress_percent = 0
        self.message = "Recovery is idle."
        self.session_token = ""
        self.session_password = ""
        self.ssid = ""
        self.active = False
        self.client_seen = False
        self.last_activity = 0.0
        self.uploaded_package_path = ""
        self.validation: ValidationResult | None = None
        self.result: dict[str, Any] = {"ok": False, "state": IDLE, "message": "No recovery has run."}
        self.api_server: Any = None
        self.monitor_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self, trigger_code: str = "") -> bool:
        with self.lock:
            if self.active:
                self.logger.info("recovery already active; duplicate trigger ignored")
                return False
            self.active = True
            self.stop_event.clear()
            self.session_token = generate_session_token()
            self.session_password = self.hotspot.password()
            self.state = QR_TRIGGER_RECEIVED if trigger_code else STARTING_HOTSPOT
            self.message = "Recovery trigger accepted."
            self.last_activity = time.time()
            self._write_state()

        try:
            self._set_state(STARTING_HOTSPOT, "Starting recovery hotspot.")
            self.ssid = self.hotspot.ssid()
            self.display.announce_recovery_active(
                self.ssid,
                self.session_password,
                self.config.hotspot.no_client_timeout_seconds,
                active=False,
            )
            self.ssid = self.hotspot.start(self.session_password)
            self.display.announce_recovery_active(
                self.ssid,
                self.session_password,
                self.config.hotspot.no_client_timeout_seconds,
                active=True,
            )
            self._set_state(WAITING_FOR_CLIENT, "Waiting for phone connection.")
            self._start_api()
            self.monitor_thread = threading.Thread(target=self._monitor_timeouts, name="orange-recovery-monitor", daemon=True)
            self.monitor_thread.start()
            return True
        except Exception as exc:
            self.logger.exception("failed to start recovery")
            self.result = {"ok": False, "state": FAILED, "message": str(exc)}
            self._set_state(FAILED, str(exc))
            self.exit_recovery()
            return False

    def _start_api(self) -> None:
        if not self.config.api.enabled:
            return
        from .api_server import RecoveryApiServer

        self.api_server = RecoveryApiServer(self, self.config.api.host, self.config.api.port, self.config.api.prefer_fastapi)
        self.api_server.start()

    def _monitor_timeouts(self) -> None:
        no_client_deadline = time.time() + self.config.hotspot.no_client_timeout_seconds
        while not self.stop_event.wait(2):
            now = time.time()
            with self.lock:
                if not self.active:
                    return
                state = self.state
                last_activity = self.last_activity
            if not self.client_seen:
                iface = self.network.detect_wifi_interface()
                if client_connected(iface, self.config.hotspot.ip):
                    with self.lock:
                        self.client_seen = True
                        self.state = CLIENT_CONNECTED
                        self.message = "Phone connected. Waiting for API activity."
                        self._write_state()
                    continue
                if now >= no_client_deadline and state in {WAITING_FOR_CLIENT, CLIENT_CONNECTED}:
                    self.result = {"ok": False, "state": TIMEOUT, "message": "No phone connected within timeout."}
                    self._set_state(TIMEOUT, "No phone connected within timeout.")
                    self.exit_recovery()
                    return
            elif now - last_activity > self.config.hotspot.connected_inactivity_timeout_seconds:
                self.result = {"ok": False, "state": TIMEOUT, "message": "Recovery API inactivity timeout."}
                self._set_state(TIMEOUT, "Recovery API inactivity timeout.")
                self.exit_recovery()
                return

    def record_api_activity(self) -> None:
        with self.lock:
            self.client_seen = True
            self.last_activity = time.time()
            if self.state in {WAITING_FOR_CLIENT, CLIENT_CONNECTED, API_ACTIVE}:
                self.state = WAITING_FOR_UPLOAD
                self.message = "Waiting for repair package."
            self._write_state()

    def save_and_validate_upload(self, filename: str, body: bytes) -> dict[str, Any]:
        self.record_api_activity()
        max_bytes = self.config.api.max_upload_mb * 1024 * 1024
        if len(body) > max_bytes:
            return {"ok": False, "package_valid": False, "error": "upload_too_large"}
        upload_dir = Path(self.config.paths.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "repair_package.zip").name
        path = upload_dir / f"{int(time.time())}-{safe_name}"
        path.write_bytes(body)
        os.chmod(path, 0o600)
        self.uploaded_package_path = str(path)
        self._set_state(PACKAGE_UPLOADED, "Repair package uploaded.")
        self._set_state(VALIDATING_PACKAGE, "Validating repair package.")
        self.validation = self.package_manager.validate_package(str(path))
        if self.validation.ok:
            self._set_state(WAITING_FOR_CONFIRMATION, "Repair package validated.")
        else:
            self.result = {"ok": False, "state": FAILED, "message": self.validation.error}
            self._set_state(FAILED, self.validation.error)
        return self.validation.as_response()

    def save_and_apply_repo_bundle(self, filename: str, body: bytes) -> dict[str, Any]:
        self.record_api_activity()
        max_bytes = self.config.api.max_upload_mb * 1024 * 1024
        if len(body) > max_bytes:
            return {"ok": False, "repo_bundle_valid": False, "error": "upload_too_large"}
        upload_dir = Path(self.config.paths.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "orangelite-python-scripts.zip").name
        path = upload_dir / f"{int(time.time())}-{safe_name}"
        path.write_bytes(body)
        os.chmod(path, 0o600)
        self.uploaded_package_path = str(path)
        self._set_state(PACKAGE_UPLOADED, "Orangelite Python scripts uploaded.")
        self._set_state(VALIDATING_PACKAGE, "Validating Orangelite Python scripts.")
        self._set_state(APPLYING_REPAIR, "Backing up and replacing Orangelite Python scripts.", step="install_orangelite_scripts", percent=50)
        result = self.repo_bundle_installer.install(str(path))
        payload = result.as_response()
        if result.ok:
            self.result = {
                "ok": True,
                "state": COMPLETE,
                "message": str(result.message),
                "reboot_required": False,
            }
            self._set_state(COMPLETE, self.result["message"], step="complete", percent=100)
            payload["disconnecting"] = True
            payload["disconnect_delay_seconds"] = 12
            self.exit_recovery_async(delay_seconds=12)
        else:
            self.result = {"ok": False, "state": FAILED, "message": result.error or result.message, "reboot_required": False}
            self._set_state(FAILED, self.result["message"], step="failed", percent=100)
        payload["state"] = self.state
        return payload

    def apply_repair(self, confirm: bool) -> dict[str, Any]:
        self.record_api_activity()
        if not confirm:
            return {"ok": False, "state": WAITING_FOR_CONFIRMATION, "message": "Confirmation is required."}
        if not self.uploaded_package_path or not self.validation or not self.validation.ok:
            return {"ok": False, "state": FAILED, "message": "No validated repair package is available."}
        self._set_state(APPLYING_REPAIR, "Repair started.", step="backup_current_config", percent=10)
        thread = threading.Thread(target=self._apply_repair_thread, name="orange-recovery-apply", daemon=True)
        thread.start()
        return {"ok": True, "state": APPLYING_REPAIR, "message": "Repair started"}

    def _apply_repair_thread(self) -> None:
        try:
            self._set_state(APPLYING_REPAIR, "Installing repair files.", step="install_config", percent=60)
            result = self.package_manager.apply_package(self.uploaded_package_path, self.validation)
            self._set_state(WRITING_RESULT, "Writing repair result.", step="writing_result", percent=90)
            self.result = {
                "ok": bool(result.get("ok")),
                "state": COMPLETE if result.get("ok") else FAILED,
                "message": "Repair complete. Restoring normal network." if result.get("ok") else str(result.get("message") or "Repair failed."),
                "reboot_required": False,
            }
            self._set_state(COMPLETE if result.get("ok") else FAILED, self.result["message"], step="complete", percent=100)
            self.exit_recovery_async(delay_seconds=5)
        except Exception as exc:
            self.logger.exception("repair failed")
            self.result = {"ok": False, "state": FAILED, "message": str(exc), "reboot_required": False}
            self._set_state(FAILED, str(exc), step="failed", percent=100)

    def rollback(self) -> dict[str, Any]:
        self.record_api_activity()
        result = self.package_manager.rollback()
        self.result = dict(result)
        return result

    def restart_service(self) -> dict[str, Any]:
        self.record_api_activity()
        return self.service_control.restart()

    def diagnostics_zip(self) -> str:
        self.record_api_activity()
        return make_diagnostics_zip(self.config, self.status(include_token=False))

    def exit_recovery_async(self, delay_seconds: float = 0.5) -> None:
        threading.Thread(target=self._delayed_exit, args=(delay_seconds,), name="orange-recovery-exit", daemon=True).start()

    def _delayed_exit(self, delay_seconds: float) -> None:
        time.sleep(delay_seconds)
        self.exit_recovery()

    def exit_recovery(self) -> None:
        with self.lock:
            if not self.active and self.state in {IDLE, COMPLETE, FAILED, TIMEOUT}:
                return
            self.state = RESTORING_NETWORK
            self.message = "Restoring normal network."
            self._write_state()
        self.stop_event.set()
        if self.api_server is not None:
            try:
                self.api_server.stop()
            except Exception:
                self.logger.exception("failed to stop recovery API server")
            self.api_server = None
        if self.config.network.restore_on_exit:
            try:
                self.hotspot.stop()
            except Exception:
                self.logger.exception("failed to restore network")
        with self.lock:
            self.active = False
            if self.result.get("state") not in {COMPLETE, FAILED, TIMEOUT}:
                self.result = {"ok": True, "state": COMPLETE, "message": "Recovery mode exited.", "reboot_required": False}
            self.state = str(self.result.get("state") or COMPLETE)
            self.message = str(self.result.get("message") or "Recovery mode exited.")
            self._write_state()

    def restore_network(self) -> dict[str, Any]:
        self.hotspot.stop()
        self._set_state(COMPLETE, "Normal network restored.")
        return {"ok": True, "state": COMPLETE, "message": "Normal network restored."}

    def status(self, include_token: bool = False) -> dict[str, Any]:
        with self.lock:
            payload = {
                "machine_id": self.config.machine_id,
                "state": self.state,
                "hotspot_active": self.active,
                "client_connected": self.client_seen,
                "normal_service": self.config.services.normal_service_name,
                "message": self.message,
                "ssid": self.ssid,
            }
            if include_token:
                payload["session_token"] = self.session_token
            return payload

    def progress(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "step": self.progress_step,
                "percent": self.progress_percent,
                "message": self.message,
            }

    def wait_forever(self) -> None:
        try:
            while self.active:
                time.sleep(1)
        except KeyboardInterrupt:
            self.exit_recovery()

    def _set_state(self, state: str, message: str, step: str | None = None, percent: int | None = None) -> None:
        with self.lock:
            self.state = state
            self.message = message
            if step is not None:
                self.progress_step = step
            if percent is not None:
                self.progress_percent = max(0, min(100, int(percent)))
            self._write_state()

    def _write_state(self) -> None:
        state_path = Path(self.config.paths.state_dir) / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.status(include_token=True)
        payload["updated_at"] = int(time.time())
        state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(state_path, 0o600)
