"""source_conflict_contract 端口契约与生成侧 prompt 组装契约（design §7.4④）。

冲突不裁决、分述各出处的指令文本是端口契约的一部分；prompt 组装结果必须
包含该指令，并按库标注各上下文段来源。
"""

from __future__ import annotations

from app.chat.ports import (
    SOURCE_CONFLICT_INSTRUCTION,
    ChatProviderRequest,
    source_conflict_contract,
)
from app.chat.prompt import assemble_generation_prompt


def _request() -> ChatProviderRequest:
    return ChatProviderRequest(
        generation_id="generation_1",
        owner_user_id="user_1",
        content="compare the policies",
        effort_level="quick",
        candidate=None,
        context_items=(
            {
                "library": "vector",
                "space_id": "space_1",
                "document_id": "doc_1",
                "document_version_id": "version_1",
                "chunk_id": "chunk_1",
                "locator": {"page": 1},
                "snippet": "policy A says 5 days",
            },
            {
                "library": "sparse",
                "space_id": "space_2",
                "document_id": "doc_2",
                "document_version_id": "version_2",
                "chunk_id": "chunk_2",
                "locator": {"page": 3},
                "snippet": "policy B says 10 days",
            },
        ),
        source_conflict_contract=source_conflict_contract(),
    )


def test_chat_provider_request_carries_non_adjudicating_source_conflict_contract() -> None:
    request = _request()
    assert request.source_conflict_contract == {
        "required_fields": ("library", "space_id", "publication/version", "locator"),
        "conflict_policy": "present_each_claim_and_citation_no_system_adjudication",
        "instruction": SOURCE_CONFLICT_INSTRUCTION,
    }


def test_canonical_instruction_requires_separate_statements_per_source() -> None:
    assert "来源冲突时分别陈述并各自标注出处" in SOURCE_CONFLICT_INSTRUCTION
    assert "不要合并成单一结论" in SOURCE_CONFLICT_INSTRUCTION
    assert "点明各段依据来自哪个库" in SOURCE_CONFLICT_INSTRUCTION


def test_assembled_prompt_contains_the_conflict_directive_and_library_labels() -> None:
    prompt = assemble_generation_prompt(_request())
    # 契约核心：分述指令显式进入 prompt。
    assert SOURCE_CONFLICT_INSTRUCTION in prompt
    assert "compare the policies" in prompt
    # 各上下文段按库标注来源（点明依据来自哪个库）。
    assert "库 vector" in prompt
    assert "库 sparse" in prompt
    assert "policy A says 5 days" in prompt
    assert "policy B says 10 days" in prompt


def test_prompt_assembly_injects_the_canonical_directive_without_a_contract() -> None:
    request = _request()
    # 防御性兜底：未显式携带契约的请求也必须组出包含分述指令的 prompt。
    object.__setattr__(request, "source_conflict_contract", None)
    prompt = assemble_generation_prompt(request)
    assert SOURCE_CONFLICT_INSTRUCTION in prompt
