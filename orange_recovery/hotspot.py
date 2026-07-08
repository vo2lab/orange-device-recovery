"""Hotspot session helpers."""

from __future__ import annotations

import socket

from .config import RecoveryConfig
from .network_manager import NetworkManager
from .security import generate_hotspot_password


class RecoveryHotspot:
    def __init__(self, config: RecoveryConfig, network: NetworkManager):
        self.config = config
        self.network = network

    def ssid(self, hostname_only: bool = False) -> str:
        if hostname_only:
            return self._safe_ssid(socket.gethostname() or self.config.machine_id)
        machine = self._safe_ssid(self.config.machine_id).upper()
        return self._safe_ssid(f"{self.config.hotspot.ssid_prefix}-{machine}")

    def password(self) -> str:
        if self.config.hotspot.password_mode == "configured" and self.config.hotspot.password:
            return self.config.hotspot.password
        return generate_hotspot_password()

    def start(self, password: str, ssid: str | None = None) -> str:
        ssid = self._safe_ssid(ssid or self.ssid())
        self.network.start_hotspot(ssid, password)
        return ssid

    def stop(self) -> None:
        self.network.restore()

    def _safe_ssid(self, value: str) -> str:
        safe = "-".join(str(value or "").strip().split())
        if not safe:
            safe = "ORANGE-RECOVERY"
        return safe[:32]
