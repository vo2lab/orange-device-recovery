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
        if backend in {"auto", "networkmanager"} and (self.dry_run or shutil.which("nmcli")):
            self._prepare_networkmanager_wifi(iface)
            self._run(["nmcli", "connection", "delete", "OrangeRecovery"], allow_failure=True)
            try:
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
            except RuntimeError as exc:
                self.logger.warning("nmcli hotspot helper failed; trying explicit AP profile: %s", exc)
                self._run(["nmcli", "connection", "delete", "OrangeRecovery"], allow_failure=True)
                try:
                    self._run([
                        "nmcli",
                        "connection",
                        "add",
                        "type",
                        "wifi",
                        "ifname",
                        iface,
                        "con-name",
                        "OrangeRecovery",
                        "autoconnect",
                        "no",
                        "ssid",
                        ssid,
                    ])
                    self._run([
                        "nmcli",
                        "connection",
                        "modify",
                        "OrangeRecovery",
                        "802-11-wireless.mode",
                        "ap",
                        "802-11-wireless.band",
                        "bg",
                        "ipv4.method",
                        "shared",
                        "ipv4.addresses",
                        f"{ip}/24",
                        "wifi-sec.key-mgmt",
                        "wpa-psk",
                        "wifi-sec.psk",
                        password,
                    ])
                except RuntimeError as fallback_exc:
                    raise RuntimeError(self._networkmanager_hint(iface, str(fallback_exc), str(exc))) from fallback_exc
            self._run(["nmcli", "connection", "up", "OrangeRecovery"])
            return

        self._run(["ip", "link", "set", iface, "up"])
        self._run(["ip", "addr", "flush", "dev", iface])
        self._run(["ip", "addr", "add", f"{ip}/24", "dev", iface])
        raise RuntimeError("Recovery hotspot requires NetworkManager/nmcli on this device.")

    def _prepare_networkmanager_wifi(self, iface: str) -> None:
        if self.dry_run:
            self._run(["rfkill", "unblock", "wifi"], allow_failure=True)
            self._run(["nmcli", "radio", "wifi", "on"], allow_failure=True)
            self._run(["nmcli", "device", "set", iface, "managed", "yes"], allow_failure=True)
            self._run(["ip", "link", "set", iface, "up"], allow_failure=True)
            return

        if shutil.which("rfkill"):
            self._run(["rfkill", "unblock", "wifi"], allow_failure=True)
        self._run(["nmcli", "radio", "wifi", "on"], allow_failure=True)
        self._run(["nmcli", "device", "set", iface, "managed", "yes"], allow_failure=True)
        if shutil.which("ip"):
            self._run(["ip", "link", "set", iface, "up"], allow_failure=True)

        state = self._networkmanager_device_state(iface)
        if state in {"unavailable", "unmanaged"}:
            raise RuntimeError(self._networkmanager_hint(iface, f"NetworkManager reports {iface} as {state}."))

    def _networkmanager_device_state(self, iface: str) -> str:
        output = self._run_capture(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
        for line in output.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == iface:
                return parts[2].strip().lower()
        return ""

    def _networkmanager_hint(self, iface: str, error: str, original_error: str = "") -> str:
        details = error
        if original_error:
            details = f"{details}; original hotspot error: {original_error}"
        return (
            f"{details} Recovery hotspot cannot use {iface} while NetworkManager reports it unavailable. "
            f"Run: sudo rfkill unblock wifi; sudo nmcli radio wifi on; "
            f"sudo nmcli device set {iface} managed yes; sudo ip link set {iface} up; "
            "nmcli device status"
        )

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
