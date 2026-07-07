"""Diagnostics ZIP creation for local recovery sessions."""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any

from .config import RecoveryConfig


def make_diagnostics_zip(config: RecoveryConfig, status: dict[str, Any] | None = None) -> str:
    state_dir = Path(config.paths.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"orange-recovery-diagnostics-{int(time.time())}.zip"
    safe_status = dict(status or {})
    safe_status.pop("session_token", None)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("status.json", json.dumps(safe_status, indent=2, sort_keys=True))
        log_path = Path(config.logging.log_file)
        if log_path.exists() and log_path.is_file():
            zf.write(log_path, "orange-recovery.log")
        network_state = state_dir / "network-state.json"
        if network_state.exists():
            zf.write(network_state, "network-state.json")

    os.chmod(path, 0o600)
    return str(path)
