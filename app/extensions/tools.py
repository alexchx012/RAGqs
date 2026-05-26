"""Tool registry for built-in and business-specific LangChain tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class UnknownToolError(KeyError):
    """Raised when configuration references an unregistered tool."""


@dataclass(frozen=True)
class ToolRegistration:
    name: str
    tool: Any
    category: str = "builtin"
    description: str = ""


class ToolRegistry:
    """Ordered registry for agent tools."""

    def __init__(self):
        self._registrations: dict[str, ToolRegistration] = {}

    def register(
        self,
        tool: Any,
        *,
        name: str | None = None,
        category: str = "builtin",
        description: str = "",
    ) -> None:
        tool_name = name or getattr(tool, "name", None) or getattr(tool, "__name__", None)
        if not tool_name:
            raise ValueError("tool must have a name")
        if tool_name in self._registrations:
            raise ValueError(f"tool already registered: {tool_name}")
        self._registrations[tool_name] = ToolRegistration(
            name=tool_name,
            tool=tool,
            category=category,
            description=description or getattr(tool, "description", ""),
        )

    def names(self) -> list[str]:
        return list(self._registrations.keys())

    def metadata(self, name: str) -> dict[str, str]:
        registration = self._get(name)
        return {
            "name": registration.name,
            "category": registration.category,
            "description": registration.description,
        }

    def build_tools(self, names: Iterable[str]) -> list[Any]:
        return [self._get(name).tool for name in names]

    def _get(self, name: str) -> ToolRegistration:
        if name not in self._registrations:
            raise UnknownToolError(f"unknown tool: {name}")
        return self._registrations[name]


def parse_enabled_tool_names(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [name.strip() for name in value.split(",") if name.strip()]
    return [str(name).strip() for name in value if str(name).strip()]


def build_default_tool_registry() -> ToolRegistry:
    from app.tools import get_current_time, retrieve_knowledge

    registry = ToolRegistry()
    registry.register(retrieve_knowledge, category="builtin")
    registry.register(get_current_time, category="builtin")
    return registry


def build_enabled_tools(
    enabled_tools: str | Iterable[str] | None,
    *,
    registry: ToolRegistry | None = None,
) -> list[Any]:
    active_registry = registry or build_default_tool_registry()
    tool_names = parse_enabled_tool_names(enabled_tools) or active_registry.names()
    return active_registry.build_tools(tool_names)
