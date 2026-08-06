from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.platform.storage import ObjectStorePort

from .ports import AccountDeletionCleanupCommand, AccountDeletionCleanupReceipt
from .schema import identity_user_table


class ObjectStoreAccountDeletionCleanupPort:
    """Removes identity-owned object-store data before an account becomes a tombstone."""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def confirm_cleanup(
        self,
        command: AccountDeletionCleanupCommand,
        *,
        connection: Connection,
    ) -> AccountDeletionCleanupReceipt:
        avatar_url = connection.execute(
            select(identity_user_table.c.avatar_url).where(
                identity_user_table.c.id == command.user_id
            )
        ).scalar_one_or_none()
        if isinstance(avatar_url, str) and avatar_url.startswith("object://"):
            avatar_key = avatar_url.removeprefix("object://")
            if self._object_store.exists(avatar_key):
                self._object_store.delete(avatar_key)
        return AccountDeletionCleanupReceipt(
            reference=f"object-storage:{command.operation_id}",
            state="completed",
        )
