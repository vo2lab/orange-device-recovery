#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orange_recovery.api_server import RecoveryApiServer
from orange_recovery.config import RecoveryConfig, config_from_dict
from orange_recovery.display import DisplayAnnouncer
from orange_recovery.hotspot import RecoveryHotspot
from orange_recovery.network_manager import NetworkManager
from orange_recovery.qr_trigger import RecoveryQrHandler
from orange_recovery.recovery_controller import RecoveryController
from orange_recovery.repair_package import RepairPackageManager
from orange_recovery.repo_bundle import RepoBundleInstaller


class FakeController:
    def __init__(self):
        self.active = False
        self.started = 0

    def start(self, trigger_code: str = "") -> bool:
        self.started += 1
        self.active = True
        self.trigger_code = trigger_code
        return True


class OrangeRecoveryTest(unittest.TestCase):
    def config(self, tmp: str) -> RecoveryConfig:
        cfg = RecoveryConfig()
        cfg.machine_id = "BEEDLES-LAKE-2"
        cfg.recovery_trigger.simple_code = "00000000"
        cfg.network.dry_run = True
        cfg.api.host = "127.0.0.1"
        cfg.api.port = 0
        cfg.api.prefer_fastapi = False
        cfg.hotspot.interface = "wlan0"
        cfg.paths.state_dir = str(Path(tmp) / "state")
        cfg.paths.upload_dir = str(Path(tmp) / "uploads")
        cfg.paths.backup_dir = str(Path(tmp) / "backups")
        cfg.repair.allowed_target_prefixes = [str(Path(tmp) / "targets") + "/"]
        return cfg

    def repair_zip(self, path: Path, cfg: RecoveryConfig, files: dict[str, bytes] | None = None, manifest_extra: dict | None = None) -> None:
        files = files or {"config/machine.json": b'{"ok":true}\n'}
        manifest = {
            "package_type": "orange_repair",
            "version": "2026.07.07",
            "target_machine": cfg.machine_id,
            "requires_confirmation": True,
            "actions": ["backup_current_config", "install_config", "restart_orange_service"],
            "files": [
                {
                    "source": name,
                    "target": str(Path(cfg.repair.allowed_target_prefixes[0]) / Path(name).name),
                    "mode": "0644",
                }
                for name in files
            ],
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        checksums = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in files.items())
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
            zf.writestr("checksums.sha256", checksums)
            for name, body in files.items():
                zf.writestr(name, body)

    def repo_zip(self, path: Path) -> None:
        root = "orange_dispenser-working"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{root}/main.py", b"# main\n")
            zf.writestr(f"{root}/serial_reader.py", b"# serial reader\n")

    def test_qr_trigger_consumes_only_exact_configured_code(self):
        cfg = RecoveryConfig()
        fake = FakeController()
        handler = RecoveryQrHandler(cfg, controller_factory=lambda _cfg: fake)

        self.assertFalse(handler.handle_scanned_qr("12345678"))
        self.assertFalse(handler.handle_scanned_qr("0000000"))
        self.assertFalse(handler.handle_scanned_qr("00000000 "))
        self.assertEqual(fake.started, 0)

        self.assertTrue(handler.handle_scanned_qr("00000000"))
        self.assertEqual(fake.started, 1)
        self.assertEqual(fake.trigger_code, "00000000")

        self.assertTrue(handler.handle_scanned_qr("99999999"))
        self.assertEqual(fake.started, 1)

    def test_package_validation_accepts_valid_repair_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            package = Path(tmp) / "repair.zip"
            self.repair_zip(package, cfg)

            result = RepairPackageManager(cfg).validate_package(str(package))
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.package_valid)
            self.assertEqual(result.manifest["package_type"], "orange_repair")

    def test_default_hotspot_password_is_simple_configured_value(self):
        cfg = RecoveryConfig()
        self.assertEqual(RecoveryHotspot(cfg, NetworkManager(cfg, dry_run=True)).password(), "orange1234")

    def test_config_loader_keeps_default_repair_password_and_timeout(self):
        cfg = config_from_dict({})
        self.assertEqual(cfg.hotspot.password, "orange1234")
        self.assertEqual(cfg.repair.upload_timeout_seconds, 120)
        self.assertTrue(cfg.repair.reboot_on_exit)

    def test_repair_hotspot_uses_hostname_ssid(self):
        cfg = RecoveryConfig()
        with patch("orange_recovery.hotspot.socket.gethostname", return_value="orange test unit"):
            self.assertEqual(RecoveryHotspot(cfg, NetworkManager(cfg, dry_run=True)).ssid(hostname_only=True), "orange-test-unit")

    def test_repair_mode_sets_two_minute_timeout_hostname_ssid_and_display_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            cfg.api.enabled = False
            cfg.repair.upload_timeout_seconds = 120
            controller = RecoveryController(cfg)
            controller.display = DisplayAnnouncer(str(Path(tmp) / "display.txt"))
            with patch("orange_recovery.hotspot.socket.gethostname", return_value="orange-host"):
                self.assertTrue(controller.start(repair_mode=True))
            self.assertEqual(controller.ssid, "orange-host")
            self.assertEqual(cfg.hotspot.no_client_timeout_seconds, 120)
            self.assertEqual(cfg.hotspot.connected_inactivity_timeout_seconds, 120)
            self.assertTrue(controller.reboot_when_done)
            self.assertIn("Please follow instructions on mobile", (Path(tmp) / "display.txt").read_text(encoding="utf-8"))
            controller.exit_recovery()

    def test_package_validation_rejects_malicious_zip_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            package = Path(tmp) / "repair.zip"
            files = {"../machine.json": b"bad"}
            self.repair_zip(package, cfg, files=files)

            result = RepairPackageManager(cfg).validate_package(str(package))
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "unsafe_zip_path")

    def test_package_validation_rejects_wrong_machine_and_bad_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            wrong_machine = Path(tmp) / "wrong.zip"
            self.repair_zip(wrong_machine, cfg, manifest_extra={"target_machine": "OTHER"})
            self.assertEqual(RepairPackageManager(cfg).validate_package(str(wrong_machine)).error, "wrong_machine")

            bad_checksum = Path(tmp) / "bad-checksum.zip"
            manifest = {
                "package_type": "orange_repair",
                "version": "2026.07.07",
                "target_machine": cfg.machine_id,
                "actions": ["install_config"],
                "files": [{
                    "source": "config/machine.json",
                    "target": str(Path(cfg.repair.allowed_target_prefixes[0]) / "machine.json"),
                    "mode": "0644",
                }],
            }
            with zipfile.ZipFile(bad_checksum, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest))
                zf.writestr("checksums.sha256", "0" * 64 + "  config/machine.json\n")
                zf.writestr("config/machine.json", b"not matching")
            self.assertEqual(RepairPackageManager(cfg).validate_package(str(bad_checksum)).error, "checksum_mismatch")

    def test_api_requires_bearer_token_and_reports_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            controller = RecoveryController(cfg)
            controller.active = True
            controller.session_token = "test-token"
            controller.state = "WAITING_FOR_UPLOAD"
            server = RecoveryApiServer(controller, "127.0.0.1", 0, prefer_fastapi=False)
            server.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request("GET", "/status")
                self.assertEqual(conn.getresponse().status, 401)
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request("GET", "/status", headers={"Authorization": "Bearer test-token"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["machine_id"], cfg.machine_id)
                self.assertEqual(payload["state"], "WAITING_FOR_UPLOAD")
                conn.close()
            finally:
                server.stop()

    def test_api_upload_validates_repair_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            package = Path(tmp) / "repair.zip"
            self.repair_zip(package, cfg)
            controller = RecoveryController(cfg)
            controller.active = True
            controller.session_token = "upload-token"
            server = RecoveryApiServer(controller, "127.0.0.1", 0, prefer_fastapi=False)
            server.start()
            try:
                boundary = "----orange-recovery-test"
                body = io.BytesIO()
                body.write(f"--{boundary}\r\n".encode("ascii"))
                body.write(b'Content-Disposition: form-data; name="file"; filename="repair_package.zip"\r\n')
                body.write(b"Content-Type: application/zip\r\n\r\n")
                body.write(package.read_bytes())
                body.write(f"\r\n--{boundary}--\r\n".encode("ascii"))

                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request(
                    "POST",
                    "/upload-repair",
                    body=body.getvalue(),
                    headers={
                        "Authorization": "Bearer upload-token",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"], payload)
                self.assertTrue(payload["package_valid"], payload)
                conn.close()
            finally:
                server.stop()

    def test_api_serves_upload_page_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            controller = RecoveryController(cfg)
            controller.active = True
            controller.session_token = "page-token"
            server = RecoveryApiServer(controller, "127.0.0.1", 0, prefer_fastapi=False)
            server.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request("GET", "/")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                html = response.read().decode("utf-8")
                self.assertIn("Orange Recovery Upload", html)
                self.assertIn("/upload-repo", html)
                conn.close()
            finally:
                server.stop()

    def test_api_uploads_orangelite_script_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            package = Path(tmp) / "orangelite-python-scripts.zip"
            self.repo_zip(package)
            controller = RecoveryController(cfg)
            controller.active = True
            controller.session_token = "repo-token"
            server = RecoveryApiServer(controller, "127.0.0.1", 0, prefer_fastapi=False)
            server.start()
            try:
                boundary = "----orange-recovery-repo-test"
                body = io.BytesIO()
                body.write(f"--{boundary}\r\n".encode("ascii"))
                body.write(b'Content-Disposition: form-data; name="file"; filename="orangelite-python-scripts.zip"\r\n')
                body.write(b"Content-Type: application/zip\r\n\r\n")
                body.write(package.read_bytes())
                body.write(f"\r\n--{boundary}--\r\n".encode("ascii"))

                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request(
                    "POST",
                    "/upload-repo",
                    body=body.getvalue(),
                    headers={
                        "Authorization": "Bearer repo-token",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"], payload)
                self.assertTrue(payload["repo_bundle_valid"], payload)
                self.assertEqual(payload["runtime_bundle_valid"], True)
                self.assertFalse(payload["installed"], payload)
                self.assertEqual(payload["installed_files"], ["main.py", "serial_reader.py"])
                conn.close()
            finally:
                server.stop()

    def test_orangelite_script_bundle_backs_up_and_replaces_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            cfg.network.dry_run = False
            cfg.repair.orangelite_root = str(Path(tmp) / "orangelite")
            target_root = Path(cfg.repair.orangelite_root)
            target_root.mkdir(parents=True)
            (target_root / "main.py").write_text("old main\n", encoding="utf-8")
            package = Path(tmp) / "orangelite-python-scripts.zip"

            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("main.py", "new main\n")
                zf.writestr("serial_reader.py", "new reader\n")

            result = RepoBundleInstaller(cfg).install(str(package))

            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.installed)
            self.assertEqual((target_root / "main.py").read_text(encoding="utf-8"), "new main\n")
            self.assertEqual((target_root / "serial_reader.py").read_text(encoding="utf-8"), "new reader\n")
            backups = list((Path(cfg.paths.backup_dir) / "orangelite-scripts").glob("*/main.py"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old main\n")

    def test_orangelite_script_bundle_rejects_nested_or_non_python_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            nested = Path(tmp) / "nested.zip"
            with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("orange_dispenser-working/main.py", "# ok\n")
                zf.writestr("orange_dispenser-working/scripts/worker.py", "# nested\n")
            self.assertEqual(RepoBundleInstaller(cfg).install(str(nested)).error, "nested_file_rejected")

            non_python = Path(tmp) / "non-python.zip"
            with zipfile.ZipFile(non_python, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("main.py", "# ok\n")
                zf.writestr("README.md", "not allowed\n")
            self.assertEqual(RepoBundleInstaller(cfg).install(str(non_python)).error, "non_python_file_rejected")

    def test_hotspot_dry_run_prepares_networkmanager_wifi(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            network = NetworkManager(cfg)
            network.start_hotspot("ORANGE-RECOVERY-TEST", "Password12345")

            commands = [" ".join(command) for command in network.commands_run]
            self.assertIn("rfkill unblock wifi", commands)
            self.assertIn("nmcli radio wifi on", commands)
            self.assertIn("nmcli device set wlan0 managed yes", commands)
            self.assertIn("ip link set wlan0 up", commands)
            self.assertTrue(any("nmcli device wifi hotspot" in command for command in commands))

    def test_restore_dry_run_does_not_flush_wifi(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            network = NetworkManager(cfg)
            network.restore()

            commands = [" ".join(command) for command in network.commands_run]
            self.assertIn("nmcli connection down OrangeRecovery", commands)
            self.assertIn("nmcli connection delete OrangeRecovery", commands)
            self.assertNotIn("ip addr flush dev wlan0", commands)
            self.assertIn("nmcli radio wifi on", commands)
            self.assertIn("nmcli device set wlan0 managed yes", commands)
            self.assertIn("nmcli device connect wlan0", commands)


if __name__ == "__main__":
    unittest.main()
