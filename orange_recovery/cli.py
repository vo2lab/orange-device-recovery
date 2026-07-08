"""Command line entry point for orange-recovery."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import load_config
from .diagnostics import make_diagnostics_zip
from .qr_trigger import RecoveryQrHandler
from .recovery_controller import RecoveryController
from .repair_package import RepairPackageManager


def _configure_logging(log_file: str, level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass
    logging.basicConfig(level=numeric, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orange dispenser local recovery API")
    parser.add_argument("--config", default=None, help="Path to /etc/orange-recovery/config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Log network/service commands without executing them")
    parser.add_argument("-repair", "--repair", dest="repair_mode", action="store_true", help="Start the QR-triggered repair flow")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")
    trigger = sub.add_parser("trigger-code")
    trigger.add_argument("code")
    sub.add_parser("start")
    sub.add_parser("repair")
    sub.add_parser("stop")
    sub.add_parser("restore-network")
    validate = sub.add_parser("validate-package")
    validate.add_argument("package")
    apply = sub.add_parser("apply-package")
    apply.add_argument("package")
    sub.add_parser("make-diagnostics")
    sub.add_parser("rollback")
    sub.add_parser("serve")

    args = parser.parse_args(argv)
    if args.repair_mode:
        args.command = "repair"
    if not args.command:
        parser.error("a command is required")
    config = load_config(args.config)
    if args.dry_run:
        config.network.dry_run = True
        config.api.prefer_fastapi = False
    _configure_logging(config.logging.log_file, config.logging.level)

    if args.command == "status":
        state_path = Path(config.paths.state_dir) / "state.json"
        if state_path.exists():
            _print_json(json.loads(state_path.read_text(encoding="utf-8")))
        else:
            _print_json(RecoveryController(config).status(include_token=False))
        return 0

    if args.command == "trigger-code":
        controller = RecoveryController(config)
        handler = RecoveryQrHandler(config, controller_factory=lambda _cfg: controller)
        consumed = handler.handle_scanned_qr(args.code)
        _print_json({"consumed": consumed, "state": controller.state, "status": controller.status(include_token=True)})
        if consumed and controller.active:
            controller.wait_forever()
        return 0 if consumed else 2

    if args.command in {"start", "serve", "repair"}:
        controller = RecoveryController(config)
        ok = controller.start(repair_mode=args.command == "repair")
        _print_json(controller.status(include_token=True))
        if ok:
            controller.wait_forever()
        return 0 if ok else 1

    if args.command == "stop":
        controller = RecoveryController(config)
        _print_json(controller.restore_network())
        return 0

    if args.command == "restore-network":
        controller = RecoveryController(config)
        _print_json(controller.restore_network())
        return 0

    if args.command == "validate-package":
        result = RepairPackageManager(config).validate_package(args.package)
        _print_json(result.as_response())
        return 0 if result.ok else 1

    if args.command == "apply-package":
        manager = RepairPackageManager(config)
        result = manager.validate_package(args.package)
        if not result.ok:
            _print_json(result.as_response())
            return 1
        _print_json(manager.apply_package(args.package, result))
        return 0

    if args.command == "make-diagnostics":
        _print_json({"ok": True, "path": make_diagnostics_zip(config)})
        return 0

    if args.command == "rollback":
        _print_json(RepairPackageManager(config).rollback())
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
