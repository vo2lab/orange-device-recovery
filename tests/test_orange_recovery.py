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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orange_recovery.api_server import RecoveryApiServer
from orange_recovery.config import RecoveryConfig
from orange_recovery.network_manager import NetworkManager
from orange_recovery.qr_trigger import RecoveryQrHandler
from orange_recovery.recovery_controller import RecoveryController
from orange_recovery.repair_package import RepairPackageManager


class FakeController:
    def __init__(self):
        self.active = False
        self.started = 0

    def start(self, trigger_code: str = "") -> bool:
        self.started += 1
        self.active = True
        self.trigger_code = trigger_code
        return True


class ActiveRecoveryNetworkManager(NetworkManager):
    def _networkmanager_connection_active(self, name: str) -> bool:
        return name == "OrangeRecovery"


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

    def test_restore_dry_run_does_not_flush_wifi_when_recovery_is_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            network = NetworkManager(cfg)
            network.restore()

            commands = [" ".join(command) for command in network.commands_run]
            self.assertIn("nmcli connection down OrangeRecovery", commands)
            self.assertIn("nmcli connection delete OrangeRecovery", commands)
            self.assertNotIn("ip addr flush dev wlan0", commands)

    def test_restore_dry_run_flushes_wifi_when_recovery_was_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            network = ActiveRecoveryNetworkManager(cfg)
            network.restore()

            commands = [" ".join(command) for command in network.commands_run]
            self.assertIn("ip addr flush dev wlan0", commands)
            self.assertIn("nmcli device connect wlan0", commands)


if __name__ == "__main__":
    unittest.main()
