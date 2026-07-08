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

1. The admin app shows the configured 8-digit recovery trigger code.
2. The dispenser QR reader scans that code before normal customer QR handling.
3. `orange_recovery.handle_scanned_qr(code)` consumes a matching trigger,
   starts a temporary hotspot, and pauses normal QR processing.
4. The phone joins `ORANGE-RECOVERY-<MACHINE_ID>`.
5. The phone opens `http://192.168.50.1:8787`.
6. The local page asks for the ZIP file and uploads it to the Pi.
7. The Pi validates the uploaded script bundle, backs up existing matching
   files, replaces them in `/home/pi/orangelite`, tells the phone to reconnect to
   normal Wi-Fi, then restores normal networking.

## Integration

```python
import orange_recovery

if orange_recovery.handle_scanned_qr(scanned_code):
    return

process_normal_customer_qr(scanned_code)
```

Only an exact configured 8-digit numeric code starts recovery. Non-matching QR
values pass through unchanged. While recovery is active, QR processing remains
paused.

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
