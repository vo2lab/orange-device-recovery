"""Rollback adapter for the recovery CLI/API."""

from __future__ import annotations

from .config import RecoveryConfig
from .repair_package import RepairPackageManager


def rollback_last_repair(config: RecoveryConfig) -> dict[str, object]:
    return RepairPackageManager(config).rollback()
