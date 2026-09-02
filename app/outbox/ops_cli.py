"""Protected ops CLI for outbox delivery inspection and manual replay.

Subcommands wrap the dispatcher/service layer directly (the same
``ops_view``/``replay`` the ``/ops/outbox-deliveries`` HTTP endpoints use);
no HTTP self-call. ``RAG_MAINTENANCE_KEY`` is required, matching the other
protected CLI entry points, because the console script bypasses HTTP auth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import uuid
from dataclasses import asdict
from typing import NoReturn

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.http_contract import validate_idempotency_key
from app.platform.runtime import PlatformRuntime, build_runtime

from .dispatcher import OutboxDispatcher

_logger = logging.getLogger(__name__)

# Same canonical hash formula as the HTTP endpoint (app/api/v1/ops.py): a key
# used here and over HTTP for the same delivery reserves the same idempotency
# row, so the two paths can never double-replay under one key.
_REPLAY_HASH_DOMAIN = b"outbox-replay-v1\0"


def _replay_request_hash(*, consumer_name: str, expected_version: int, idempotency_key: str) -> str:
    encoded = json.dumps(
        {
            "consumer_name": consumer_name,
            "expected_version": expected_version,
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(_REPLAY_HASH_DOMAIN + encoded.encode("utf-8")).hexdigest()


def _require_maintenance_key(settings: PlatformSettings) -> None:
    if settings.maintenance_key is None or not settings.maintenance_key.get_secret_value().strip():
        raise ValueError("RAG_MAINTENANCE_KEY is required")


def _dispatcher(runtime: PlatformRuntime) -> OutboxDispatcher:
    dispatcher = runtime.resolve("outbox_dispatcher")
    if not isinstance(dispatcher, OutboxDispatcher):
        raise RuntimeError("outbox dispatcher is not configured")
    return dispatcher


def run_outbox_delivery_view(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    event_id: str,
    consumer_name: str = "in_app_notification",
) -> dict[str, object] | None:
    """GET /ops/outbox-deliveries/{event_id} 等价查询（service 层直连）。"""

    _require_maintenance_key(settings)
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        view = _dispatcher(active_runtime).ops_view(event_id, consumer_name=consumer_name)
        if view is None:
            return None
        return {
            "event_id": view.event_id,
            "consumer_name": view.consumer_name,
            "status": view.status,
            "version": view.version,
            "replay_generation": view.replay_generation,
            "attempt_number": view.attempt_number,
            "error": (
                {"category": view.error_category, "code": view.error_code}
                if view.error_category is not None
                else None
            ),
            "replayable": view.replayable,
            "next_attempt_at": (
                view.next_attempt_at.isoformat() if view.next_attempt_at is not None else None
            ),
            "lease_expires_at": (
                view.lease_expires_at.isoformat() if view.lease_expires_at is not None else None
            ),
        }
    finally:
        if owns_runtime:
            active_runtime.close()


def run_outbox_delivery_replay(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    event_id: str,
    expected_version: int,
    consumer_name: str = "in_app_notification",
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """POST /ops/outbox-deliveries/{event_id}/replay 等价调用（service 层直连）。"""

    _require_maintenance_key(settings)
    key = validate_idempotency_key(idempotency_key)
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        receipt = _dispatcher(active_runtime).replay(
            event_id,
            consumer_name=consumer_name,
            expected_version=expected_version,
            idempotency_key=key,
            request_hash=_replay_request_hash(
                consumer_name=consumer_name,
                expected_version=expected_version,
                idempotency_key=key,
            ),
        )
        return dict(asdict(receipt))
    finally:
        if owns_runtime:
            active_runtime.close()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: invalid arguments\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="ragqs-outbox-delivery")
    subparsers = parser.add_subparsers(dest="command", required=True)

    view = subparsers.add_parser("list", help="show one delivery (ops view)")
    view.add_argument("--event-id", required=True)
    view.add_argument("--consumer-name", default="in_app_notification")

    replay = subparsers.add_parser("replay", help="replay a dead-lettered delivery")
    replay.add_argument("--event-id", required=True)
    replay.add_argument("--expected-version", type=int, required=True)
    replay.add_argument("--consumer-name", default="in_app_notification")
    replay.add_argument(
        "--idempotency-key",
        default=None,
        help="reuses the HTTP endpoint idempotency namespace; defaults to a fresh key",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_platform_settings()
        _require_maintenance_key(settings)
    except ValueError:
        print(
            "ragqs-outbox-delivery: configuration or RAG_MAINTENANCE_KEY error",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    try:
        if args.command == "list":
            result = run_outbox_delivery_view(
                settings,
                event_id=args.event_id,
                consumer_name=args.consumer_name,
            )
            if result is None:
                print(json.dumps({"error": "not_found", "event_id": args.event_id}))
                raise SystemExit(1)
        else:
            result = run_outbox_delivery_replay(
                settings,
                event_id=args.event_id,
                expected_version=args.expected_version,
                consumer_name=args.consumer_name,
                idempotency_key=args.idempotency_key or f"cli-{uuid.uuid4().hex}",
            )
    except PlatformError as error:
        print(
            json.dumps({"error": error.code, "message": error.message, "details": error.details}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
