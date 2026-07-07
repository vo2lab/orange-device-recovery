# Orange Device Recovery

Orange Device Recovery is a dispenser-side, hotspot-bound recovery API for
repair packages. The Raspberry Pi does not serve an HTML portal, render
templates, or provide SMB shares. The o:range phone app or admin portal owns the
UI.

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
5. The phone calls `http://192.168.50.1:8787` with
   `Authorization: Bearer <session_token>`.
6. The phone uploads, validates, applies, monitors, and exits recovery through
   the JSON API.

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
