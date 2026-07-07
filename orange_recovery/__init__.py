"""Orange dispenser local recovery API package."""

from __future__ import annotations

from .qr_trigger import RecoveryQrHandler, configure_default_handler, handle_scanned_qr

__all__ = ["RecoveryQrHandler", "configure_default_handler", "handle_scanned_qr"]
__version__ = "2026.07.07.1"
