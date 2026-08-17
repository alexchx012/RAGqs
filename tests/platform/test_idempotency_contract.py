from __future__ import annotations

import pytest

from app.platform.errors import PlatformError
from app.platform.http_contract import parse_idempotency_key, validate_idempotency_key


def test_idempotency_key_parser_rejects_values_longer_than_persistence_limit() -> None:
    with pytest.raises(PlatformError) as exc_info:
        parse_idempotency_key({"Idempotency-Key": "x" * 257})

    assert exc_info.value.code == "validation_error"
    assert exc_info.value.status_code == 422
    assert exc_info.value.details == {"max_length": 256}


@pytest.mark.parametrize("value", [None, "", " \t "])
def test_idempotency_key_validation_rejects_missing_or_blank_values(value: str | None) -> None:
    with pytest.raises(PlatformError) as exc_info:
        validate_idempotency_key(value)

    assert exc_info.value.code == "validation_error"
    assert exc_info.value.status_code == 422
    assert exc_info.value.details == {}


def test_idempotency_key_validation_accepts_the_persistence_limit() -> None:
    assert validate_idempotency_key("x" * 256) == "x" * 256
