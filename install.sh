#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_URL="${REPO_RAW_URL:-https://raw.githubusercontent.com/vo2lab/orange-device-recovery/main}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/orange-recovery}"
CONFIG_DIR="${CONFIG_DIR:-/etc/orange-recovery}"
CONFIG_FILE="${CONFIG_FILE:-${CONFIG_DIR}/config.yaml}"
MACHINE_ID="${MACHINE_ID:-${DEVICE_NAME:-$(hostname -s 2>/dev/null || hostname)}}"
RECOVERY_SIMPLE_CODE="${RECOVERY_SIMPLE_CODE:-${ORANGE_RECOVERY_SIMPLE_CODE:-00000000}}"
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
  require_token: true
  max_upload_mb: 50
  prefer_fastapi: true

repair:
  max_zip_mb: 50
  require_machine_match: true
  allow_script_execution: false
  orangelite_root: "/home/pi/orangelite"
  upload_timeout_seconds: 120
  reboot_on_exit: true
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
DONE
