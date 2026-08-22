"""Physical account-deletion archive package (§9.2.1).

Identity owns the physical archive: when an account deletion workflow is
accepted, identity builds ``{user_id}-{deletion_id}.zip`` in the configured
archive directory via a same-directory temporary file and an atomic rename.
``manifest.json`` records the archive format version, entity counts, file
list and per-file SHA-256 values. Column lists are enumerated explicitly so
password hashes, auth tokens, session credentials, runtime caches, derived
indexes, audit rows, ``usage_event`` and ``quota_debit`` can never enter a
package; shared-space file copies are excluded by construction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.platform.storage import ObjectStorePort

ARCHIVE_FORMAT_VERSION = "account-archive/v1"

_ARCHIVE_PROFILE_SQL = """
SELECT id, username, real_name, display_name, department_id, role,
       lifecycle_status, version, avatar_url, preferences_json,
       created_at_utc, updated_at_utc, deletion_requested_at_utc, purge_after_at_utc
FROM identity_user WHERE id = :user_id
"""
_ARCHIVE_DEPARTMENT_SQL = """
SELECT d.id, d.name, d.status, d.version
FROM identity_department d
JOIN identity_user u ON u.department_id = d.id
WHERE u.id = :user_id
"""
_ARCHIVE_PERSONAL_SPACES_SQL = """
SELECT id, kind, name, created_at_utc FROM identity_space
WHERE owner_user_id = :user_id
"""
_ARCHIVE_DOCUMENTS_SQL = """
SELECT d.id, d.space_id, d.name, d.media_kind, d.lifecycle_status, d.version,
       d.created_at_utc, d.updated_at_utc
FROM documents d
JOIN identity_space s ON s.id = d.space_id
WHERE s.owner_user_id = :user_id
"""
_ARCHIVE_VERSIONS_SQL = """
SELECT v.id, v.document_id, v.version_number, v.status, v.file_name,
       v.media_kind, v.size_bytes, v.content_hash_sha256, v.created_by_user_id,
       v.created_at_utc, v.activated_at_utc, v.terminal_at_utc
FROM document_versions v
JOIN documents d ON d.id = v.document_id
JOIN identity_space s ON s.id = d.space_id
WHERE s.owner_user_id = :user_id
"""
_ARCHIVE_ORIGINAL_FILES_SQL = """
SELECT v.id AS version_id, v.original_object_key, v.file_name
FROM document_versions v
JOIN documents d ON d.id = v.document_id
JOIN identity_space s ON s.id = d.space_id
WHERE s.owner_user_id = :user_id AND v.original_object_key IS NOT NULL
"""
_ARCHIVE_CONVERSATION_GROUPS_SQL = """
SELECT id, name, created_at_utc FROM chat_conversation_group
WHERE owner_user_id = :user_id
"""
_ARCHIVE_CONVERSATIONS_SQL = """
SELECT id, title, pinned, effort_level, scope_json, last_active_at_utc, created_at_utc
FROM chat_conversation WHERE owner_user_id = :user_id
"""
_ARCHIVE_MESSAGES_SQL = """
SELECT id, conversation_id, role, content, answer_mode, effort_level,
       citations_json, created_at_utc
FROM chat_message WHERE owner_user_id = :user_id
"""
_ARCHIVE_FEEDBACK_SQL = """
SELECT message_id, vote, down_reason, created_at_utc FROM chat_message_feedback
WHERE voter_user_id = :user_id
"""
_ARCHIVE_TASK_SUMMARY_SQL = """
SELECT id, document_id, operation, state, failure_reason, created_at_utc, updated_at_utc
FROM ingestion_jobs WHERE created_by_user_id = :user_id
"""
_ARCHIVE_CONTRIBUTIONS_SQL = """
SELECT d.id, d.space_id, d.name, d.lifecycle_status, d.created_at_utc
FROM documents d
LEFT JOIN identity_space s ON s.id = d.space_id
WHERE d.created_by_user_id = :user_id AND (s.owner_user_id IS NULL OR s.owner_user_id != :user_id)
"""
_ARCHIVE_NOTIFICATIONS_SQL = """
SELECT id, event_id, notification_type, title, payload_json, document_id,
       event_occurred_at_utc, notification_seq, read_at_utc
FROM notification
WHERE recipient_user_id = :user_id AND retire_after_at_utc > :now
"""
_ARCHIVE_INBOX_SQL = """
SELECT recipient_user_id, next_notification_seq, read_through_seq, read_all_at_utc, version
FROM notification_inbox WHERE recipient_user_id = :user_id
"""
_ARCHIVE_CONTEXT_ACKS_SQL = """
SELECT event_id, acked_at_utc FROM notification_context_ack
WHERE recipient_user_id = :user_id
"""


@dataclass(frozen=True, slots=True)
class AccountArchiveRecord:
    file_name: str
    size_bytes: int
    sha256: str


def _default(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat()
    return str(value)


def _object_payload(result: object) -> bytes:
    # ObjectStorePort.get() returns (payload, metadata) for some adapters.
    if isinstance(result, tuple):
        return bytes(result[0])
    return bytes(result)  # type: ignore[arg-type]


def _add_entry(
    archive: zipfile.ZipFile,
    manifest_files: list[dict[str, object]],
    name: str,
    payload: bytes,
) -> None:
    archive.writestr(zipfile.ZipInfo(name), payload, zipfile.ZIP_DEFLATED)
    manifest_files.append(
        {"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    )


class AccountArchivePackageBuilder:
    """Builds and verifies the physical archive package for one deletion."""

    def __init__(self, archive_dir: str, object_store: ObjectStorePort | None = None) -> None:
        self._archive_dir = os.path.abspath(archive_dir)
        self._object_store = object_store

    def archive_file_name(self, *, user_id: str, deletion_id: str) -> str:
        return f"{user_id}-{deletion_id}.zip"

    def archive_path(self, *, user_id: str, deletion_id: str) -> str:
        return os.path.join(
            self._archive_dir, self.archive_file_name(user_id=user_id, deletion_id=deletion_id)
        )

    def build(
        self,
        connection: Connection,
        *,
        user_id: str,
        deletion_id: str,
        now: datetime,
    ) -> AccountArchiveRecord:
        os.makedirs(self._archive_dir, exist_ok=True)
        params = {"user_id": user_id, "now": now}
        entities: dict[str, list[dict[str, object]]] = {}
        for name, sql in (
            ("profile", _ARCHIVE_PROFILE_SQL),
            ("department", _ARCHIVE_DEPARTMENT_SQL),
            ("personal_spaces", _ARCHIVE_PERSONAL_SPACES_SQL),
            ("documents", _ARCHIVE_DOCUMENTS_SQL),
            ("document_versions", _ARCHIVE_VERSIONS_SQL),
            ("conversation_groups", _ARCHIVE_CONVERSATION_GROUPS_SQL),
            ("conversations", _ARCHIVE_CONVERSATIONS_SQL),
            ("messages", _ARCHIVE_MESSAGES_SQL),
            ("message_feedback", _ARCHIVE_FEEDBACK_SQL),
            ("task_summary", _ARCHIVE_TASK_SUMMARY_SQL),
            ("contributions", _ARCHIVE_CONTRIBUTIONS_SQL),
            ("notifications", _ARCHIVE_NOTIFICATIONS_SQL),
            ("notification_inbox", _ARCHIVE_INBOX_SQL),
            ("notification_context_acks", _ARCHIVE_CONTEXT_ACKS_SQL),
        ):
            rows = connection.execute(sa.text(sql), params).mappings().all()
            entities[name] = [dict(row) for row in rows]

        blobs: list[tuple[str, bytes]] = []
        profile = entities["profile"][0] if entities["profile"] else {}
        avatar_url = profile.get("avatar_url")
        if (
            self._object_store is not None
            and isinstance(avatar_url, str)
            and avatar_url.startswith("object://")
        ):
            key = avatar_url.removeprefix("object://")
            if self._object_store.exists(key):
                blobs.append(("objects/avatar", _object_payload(self._object_store.get(key))))
        if self._object_store is not None:
            for row in connection.execute(sa.text(_ARCHIVE_ORIGINAL_FILES_SQL), params).mappings():
                key = str(row["original_object_key"])
                if self._object_store.exists(key):
                    blobs.append(
                        (f"objects/{row['version_id']}", _object_payload(self._object_store.get(key)))
                    )

        manifest_files: list[dict[str, object]] = []
        target_name = self.archive_file_name(user_id=user_id, deletion_id=deletion_id)
        final_path = os.path.join(self._archive_dir, target_name)
        fd, temp_path = tempfile.mkstemp(prefix=f".{target_name}.", dir=self._archive_dir)
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry_name, payload in blobs:
                    _add_entry(archive, manifest_files, entry_name, payload)
                for entity_name, rows in entities.items():
                    payload = json.dumps(rows, default=_default, ensure_ascii=False).encode("utf-8")
                    _add_entry(archive, manifest_files, f"{entity_name}.json", payload)
                manifest = {
                    "format_version": ARCHIVE_FORMAT_VERSION,
                    "user_id": user_id,
                    "deletion_id": deletion_id,
                    "created_at": now.isoformat(),
                    "entity_counts": {name: len(rows) for name, rows in entities.items()},
                    "files": manifest_files,
                }
                _add_entry(
                    archive,
                    manifest_files,
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                )
            with zipfile.ZipFile(temp_path) as verify_zip:
                if verify_zip.testzip() is not None:
                    raise ValueError("archive package failed integrity verification")
            os.replace(temp_path, final_path)
        except BaseException:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        record = _hash_file(final_path)
        return AccountArchiveRecord(
            file_name=target_name, size_bytes=record[0], sha256=record[1]
        )

    def verify(
        self,
        *,
        user_id: str,
        deletion_id: str,
        expected_sha256: str,
        expected_size: int | None = None,
    ) -> bool:
        path = self.archive_path(user_id=user_id, deletion_id=deletion_id)
        try:
            size, digest = _hash_file(path)
        except OSError:
            return False
        if expected_size is not None and size != expected_size:
            return False
        return secrets.compare_digest(digest, expected_sha256)


def _hash_file(path: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
