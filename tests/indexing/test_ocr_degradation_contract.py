"""OCR degradation event contract: explicit reason/status and closed fact shape."""

from __future__ import annotations

from app.indexing.processing import _machine_low_confidence_fact
from app.outbox.publisher import PAYLOAD_SCHEMAS


def test_machine_low_confidence_fact_matches_outbox_closed_schema() -> None:
    fact = _machine_low_confidence_fact(0.42, 3)
    assert fact == {"confidence": 0.42, "page": 3, "region": []}
    # The outbox validator only allows confidence, page and region in the fact.
    assert set(fact) <= {"confidence", "page", "region"}


def test_ocr_low_confidence_payload_requires_reason_and_status() -> None:
    schema = PAYLOAD_SCHEMAS["ocr_low_confidence"]
    assert schema["reason"] is str
    assert schema["status"] is str
    assert schema["machine_low_confidence_fact"] is dict
