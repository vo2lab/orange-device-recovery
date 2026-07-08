"""Configuration loading for the Orange recovery service."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/etc/orange-recovery/config.yaml"


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text[0:1] in {"'", '"'} and text[-1:] == text[0]:
        return text[1:-1]
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.lower() in {"null", "none"}:
        return None
    if text.startswith("[") and text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return [item.strip().strip("'\"") for item in text[1:-1].split(",") if item.strip()]
    try:
        return int(text)
    except ValueError:
        return text


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by /etc/orange-recovery/config.yaml."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(parent, list):
                continue
            parent.append(_scalar(line[2:]))
            continue

        if ":" not in line or not isinstance(parent, dict):
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "":
            next_container: Any = {}
            parent[key] = next_container
            stack.append((indent, next_container))
        else:
            parent[key] = _scalar(value)

        if isinstance(parent.get(key), dict):
            stack.append((indent, parent[key]))

    # Second pass for list-only blocks. This keeps the parser tiny but supports
    # allowed_target_prefixes: followed by "- /path" lines.
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.endswith(":"):
            continue
        key = stripped[:-1].strip()
        base_indent = len(raw_line) - len(raw_line.lstrip(" "))
        values: list[Any] = []
        for following in lines[index + 1:]:
            if not following.strip() or following.lstrip().startswith("#"):
                continue
            indent = len(following) - len(following.lstrip(" "))
            if indent <= base_indent:
                break
            item = following.strip()
            if item.startswith("- "):
                values.append(_scalar(item[2:]))
        if values:
            container = root
            if base_indent > 0:
                for candidate in root.values():
                    if isinstance(candidate, dict) and key in candidate:
                        candidate[key] = values
                        break
            else:
                container[key] = values

    return root


def _load_raw(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}

    try:
        import yaml  # type: ignore

        payload = yaml.safe_load(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return _parse_simple_yaml(text)


@dataclass
class RecoveryTriggerConfig:
    mode: str = "simple_code"
    simple_code: str = "00000000"
    code_length: int = 8
    require_exact_match: bool = True


@dataclass
class HotspotConfig:
    ssid_prefix: str = "ORANGE-RECOVERY"
    ip: str = "192.168.50.1"
    dhcp_start: str = "192.168.50.20"
    dhcp_end: str = "192.168.50.100"
    no_client_timeout_seconds: int = 60
    connected_inactivity_timeout_seconds: int = 1200
    password_mode: str = "configured"
    password: str = "orange1234"
    interface: str = ""


@dataclass
class ApiConfig:
    enabled: bool = True
    host: str = "192.168.50.1"
    port: int = 8787
    require_token: bool = True
    max_upload_mb: int = 50
    prefer_fastapi: bool = True


@dataclass
class RepairConfig:
    max_zip_mb: int = 50
    require_machine_match: bool = True
    allow_script_execution: bool = False
    allowed_target_prefixes: list[str] = field(default_factory=lambda: [
        "/home/pi/orangelite/config/",
        "/etc/orange/",
        "/var/lib/orange/",
    ])


@dataclass
class ServicesConfig:
    normal_service_name: str = "orange-service"


@dataclass
class NetworkConfig:
    preferred_backend: str = "auto"
    restore_on_exit: bool = True
    dry_run: bool = False


@dataclass
class LoggingConfig:
    log_file: str = "/var/log/orange-recovery.log"
    level: str = "INFO"


@dataclass
class PathsConfig:
    state_dir: str = "/var/lib/orange-recovery"
    upload_dir: str = "/var/lib/orange-recovery/uploads"
    backup_dir: str = "/var/backups/orange-recovery"


@dataclass
class RecoveryConfig:
    machine_id: str = field(default_factory=lambda: socket.gethostname().upper())
    recovery_trigger: RecoveryTriggerConfig = field(default_factory=RecoveryTriggerConfig)
    hotspot: HotspotConfig = field(default_factory=HotspotConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    repair: RepairConfig = field(default_factory=RepairConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def config_from_dict(raw: dict[str, Any]) -> RecoveryConfig:
    cfg = RecoveryConfig()
    cfg.machine_id = str(raw.get("machine_id") or cfg.machine_id).strip() or cfg.machine_id

    trigger = _section(raw, "recovery_trigger")
    cfg.recovery_trigger = RecoveryTriggerConfig(
        mode=str(trigger.get("mode") or cfg.recovery_trigger.mode),
        simple_code=str(trigger.get("simple_code") or cfg.recovery_trigger.simple_code),
        code_length=_int(trigger.get("code_length"), cfg.recovery_trigger.code_length),
        require_exact_match=_bool(trigger.get("require_exact_match"), cfg.recovery_trigger.require_exact_match),
    )

    hotspot = _section(raw, "hotspot")
    cfg.hotspot = HotspotConfig(
        ssid_prefix=str(hotspot.get("ssid_prefix") or cfg.hotspot.ssid_prefix),
        ip=str(hotspot.get("ip") or cfg.hotspot.ip),
        dhcp_start=str(hotspot.get("dhcp_start") or cfg.hotspot.dhcp_start),
        dhcp_end=str(hotspot.get("dhcp_end") or cfg.hotspot.dhcp_end),
        no_client_timeout_seconds=_int(hotspot.get("no_client_timeout_seconds"), cfg.hotspot.no_client_timeout_seconds),
        connected_inactivity_timeout_seconds=_int(hotspot.get("connected_inactivity_timeout_seconds"), cfg.hotspot.connected_inactivity_timeout_seconds),
        password_mode=str(hotspot.get("password_mode") or cfg.hotspot.password_mode),
        password=str(hotspot.get("password") or ""),
        interface=str(hotspot.get("interface") or ""),
    )

    api = _section(raw, "api")
    cfg.api = ApiConfig(
        enabled=_bool(api.get("enabled"), cfg.api.enabled),
        host=str(api.get("host") or cfg.api.host),
        port=_int(api.get("port"), cfg.api.port),
        require_token=_bool(api.get("require_token"), cfg.api.require_token),
        max_upload_mb=_int(api.get("max_upload_mb"), cfg.api.max_upload_mb),
        prefer_fastapi=_bool(api.get("prefer_fastapi"), cfg.api.prefer_fastapi),
    )

    repair = _section(raw, "repair")
    prefixes = repair.get("allowed_target_prefixes")
    cfg.repair = RepairConfig(
        max_zip_mb=_int(repair.get("max_zip_mb"), cfg.repair.max_zip_mb),
        require_machine_match=_bool(repair.get("require_machine_match"), cfg.repair.require_machine_match),
        allow_script_execution=_bool(repair.get("allow_script_execution"), cfg.repair.allow_script_execution),
        allowed_target_prefixes=[str(item) for item in prefixes] if isinstance(prefixes, list) else cfg.repair.allowed_target_prefixes,
    )

    services = _section(raw, "services")
    cfg.services = ServicesConfig(
        normal_service_name=str(services.get("normal_service_name") or cfg.services.normal_service_name)
    )

    network = _section(raw, "network")
    cfg.network = NetworkConfig(
        preferred_backend=str(network.get("preferred_backend") or cfg.network.preferred_backend),
        restore_on_exit=_bool(network.get("restore_on_exit"), cfg.network.restore_on_exit),
        dry_run=_bool(network.get("dry_run"), cfg.network.dry_run),
    )

    logging_config = _section(raw, "logging")
    cfg.logging = LoggingConfig(
        log_file=str(logging_config.get("log_file") or cfg.logging.log_file),
        level=str(logging_config.get("level") or cfg.logging.level),
    )

    paths = _section(raw, "paths")
    cfg.paths = PathsConfig(
        state_dir=str(paths.get("state_dir") or cfg.paths.state_dir),
        upload_dir=str(paths.get("upload_dir") or cfg.paths.upload_dir),
        backup_dir=str(paths.get("backup_dir") or cfg.paths.backup_dir),
    )

    return cfg


def load_config(path: str | None = None) -> RecoveryConfig:
    config_path = path or os.environ.get("ORANGE_RECOVERY_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = config_from_dict(_load_raw(config_path))

    if os.environ.get("ORANGE_RECOVERY_MACHINE_ID"):
        cfg.machine_id = os.environ["ORANGE_RECOVERY_MACHINE_ID"].strip()
    if os.environ.get("ORANGE_RECOVERY_SIMPLE_CODE"):
        cfg.recovery_trigger.simple_code = os.environ["ORANGE_RECOVERY_SIMPLE_CODE"].strip()
    if os.environ.get("ORANGE_RECOVERY_DRY_RUN"):
        cfg.network.dry_run = _bool(os.environ["ORANGE_RECOVERY_DRY_RUN"], cfg.network.dry_run)
    if os.environ.get("ORANGE_RECOVERY_API_HOST"):
        cfg.api.host = os.environ["ORANGE_RECOVERY_API_HOST"].strip()
    if os.environ.get("ORANGE_RECOVERY_API_PORT"):
        cfg.api.port = _int(os.environ["ORANGE_RECOVERY_API_PORT"], cfg.api.port)

    return cfg
