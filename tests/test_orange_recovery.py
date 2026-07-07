#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orange_recovery.api_server import RecoveryApiServer
from orange_recovery.config import RecoveryConfig, config_from_dict
from orange_recovery.hotspot import RecoveryHotspot
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
        root = "orange-device-recovery-main"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{root}/install.sh", b"#!/usr/bin/env bash\nexit 0\n")
            zf.writestr(f"{root}/orange_recovery/api_server.py", b"# api\n")
            zf.writestr(f"{root}/orange_recovery/recovery_controller.py", b"# controller\n")

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

    def test_api_tls_config_loads(self):
        cfg = config_from_dict({
            "api": {
                "public_hostname": "recovery.example.test",
                "public_url": "https://recovery.example.test:8787/",
                "tls_enabled": True,
                "tls_cert_file": "/tmp/fullchain.pem",
                "tls_key_file": "/tmp/privkey.pem",
            }
        })

        self.assertEqual(cfg.api.public_hostname, "recovery.example.test")
        self.assertEqual(cfg.api.public_url, "https://recovery.example.test:8787/")
        self.assertTrue(cfg.api.tls_enabled)
        self.assertEqual(cfg.api.tls_cert_file, "/tmp/fullchain.pem")
        self.assertEqual(cfg.api.tls_key_file, "/tmp/privkey.pem")

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

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for temporary TLS cert generation")
    def test_builtin_api_serves_https_with_tls_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-subj",
                    "/CN=localhost",
                    "-days",
                    "1",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            cfg = self.config(tmp)
            controller = RecoveryController(cfg)
            controller.active = True
            controller.session_token = "page-token"
            server = RecoveryApiServer(
                controller,
                "127.0.0.1",
                0,
                prefer_fastapi=False,
                tls_enabled=True,
                tls_cert_file=str(cert),
                tls_key_file=str(key),
            )
            server.start()
            try:
                context = ssl._create_unverified_context()
                conn = http.client.HTTPSConnection("127.0.0.1", server.port, timeout=5, context=context)
                conn.request("GET", "/")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("Orange Recovery Upload", response.read().decode("utf-8"))
                conn.close()
            finally:
                server.stop()

    def test_api_uploads_repo_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp)
            package = Path(tmp) / "orange-device-recovery.zip"
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
                body.write(b'Content-Disposition: form-data; name="file"; filename="orange-device-recovery.zip"\r\n')
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
                self.assertFalse(payload["installed"], payload)
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
            self.assertIn(
                "write /etc/NetworkManager/dnsmasq-shared.d/orange-recovery.conf address=/recovery.o-range.golf/192.168.50.1",
                commands,
            )
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
