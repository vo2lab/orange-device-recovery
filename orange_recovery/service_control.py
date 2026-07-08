"""Service control helpers for Orange recovery."""

from __future__ import annotations

import logging
import shutil
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

    def stop_for_repair(self) -> dict[str, object]:
        commands = [
            ["systemctl", "stop", self.service_name],
            ["systemctl", "stop", "range-orange-service-watchdog.service"],
            ["systemctl", "stop", "range-orange-service-watchdog.timer"],
            ["systemctl", "stop", "orange-service-watchdog.service"],
            ["systemctl", "stop", "orange-service-watchdog.timer"],
        ]
        patterns = [
            "orange-service-runner.sh",
            "orange_main.py",
            "orangelitesol.py",
            "orangeliterock.py",
            "orangelitesolrock.py",
            "orangeliterocksol.py",
            "orange_tasks.py",
            "orange_remote.py",
            "orange_vend_receiver.py",
            "orange_coinmonitor.py",
            "orange_nayaxmonitor.py",
            "orange_emp800.py",
            "displayOnOff.py",
            "externalPayment.py",
            "getProcesses.py",
        ]
        results = [self._run(command, check=False) for command in commands]
        if shutil.which("pkill") or self.dry_run:
            for pattern in patterns:
                results.append(self._run(["pkill", "-f", pattern], check=False))
        return {"ok": True, "results": results}

    def reboot(self) -> dict[str, object]:
        return self._run(["systemctl", "reboot"], check=False)

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
        return self._run(["systemctl", action, self.service_name], check=True)

    def _run(self, command: list[str], check: bool = False) -> dict[str, object]:
        self.logger.info("service command: %s", " ".join(command))
        if self.dry_run:
            return {"ok": True, "dry_run": True, "command": command}
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return {
            "ok": result.returncode == 0 or not check,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
