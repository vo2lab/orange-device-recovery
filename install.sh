#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_URL="${REPO_RAW_URL:-https://raw.githubusercontent.com/vo2lab/orange-device-recovery/main}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/orange-recovery}"
CONFIG_DIR="${CONFIG_DIR:-/etc/orange-recovery}"
CONFIG_FILE="${CONFIG_FILE:-${CONFIG_DIR}/config.yaml}"
MACHINE_ID="${MACHINE_ID:-${DEVICE_NAME:-$(hostname -s 2>/dev/null || hostname)}}"
RECOVERY_SIMPLE_CODE="${RECOVERY_SIMPLE_CODE:-${ORANGE_RECOVERY_SIMPLE_CODE:-00000000}}"
RECOVERY_PUBLIC_HOSTNAME="${RECOVERY_PUBLIC_HOSTNAME:-${ORANGE_RECOVERY_PUBLIC_HOSTNAME:-recovery.o-range.golf}}"
RECOVERY_PUBLIC_URL="${RECOVERY_PUBLIC_URL:-${ORANGE_RECOVERY_PUBLIC_URL:-https://${RECOVERY_PUBLIC_HOSTNAME}:8787/}}"
TLS_CERT_FILE="${TLS_CERT_FILE:-${ORANGE_RECOVERY_TLS_CERT_FILE:-${CONFIG_DIR}/tls/${RECOVERY_PUBLIC_HOSTNAME}/fullchain.pem}}"
TLS_KEY_FILE="${TLS_KEY_FILE:-${ORANGE_RECOVERY_TLS_KEY_FILE:-${CONFIG_DIR}/tls/${RECOVERY_PUBLIC_HOSTNAME}/privkey.pem}}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<USAGE
Install Orange Device Recovery on a dispenser.

Optional environment:
  REPO_RAW_URL="https://raw.githubusercontent.com/vo2lab/orange-device-recovery/main"
  MACHINE_ID="$(hostname -s 2>/dev/null || hostname)"
  RECOVERY_SIMPLE_CODE="00000000"
  RECOVERY_PUBLIC_HOSTNAME="recovery.o-range.golf"
  RECOVERY_PUBLIC_URL="https://recovery.o-range.golf:8787/"
  INSTALL_ROOT="/opt/orange-recovery"
  CONFIG_FILE="/etc/orange-recovery/config.yaml"

Example:
  curl -fsSL https://raw.githubusercontent.com/vo2lab/orange-device-recovery/main/install.sh | sudo bash
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || fail "Run as root with sudo."
command -v python3 >/dev/null 2>&1 || fail "python3 is required."

if [[ ! "${RECOVERY_SIMPLE_CODE}" =~ ^[0-9]{8}$ ]]; then
  fail "RECOVERY_SIMPLE_CODE must be exactly 8 digits."
fi

if [[ ! -d "${SOURCE_DIR}/orange_recovery" ]]; then
  command -v curl >/dev/null 2>&1 || fail "curl is required when installing from REPO_RAW_URL."
fi

files=(
  "README.md"
  "config.example.yaml"
  "orange_recovery/__init__.py"
  "orange_recovery/api_server.py"
  "orange_recovery/cli.py"
  "orange_recovery/client_detection.py"
  "orange_recovery/config.py"
  "orange_recovery/diagnostics.py"
  "orange_recovery/display.py"
  "orange_recovery/hotspot.py"
  "orange_recovery/network_manager.py"
  "orange_recovery/qr_trigger.py"
  "orange_recovery/recovery_controller.py"
  "orange_recovery/repair_package.py"
  "orange_recovery/repo_bundle.py"
  "orange_recovery/rollback.py"
  "orange_recovery/security.py"
  "orange_recovery/service_control.py"
  "scripts/orange-recovery"
  "systemd/orange-recovery.service"
)

install_one() {
  local rel_path="$1"
  local mode="$2"
  local target="${INSTALL_ROOT}/${rel_path}"
  local tmp_file=""

  install -d -m 755 "$(dirname "${target}")"
  if [[ -f "${SOURCE_DIR}/${rel_path}" ]]; then
    install -m "${mode}" "${SOURCE_DIR}/${rel_path}" "${target}"
    return
  fi

  tmp_file="$(mktemp)"
  curl -fsSL "${REPO_RAW_URL%/}/${rel_path}" -o "${tmp_file}"
  install -m "${mode}" "${tmp_file}" "${target}"
  rm -f "${tmp_file}"
}

install -d -m 755 "${INSTALL_ROOT}" "${INSTALL_ROOT}/orange_recovery" "${INSTALL_ROOT}/scripts" "${INSTALL_ROOT}/systemd"

for file in "${files[@]}"; do
  mode="644"
  case "${file}" in
    scripts/orange-recovery)
      mode="755"
      ;;
  esac
  install_one "${file}" "${mode}"
done

ln -sf "${INSTALL_ROOT}/scripts/orange-recovery" /usr/local/bin/orange-recovery
install -m 644 "${INSTALL_ROOT}/systemd/orange-recovery.service" /etc/systemd/system/orange-recovery.service

install -d -m 700 "${CONFIG_DIR}" /var/lib/orange-recovery /var/lib/orange-recovery/uploads /var/backups/orange-recovery

if [[ ! -f "${CONFIG_FILE}" ]]; then
  MACHINE_ID="${MACHINE_ID}" RECOVERY_SIMPLE_CODE="${RECOVERY_SIMPLE_CODE}" CONFIG_FILE="${CONFIG_FILE}" python3 - <<'PY'
import os
from pathlib import Path

machine_id = os.environ["MACHINE_ID"].replace('"', "")
simple_code = os.environ["RECOVERY_SIMPLE_CODE"]
config_file = Path(os.environ["CONFIG_FILE"])
config_file.write_text(f'''machine_id: "{machine_id}"

recovery_trigger:
  mode: "simple_code"
  simple_code: "{simple_code}"
  code_length: 8
  require_exact_match: true

hotspot:
  ssid_prefix: "ORANGE-RECOVERY"
  ip: "192.168.50.1"
  dhcp_start: "192.168.50.20"
  dhcp_end: "192.168.50.100"
  no_client_timeout_seconds: 60
  connected_inactivity_timeout_seconds: 1200
  password_mode: "configured"
  password: "orange1234"

api:
  enabled: true
  host: "192.168.50.1"
  port: 8787
  public_hostname: "recovery.o-range.golf"
  public_url: "https://recovery.o-range.golf:8787/"
  require_token: true
  max_upload_mb: 50
  prefer_fastapi: true
  tls_enabled: true
  tls_cert_file: "/etc/orange-recovery/tls/recovery.o-range.golf/fullchain.pem"
  tls_key_file: "/etc/orange-recovery/tls/recovery.o-range.golf/privkey.pem"

repair:
  max_zip_mb: 50
  require_machine_match: true
  allow_script_execution: false
  allowed_target_prefixes:
    - "/home/pi/orangelite/config/"
    - "/etc/orange/"
    - "/var/lib/orange/"

services:
  normal_service_name: "orange-service"

network:
  preferred_backend: "auto"
  restore_on_exit: true
  dry_run: false

logging:
  log_file: "/var/log/orange-recovery.log"
  level: "INFO"

paths:
  state_dir: "/var/lib/orange-recovery"
  upload_dir: "/var/lib/orange-recovery/uploads"
  backup_dir: "/var/backups/orange-recovery"
''', encoding="utf-8")
PY
  chmod 600 "${CONFIG_FILE}"
fi

PYTHONPATH="${INSTALL_ROOT}" CONFIG_FILE="${CONFIG_FILE}" RECOVERY_PUBLIC_HOSTNAME="${RECOVERY_PUBLIC_HOSTNAME}" RECOVERY_PUBLIC_URL="${RECOVERY_PUBLIC_URL}" TLS_CERT_FILE="${TLS_CERT_FILE}" TLS_KEY_FILE="${TLS_KEY_FILE}" python3 - <<'PY'
import os
from pathlib import Path

from orange_recovery.config import load_config


def quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def boolean(value: bool) -> str:
    return "true" if value else "false"


config_file = Path(os.environ["CONFIG_FILE"])
cfg = load_config(str(config_file))
cfg.api.public_hostname = os.environ["RECOVERY_PUBLIC_HOSTNAME"].strip() or cfg.api.public_hostname
cfg.api.public_url = os.environ["RECOVERY_PUBLIC_URL"].strip() or cfg.api.public_url
cfg.api.tls_cert_file = os.environ["TLS_CERT_FILE"].strip() or cfg.api.tls_cert_file
cfg.api.tls_key_file = os.environ["TLS_KEY_FILE"].strip() or cfg.api.tls_key_file

lines = [
    f"machine_id: {quote(cfg.machine_id)}",
    "",
    "recovery_trigger:",
    f"  mode: {quote(cfg.recovery_trigger.mode)}",
    f"  simple_code: {quote(cfg.recovery_trigger.simple_code)}",
    f"  code_length: {cfg.recovery_trigger.code_length}",
    f"  require_exact_match: {boolean(cfg.recovery_trigger.require_exact_match)}",
    "",
    "hotspot:",
    f"  ssid_prefix: {quote(cfg.hotspot.ssid_prefix)}",
    f"  ip: {quote(cfg.hotspot.ip)}",
    f"  dhcp_start: {quote(cfg.hotspot.dhcp_start)}",
    f"  dhcp_end: {quote(cfg.hotspot.dhcp_end)}",
    f"  no_client_timeout_seconds: {cfg.hotspot.no_client_timeout_seconds}",
    f"  connected_inactivity_timeout_seconds: {cfg.hotspot.connected_inactivity_timeout_seconds}",
    f"  password_mode: {quote(cfg.hotspot.password_mode)}",
    f"  password: {quote(cfg.hotspot.password)}",
    f"  interface: {quote(cfg.hotspot.interface)}",
    "",
    "api:",
    f"  enabled: {boolean(cfg.api.enabled)}",
    f"  host: {quote(cfg.api.host)}",
    f"  port: {cfg.api.port}",
    f"  public_hostname: {quote(cfg.api.public_hostname)}",
    f"  public_url: {quote(cfg.api.public_url)}",
    f"  require_token: {boolean(cfg.api.require_token)}",
    f"  max_upload_mb: {cfg.api.max_upload_mb}",
    f"  prefer_fastapi: {boolean(cfg.api.prefer_fastapi)}",
    f"  tls_enabled: {boolean(cfg.api.tls_enabled)}",
    f"  tls_cert_file: {quote(cfg.api.tls_cert_file)}",
    f"  tls_key_file: {quote(cfg.api.tls_key_file)}",
    "",
    "repair:",
    f"  max_zip_mb: {cfg.repair.max_zip_mb}",
    f"  require_machine_match: {boolean(cfg.repair.require_machine_match)}",
    f"  allow_script_execution: {boolean(cfg.repair.allow_script_execution)}",
    "  allowed_target_prefixes:",
]
lines.extend(f"    - {quote(prefix)}" for prefix in cfg.repair.allowed_target_prefixes)
lines.extend([
    "",
    "services:",
    f"  normal_service_name: {quote(cfg.services.normal_service_name)}",
    "",
    "network:",
    f"  preferred_backend: {quote(cfg.network.preferred_backend)}",
    f"  restore_on_exit: {boolean(cfg.network.restore_on_exit)}",
    f"  dry_run: {boolean(cfg.network.dry_run)}",
    "",
    "logging:",
    f"  log_file: {quote(cfg.logging.log_file)}",
    f"  level: {quote(cfg.logging.level)}",
    "",
    "paths:",
    f"  state_dir: {quote(cfg.paths.state_dir)}",
    f"  upload_dir: {quote(cfg.paths.upload_dir)}",
    f"  backup_dir: {quote(cfg.paths.backup_dir)}",
    "",
])
config_file.write_text("\n".join(lines), encoding="utf-8")
PY
chmod 600 "${CONFIG_FILE}"

tls_source_dir="${SOURCE_DIR}/tls/${RECOVERY_PUBLIC_HOSTNAME}"
if [[ -f "${tls_source_dir}/fullchain.pem" && -f "${tls_source_dir}/privkey.pem" ]]; then
  install -d -m 700 "$(dirname "${TLS_CERT_FILE}")" "$(dirname "${TLS_KEY_FILE}")"
  install -m 644 "${tls_source_dir}/fullchain.pem" "${TLS_CERT_FILE}"
  install -m 600 "${tls_source_dir}/privkey.pem" "${TLS_KEY_FILE}"
fi

install -d -m 755 /etc/NetworkManager/dnsmasq-shared.d
PYTHONPATH="${INSTALL_ROOT}" CONFIG_FILE="${CONFIG_FILE}" python3 - <<'PY' > /etc/NetworkManager/dnsmasq-shared.d/orange-recovery.conf
import os

from orange_recovery.config import load_config

cfg = load_config(os.environ["CONFIG_FILE"])
hostname = cfg.api.public_hostname.strip().rstrip(".")
if hostname:
    print(f"address=/{hostname}/{cfg.hotspot.ip}")
PY
chmod 644 /etc/NetworkManager/dnsmasq-shared.d/orange-recovery.conf

site_dir="$(python3 - <<'PY'
import sysconfig
print(sysconfig.get_paths().get("purelib", ""))
PY
)"
if [[ -n "${site_dir}" ]]; then
  install -d -m 755 "${site_dir}"
  printf '%s\n' "${INSTALL_ROOT}" > "${site_dir}/orange_recovery.pth"
fi

python3 -m py_compile "${INSTALL_ROOT}"/orange_recovery/*.py
systemctl daemon-reload >/dev/null 2>&1 || true

cat <<DONE
Orange Device Recovery installed.

CLI: /usr/local/bin/orange-recovery
Config: ${CONFIG_FILE}
Systemd unit: orange-recovery.service (installed but not enabled)
System Python path: ${site_dir}/orange_recovery.pth
API bind address during recovery: 192.168.50.1:8787
Recovery URL during hotspot: ${RECOVERY_PUBLIC_URL}
DONE
