"""fix-migration-id-length：revision id 长度守卫与迁移链完整性。

PostgreSQL `alembic_version.version_num` 是 varchar(32)；任何超过 32 字符的
revision id 都会在 `alembic upgrade` 写入版本表时报 StringDataRightTruncation
（tests/outbox 的 postgres 套件曾因 0025/0026 两个超长 id 集体失败）。
"""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory

MAX_REVISION_ID_LENGTH = 32


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def _parents(down_revision: object) -> tuple[str, ...]:
    if isinstance(down_revision, (list, tuple)):
        return tuple(str(item) for item in down_revision)
    return (str(down_revision),) if down_revision else ()


def test_every_revision_id_fits_alembic_version_varchar32() -> None:
    offenders = [
        revision.revision
        for revision in _script().walk_revisions()
        if len(revision.revision) > MAX_REVISION_ID_LENGTH
    ]
    assert offenders == [], f"revision ids exceed varchar(32): {offenders}"


def test_migration_chain_has_single_head_and_no_dangling_down_revisions() -> None:
    script = _script()
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"
    known = {revision.revision for revision in script.walk_revisions()}
    for revision in script.walk_revisions():
        for parent in _parents(revision.down_revision):
            assert parent in known, f"{revision.revision} references missing {parent}"
