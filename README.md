# Orange Device Recovery

Orange Device Recovery is a dispenser-side, hotspot-bound recovery service for
repair packages and Orangelite Python script ZIP bundles. The o:range phone app or admin
portal owns the technician workflow. During recovery, the Raspberry Pi also
serves a minimal local upload page so a phone can transfer the ZIP after joining
the Pi hotspot.

## Install

Run on the dispenser using system Python, not a virtualenv:

```bash
curl -fsSL https://raw.githubusercontent.com/vo2lab/orange-device-recovery/main/install.sh | sudo bash
```

Optional install variables:

```bash
sudo MACHINE_ID="BEEDLES-LAKE-2" \
  RECOVERY_SIMPLE_CODE="00000000" \
  bash install.sh
```

The installer writes:

- `/opt/orange-recovery`
- `/usr/local/bin/orange-recovery`
- `/etc/orange-recovery/config.yaml`
- `/etc/systemd/system/orange-recovery.service`
- a system Python `.pth` file pointing at `/opt/orange-recovery`

No `.venv` or `.venvs` path is used.

The default recovery Wi-Fi password is:

```text
orange1234
```

Uninstall:

```bash
sudo bash uninstall.sh
```

Set `PURGE_CONFIG=1` to remove config, uploads, state, and backups.

## Flow

1. The Range repair view downloads the marked-working Orangelite Python scripts ZIP.
2. The repair view shows a QR code containing the exact text `REPAIR`.
3. `orange_main.py` scans `REPAIR`, displays `Please follow instructions on mobile`,
   and starts `orange-recovery -repair` outside the normal Orange service cgroup.
4. Repair mode stops Orange processes, starts a temporary hotspot named after the
   dispenser hostname, and waits up to 2 minutes for the phone upload.
5. The phone joins the hostname hotspot with password `orange1234`, unless the
   device config overrides `hotspot.password`.
6. The phone opens `http://192.168.50.1:8787`.
7. The local page asks for the ZIP file and uploads it to the Pi.
8. The Pi validates the uploaded script bundle, backs up existing matching
   files, replaces them in `/home/pi/orangelite`, tells the phone to reconnect to
   normal Wi-Fi, restores normal networking, and reboots.

## Integration

```python
import subprocess

if normalize_reader_code(scanned_code).upper() == "REPAIR":
    subprocess.run([
        "sudo",
        "systemd-run",
        "--unit",
        "orange-recovery-repair",
        "--collect",
        "orange-recovery",
        "-repair",
    ], check=False)
    return

process_normal_customer_qr(scanned_code)
```

The current production handoff is the command-line repair entrypoint:

```bash
sudo orange-recovery -repair
```

Only an exact `REPAIR` QR payload should call that entrypoint from the dispenser
runtime. Non-matching QR values pass through unchanged. While repair is active,
normal QR processing is stopped.

The dispenser runtime must call this before normal payment/customer QR
processing.

## Local API

- `GET /status`
- `POST /upload-repair` with multipart `file=repair_package.zip`
- `POST /upload-repo` with multipart `file=orangelite-python-scripts.zip`
- `POST /apply-repair` with `{"confirm": true}`
- `GET /progress`
- `GET /result`
- `GET /diagnostics`
- `POST /restart-service`
- `POST /rollback`
- `POST /exit-recovery`

All requests require the per-session bearer token when `api.require_token` is
enabled. The server binds only to `api.host`, which must be the hotspot IP in
production.

`GET /` serves the minimal browser upload page used by the mobile transfer flow.
The page embeds the current session token and posts the chosen Orangelite Python
scripts ZIP to `/upload-repo`.

The `/upload-repo` endpoint only accepts top-level `.py` files. It rejects nested
paths, symlinks, and non-Python files. Existing target files are backed up under
`/var/backups/orange-recovery/orangelite-scripts/` before replacement.

## Hotspot Troubleshooting

If recovery fails with `device is not available`, NetworkManager cannot use the
Wi-Fi interface. On the dispenser, run:

```bash
sudo orange-recovery restore-network
sudo rfkill unblock wifi
sudo nmcli radio wifi on
sudo nmcli device set wlan0 managed yes
sudo ip link set wlan0 up
nmcli device status
sudo orange-recovery start
```

If `wlan0` still shows `unavailable` or `unmanaged`, restart NetworkManager
from an Ethernet, local, or existing support tunnel session:

```bash
sudo systemctl restart NetworkManager
nmcli device status
```
