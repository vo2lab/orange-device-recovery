"""Service control helpers for Orange recovery."""

from __future__ import annotations

import logging
import subprocess


class ServiceControl:
    def __init__(self, service_name: str, dry_run: bool = False):
        self.service_name = service_name
        self.dry_run = dry_run
        self.logger = logging.getLogger("orange_recovery.service")

    def restart(self) -> dict[str, object]:
        return self._systemctl("restart")

    def stop(self) -> dict[str, object]:
        return self._systemctl("stop")

    def start(self) -> dict[str, object]:
        return self._systemctl("start")

    def status(self) -> str:
        if self.dry_run:
            return "dry-run"
        result = subprocess.run(
            ["systemctl", "is-active", self.service_name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip() or result.stderr.strip() or "unknown"

    def _systemctl(self, action: str) -> dict[str, object]:
        command = ["systemctl", action, self.service_name]
        self.logger.info("service command: %s", " ".join(command))
        if self.dry_run:
            return {"ok": True, "dry_run": True, "command": command}
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
