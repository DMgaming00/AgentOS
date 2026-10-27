"""Tool registry. Tools not registered here cannot be called, whatever a model asks for.

An HTTP fetch tool is intentionally *not* registered in v1 (see docs/security.md);
there is no shell or code-execution tool.
"""

from __future__ import annotations

from typing import Any

from agentos_core.domain.dag import ToolInfo
from agentos_core.tools.artifact_writer import ArtifactWriterTool
from agentos_core.tools.base import Tool
from agentos_core.tools.calculator import CalculatorTool
from agentos_core.tools.data_reader import DataReaderTool
from agentos_core.tools.document_search import DocumentSearchTool
from agentos_core.tools.memory import MemorySearchTool, MemoryWriteTool


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def catalog(self) -> list[dict[str, Any]]:
        return [self._tools[n].metadata() for n in self.names()]

    def infos(self) -> dict[str, ToolInfo]:
        return {n: ToolInfo(name=n, enabled=t.enabled_by_default) for n, t in self._tools.items()}


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry(
            [
                CalculatorTool(),
                DocumentSearchTool(),
                DataReaderTool(),
                ArtifactWriterTool(),
                MemorySearchTool(),
                MemoryWriteTool(),
            ]
        )
    return _registry
