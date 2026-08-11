"""Canonical serialization and ledger fingerprints for the usage/quota domain.

供 02/03/04 复用的纯函数工具；不依赖数据库。

序列化策略（review #1）：递归把任意 payload 转换为带显式类型标签的
JSON-compatible 结构，再用 ``json.dumps(sort_keys=True, separators=(",", ":"))``
输出。每个叶子都包成 ``{"t": <tag>, "v": ...}``，因此字符串/布尔/None/Decimal/
datetime/容器（list/dict/tuple）在序列化后不可能产生相同字节串——旧实现用
``str()`` 拼接（如 ``{a:b:c}``、``true``/``null``、``<d:1.5>``）存在分隔碰撞。
非字符串 dict key 使用 typed key/value pair 形式（字符串 key 保持 JSON object
形式，两种形式由 ``v`` 的类型区分，互不碰撞）。
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import hmac
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

__all__ = ["canonical_json", "ledger_fingerprint"]

_TAG_NULL = "null"
_TAG_BOOL = "bool"
_TAG_NUM = "num"
_TAG_STR = "str"
_TAG_BYTES = "bytes"
_TAG_DECIMAL = "decimal"
_TAG_DATETIME = "datetime"
_TAG_DATETIME_NAIVE = "datetime-naive"
_TAG_DATE = "date"
_TAG_UUID = "uuid"
_TAG_ENUM = "enum"
_TAG_LIST = "list"
_TAG_DICT = "dict"
_TAG_KEY = "key"


def _tagged(tag: str, value: object) -> dict[str, object]:
    """单层类型标签包装：``{"t": <tag>, "v": <json-compatible>}``。"""
    return {"t": tag, "v": value}


def _to_tagged(payload: object) -> dict[str, object]:
    """递归把 payload 转为带显式类型标签的 JSON-compatible 结构。

    支持类型：None/bool/int/float/str/bytes/Decimal/datetime/date/UUID/Enum/
    dict（字符串 key 或 typed key/value pair）/list/tuple/dataclass。
    其余类型一律 ``TypeError`` 拒绝（不做隐式 str() 兜底，避免类型碰撞）。
    """
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    if payload is None:
        return _tagged(_TAG_NULL, None)
    if isinstance(payload, bool):
        return _tagged(_TAG_BOOL, payload)
    if isinstance(payload, enum.Enum):
        # 先于 int/str 检查：IntEnum/StrEnum 也走显式 enum 标签
        return _tagged(
            _TAG_ENUM,
            f"{type(payload).__module__}.{type(payload).__qualname__}:{payload.name}",
        )
    if isinstance(payload, (int, float)):
        return _tagged(_TAG_NUM, payload)
    if isinstance(payload, str):
        return _tagged(_TAG_STR, payload)
    if isinstance(payload, bytes):
        return _tagged(_TAG_BYTES, payload.hex())
    if isinstance(payload, Decimal):
        return _tagged(_TAG_DECIMAL, str(payload))
    if isinstance(payload, datetime):
        if payload.tzinfo:
            return _tagged(_TAG_DATETIME, payload.astimezone(UTC).isoformat())
        return _tagged(_TAG_DATETIME_NAIVE, payload.isoformat())
    if isinstance(payload, date):
        # datetime 是 date 子类，此检查必须在 datetime 之后
        return _tagged(_TAG_DATE, payload.isoformat())
    if isinstance(payload, UUID):
        return _tagged(_TAG_UUID, str(payload))
    if isinstance(payload, dict):
        if all(isinstance(key, str) for key in payload):
            return _tagged(_TAG_DICT, {key: _to_tagged(value) for key, value in payload.items()})
        pairs = [
            [_tagged(_TAG_KEY, _to_tagged(key)), _to_tagged(value)]
            for key, value in payload.items()
        ]
        pairs.sort(key=lambda pair: json.dumps(pair[0], sort_keys=True, separators=(",", ":")))
        return _tagged(_TAG_DICT, pairs)
    if isinstance(payload, (list, tuple)):
        return _tagged(_TAG_LIST, [_to_tagged(value) for value in payload])
    raise TypeError(
        f"unsupported type for canonical serialization: {type(payload).__module__}."
        f"{type(payload).__qualname__}"
    )


def canonical_json(payload: object) -> str:
    """Canonical serialization for ledger fingerprints: typed-tag JSON, sorted keys, compact."""
    return json.dumps(_to_tagged(payload), sort_keys=True, separators=(",", ":"))


def ledger_fingerprint(kind: str, payload: object) -> str:
    """HMAC-SHA256 over canonical payload; used for provider_usage/local_usage/debit entries."""
    canonical = json.dumps(
        {"kind": kind, "payload": _to_tagged(payload)}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(b"ragqs-usage-ledger-v1", canonical, digestmod=hashlib.sha256).hexdigest()
