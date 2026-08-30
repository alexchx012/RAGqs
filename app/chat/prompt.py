"""Canonical generation-side prompt assembly for the chat provider port.

真实 provider 适配器把 ``ChatProviderRequest`` 变成模型输入时必须走这里的
组装：冲突分述指令（端口契约）显式进入 prompt，各上下文段按库标注来源，
满足《后端设计》§7.4④「优先级冲突不由系统裁决」的措辞要求。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .ports import (
    SOURCE_CONFLICT_INSTRUCTION,
    ChatProviderRequest,
    source_conflict_contract,
)


def _context_block(item: Mapping[str, Any]) -> str:
    library = str(item.get("library") or "unknown")
    space_id = str(item.get("space_id") or "unknown")
    document_id = str(item.get("document_id") or "")
    version_id = str(item.get("document_version_id") or "")
    locator = item.get("locator")
    locator_text = f" locator={locator}" if locator else ""
    source_text = f"{document_id}@{version_id}" if document_id or version_id else ""
    snippet = str(item.get("snippet") or "")
    header = f"[库 {library} | space {space_id}"
    if source_text:
        header += f" | {source_text}"
    header += f"]{locator_text}"
    return f"{header}\n{snippet}"


def assemble_generation_prompt(request: ChatProviderRequest) -> str:
    """Assemble the provider prompt; the conflict directive is always present."""

    contract = request.source_conflict_contract or source_conflict_contract()
    instruction = str(contract.get("instruction") or SOURCE_CONFLICT_INSTRUCTION)
    if request.purpose == "deep_retrieval_plan":
        return "\n\n".join(
            (
                instruction,
                "为深度研究选择检索策略。仅返回一个 JSON 对象，格式必须是 "
                '{"strategies":[...]}。可选值仅为 rewrite、split_subquestions、hyde、tree、'
                "sub_chunk、parent_document、document_summary；不输出理由、参数或其他字段。",
                request.content,
            )
        )
    blocks = [
        instruction,
        *(_context_block(item) for item in request.context_items),
        request.content,
    ]
    return "\n\n".join(blocks)
