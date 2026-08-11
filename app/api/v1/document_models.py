from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class ExpectedVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)


class SubmissionRejectRequest(ExpectedVersionRequest):
    reason: StrictStr | None = Field(default=None, max_length=256)
