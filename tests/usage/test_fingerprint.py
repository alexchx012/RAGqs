"""Fingerprint 碰撞回归测试（review #1）。

旧实现用 str() 拼接 canonical 字符串，下列每组输入都会碰撞（产生相同指纹）。
typed-tag + json.dumps 实现必须全部区分。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.usage._fingerprint import canonical_json, ledger_fingerprint


def test_fingerprint_separates_string_content_from_dict_structure() -> None:
    # 旧实现两者都序列化为 {a:b:c}
    assert ledger_fingerprint("k", {"a": "b:c"}) != ledger_fingerprint("k", {"a:b": "c"})


def test_fingerprint_distinguishes_bool_from_string() -> None:
    assert ledger_fingerprint("k", True) != ledger_fingerprint("k", "true")


def test_fingerprint_distinguishes_none_from_string() -> None:
    assert ledger_fingerprint("k", None) != ledger_fingerprint("k", "null")


def test_fingerprint_distinguishes_int_from_string_in_containers() -> None:
    # 旧实现 [1, "2"] 与 [1, 2] 都序列化为 [1,2]
    assert ledger_fingerprint("k", [1, "2"]) != ledger_fingerprint("k", [1, 2])


def test_fingerprint_distinguishes_decimal_from_number_and_string() -> None:
    assert ledger_fingerprint("k", Decimal("1.5")) != ledger_fingerprint("k", 1.5)
    assert ledger_fingerprint("k", Decimal("1.5")) != ledger_fingerprint("k", "1.5")


def test_fingerprint_distinguishes_datetime_from_string() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    assert ledger_fingerprint("k", now) != ledger_fingerprint("k", "2026-08-05T12:00:00+00:00")


def test_fingerprint_distinguishes_aware_and_naive_datetime() -> None:
    aware = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    naive = datetime(2026, 8, 5, 12, 0)
    assert ledger_fingerprint("k", aware) != ledger_fingerprint("k", naive)


def test_fingerprint_distinguishes_string_key_from_int_key() -> None:
    assert ledger_fingerprint("k", {"1": "a"}) != ledger_fingerprint("k", {1: "a"})


def test_fingerprint_distinguishes_list_from_dict() -> None:
    assert ledger_fingerprint("k", [{"a": 1}]) != ledger_fingerprint("k", {"a": [1]})


def test_fingerprint_is_order_insensitive_for_string_keys() -> None:
    assert ledger_fingerprint("k", {"a": 1, "b": 2}) == ledger_fingerprint("k", {"b": 2, "a": 1})


def test_fingerprint_is_order_insensitive_for_non_string_keys() -> None:
    assert ledger_fingerprint("k", {1: "x", "b": 2}) == ledger_fingerprint("k", {"b": 2, 1: "x"})


def test_fingerprint_tuple_and_list_are_equivalent() -> None:
    # 既有约定：tuple → list
    assert ledger_fingerprint("k", (1, 2)) == ledger_fingerprint("k", [1, 2])


def test_fingerprint_kind_is_part_of_the_fingerprint() -> None:
    assert ledger_fingerprint("provider_usage", {"x": 1}) != ledger_fingerprint(
        "local_usage", {"x": 1}
    )


def test_canonical_json_is_compact_sorted_and_typed() -> None:
    assert canonical_json({"b": 1, "a": 2}) == (
        '{"t":"dict","v":{"a":{"t":"num","v":2},"b":{"t":"num","v":1}}}'
    )


@dataclasses.dataclass
class _Sample:
    user_id: str
    pages: int


def test_fingerprint_dataclass_matches_asdict() -> None:
    assert ledger_fingerprint("k", _Sample("u1", 3)) == ledger_fingerprint(
        "k", {"user_id": "u1", "pages": 3}
    )


def test_fingerprint_distinguishes_bytes_from_string() -> None:
    assert ledger_fingerprint("k", b"abc") != ledger_fingerprint("k", "abc")
    # 相同内容一致性
    assert ledger_fingerprint("k", b"abc") == ledger_fingerprint("k", b"abc")


def test_fingerprint_distinguishes_uuid_from_string() -> None:
    import uuid

    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert ledger_fingerprint("k", u) != ledger_fingerprint("k", str(u))
    # UUID 规范化（大小写/连字符形式）不影响指纹
    assert ledger_fingerprint("k", u) == ledger_fingerprint(
        "k", uuid.UUID("12345678123456781234567812345678")
    )


def test_fingerprint_distinguishes_date_from_datetime_and_string() -> None:
    from datetime import date, datetime

    d = date(2026, 8, 5)
    assert ledger_fingerprint("k", d) != ledger_fingerprint("k", datetime(2026, 8, 5))
    assert ledger_fingerprint("k", d) != ledger_fingerprint("k", "2026-08-05")


def test_fingerprint_distinguishes_enum_from_string_and_int() -> None:
    import enum

    class _Kind(enum.Enum):
        RUNNING = "running"

    class _Other(enum.Enum):
        RUNNING = "running"

    assert ledger_fingerprint("k", _Kind.RUNNING) != ledger_fingerprint("k", "RUNNING")
    assert ledger_fingerprint("k", _Kind.RUNNING) != ledger_fingerprint("k", "running")
    # 同 name 不同枚举类型 → 不同指纹
    assert ledger_fingerprint("k", _Kind.RUNNING) != ledger_fingerprint("k", _Other.RUNNING)


def test_fingerprint_unknown_object_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="unsupported type"):
        ledger_fingerprint("k", object())
    with pytest.raises(TypeError, match="unsupported type"):
        canonical_json({1, 2})  # set 不支持
    with pytest.raises(TypeError, match="unsupported type"):
        ledger_fingerprint("k", {"nested": object()})  # 递归深处同样拒绝
