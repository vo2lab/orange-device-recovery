"""Client detection for the temporary recovery hotspot."""

from __future__ import annotations

import shutil
import subprocess


def client_connected(interface: str, hotspot_ip: str = "192.168.50.1") -> bool:
    if not interface or not shutil.which("ip"):
        return False
    result = subprocess.run(
        ["ip", "neigh", "show", "dev", interface],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] == hotspot_ip:
            continue
        if any(state in parts for state in ("REACHABLE", "STALE", "DELAY", "PROBE")):
            return True
    return False
