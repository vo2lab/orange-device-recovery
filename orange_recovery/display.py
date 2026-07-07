"""Display/announcement adapter.

This module intentionally does not serve HTML. It only emits safe operational
messages for a physical display, logs, or stdout.
"""

from __future__ import annotations

import logging
from pathlib import Path


class DisplayAnnouncer:
    def __init__(self, status_file: str = "/run/orange-recovery-display.txt"):
        self.status_file = Path(status_file)
        self.logger = logging.getLogger("orange_recovery.display")

    def announce_recovery_active(self, ssid: str, password: str, timeout_seconds: int, active: bool = True) -> None:
        lines = [
            "RECOVERY MODE ACTIVE" if active else "RECOVERY MODE STARTING",
            "Connect phone to:",
            ssid,
            "",
            "Password:",
            password,
            "",
            "Waiting for phone connection." if active else "Starting hotspot.",
            f"Timeout: {max(1, timeout_seconds // 60)} minutes.",
        ]
        self.write_lines(lines)

    def write_lines(self, lines: list[str]) -> None:
        message = "\n".join(lines)
        self.logger.info("%s", message.replace("\n", " | "))
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.write_text(message + "\n", encoding="utf-8")
        except OSError:
            print(message)
