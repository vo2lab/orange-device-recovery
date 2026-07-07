"""Recovery QR trigger integration.

Call handle_scanned_qr() before the normal payment/customer QR path.
"""

from __future__ import annotations

import logging
from typing import Callable

from .config import RecoveryConfig, load_config
from .recovery_controller import RecoveryController


class RecoveryQrHandler:
    def __init__(
        self,
        config: RecoveryConfig,
        controller_factory: Callable[[RecoveryConfig], RecoveryController] | None = None,
    ):
        self.config = config
        self.controller = (controller_factory or RecoveryController)(config)
        self.logger = logging.getLogger("orange_recovery.qr")

    def is_trigger_code(self, code: str) -> bool:
        raw_value = str(code or "")
        value = raw_value.strip()
        trigger = self.config.recovery_trigger
        if trigger.mode != "simple_code":
            return False
        if trigger.require_exact_match and raw_value != trigger.simple_code:
            return False
        return len(value) == trigger.code_length and value.isdigit() and value == trigger.simple_code

    def handle_scanned_qr(self, code: str) -> bool:
        if self.controller.active:
            self.logger.info("recovery active; normal QR processing remains paused")
            return True
        if not self.is_trigger_code(code):
            return False
        self.logger.info("recovery QR trigger consumed")
        self.controller.start(trigger_code=str(code).strip())
        return True


_default_handler: RecoveryQrHandler | None = None


def configure_default_handler(handler: RecoveryQrHandler) -> None:
    global _default_handler
    _default_handler = handler


def handle_scanned_qr(code: str) -> bool:
    """Return True when recovery consumed the QR code.

    Return False when normal QR handling should continue.
    """
    global _default_handler
    if _default_handler is None:
        _default_handler = RecoveryQrHandler(load_config())
    return _default_handler.handle_scanned_qr(code)
