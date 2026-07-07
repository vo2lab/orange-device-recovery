"""Hotspot session helpers."""

from __future__ import annotations

from .config import RecoveryConfig
from .network_manager import NetworkManager
from .security import generate_hotspot_password


class RecoveryHotspot:
    def __init__(self, config: RecoveryConfig, network: NetworkManager):
        self.config = config
        self.network = network

    def ssid(self) -> str:
        machine = self.config.machine_id.replace(" ", "-").upper()
        return f"{self.config.hotspot.ssid_prefix}-{machine}"

    def password(self) -> str:
        if self.config.hotspot.password_mode == "configured" and self.config.hotspot.password:
            return self.config.hotspot.password
        return generate_hotspot_password()

    def start(self, password: str) -> str:
        ssid = self.ssid()
        self.network.start_hotspot(ssid, password)
        return ssid

    def stop(self) -> None:
        self.network.restore()
