"""Outbox and notification domain exports."""

from .schema import OUTBOX_TABLE_NAMES, outbox_metadata

__all__ = ["OUTBOX_TABLE_NAMES", "outbox_metadata"]
