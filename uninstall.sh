#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/opt/orange-recovery}"
CONFIG_DIR="${CONFIG_DIR:-/etc/orange-recovery}"
PURGE_CONFIG="${PURGE_CONFIG:-0}"

[[ "$(id -u)" -eq 0 ]] || {
  echo "ERROR: Run as root with sudo." >&2
  exit 1
}

systemctl stop orange-recovery.service >/dev/null 2>&1 || true
systemctl disable orange-recovery.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/orange-recovery.service
rm -f /usr/local/bin/orange-recovery
rm -rf "${INSTALL_ROOT}"

site_dir="$(python3 - <<'PY'
import sysconfig
print(sysconfig.get_paths().get("purelib", ""))
PY
)"
if [[ -n "${site_dir}" ]]; then
  rm -f "${site_dir}/orange_recovery.pth"
fi

if [[ "${PURGE_CONFIG}" == "1" ]]; then
  rm -rf "${CONFIG_DIR}" /var/lib/orange-recovery /var/backups/orange-recovery
fi

systemctl daemon-reload >/dev/null 2>&1 || true
echo "Orange Device Recovery uninstalled."
