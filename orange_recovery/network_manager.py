"""Network and hotspot control with dry-run support."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import RecoveryConfig


class NetworkManager:
    def __init__(self, config: RecoveryConfig, dry_run: bool | None = None):
        self.config = config
        self.dry_run = config.network.dry_run if dry_run is None else dry_run
        self.logger = logging.getLogger("orange_recovery.network")
        self.state_file = Path(config.paths.state_dir) / "network-state.json"
        self.commands_run: list[list[str]] = []

    def save_current_state(self) -> dict[str, Any]:
        state = {
            "saved_at": int(time.time()),
            "dry_run": self.dry_run,
            "hostname": self._run_capture(["hostname"]),
            "ip_addr": self._run_capture(["ip", "-json", "addr"]),
            "ip_route": self._run_capture(["ip", "-json", "route"]),
            "nmcli_connections": self._run_capture(["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"]),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(self.state_file, 0o600)
        return state

    def start_hotspot(self, ssid: str, password: str) -> None:
        self.save_current_state()
        iface = self.config.hotspot.interface or self.detect_wifi_interface()
        if not iface:
            raise RuntimeError("No wireless interface was detected for recovery hotspot.")

        ip = self.config.hotspot.ip
        backend = self.config.network.preferred_backend
        if backend in {"auto", "networkmanager"} and shutil.which("nmcli"):
            self._run(["nmcli", "connection", "delete", "OrangeRecovery"], allow_failure=True)
            self._run([
                "nmcli",
                "device",
                "wifi",
                "hotspot",
                "ifname",
                iface,
                "con-name",
                "OrangeRecovery",
                "ssid",
                ssid,
                "password",
                password,
            ])
            self._run(["nmcli", "connection", "modify", "OrangeRecovery", "ipv4.method", "shared", "ipv4.addresses", f"{ip}/24"])
            self._run(["nmcli", "connection", "up", "OrangeRecovery"])
            return

        self._run(["ip", "link", "set", iface, "up"])
        self._run(["ip", "addr", "flush", "dev", iface])
        self._run(["ip", "addr", "add", f"{ip}/24", "dev", iface])
        raise RuntimeError("Recovery hotspot requires NetworkManager/nmcli on this device.")

    def restore(self) -> None:
        iface = self.config.hotspot.interface or self.detect_wifi_interface()
        if shutil.which("nmcli"):
            self._run(["nmcli", "connection", "down", "OrangeRecovery"], allow_failure=True)
            self._run(["nmcli", "connection", "delete", "OrangeRecovery"], allow_failure=True)
        if iface:
            self._run(["ip", "addr", "flush", "dev", iface], allow_failure=True)
        if shutil.which("systemctl"):
            self._run(["systemctl", "restart", "NetworkManager"], allow_failure=True)

    def detect_wifi_interface(self) -> str:
        env_iface = os.environ.get("ORANGE_RECOVERY_WIFI_IFACE", "").strip()
        if env_iface:
            return env_iface
        wireless_dir = Path("/sys/class/net")
        try:
            for candidate in sorted(wireless_dir.iterdir()):
                if (candidate / "wireless").exists():
                    return candidate.name
        except OSError:
            return ""
        return ""

    def _run_capture(self, command: list[str]) -> str:
        if self.dry_run or not shutil.which(command[0]):
            return ""
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.strip() or result.stderr.strip()

    def _run(self, command: list[str], allow_failure: bool = False) -> None:
        self.commands_run.append(command)
        self.logger.info("network command: %s", " ".join(command))
        if self.dry_run:
            return
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0 and not allow_failure:
            raise RuntimeError(f"Network command failed: {' '.join(command)}: {result.stderr.strip()}")
