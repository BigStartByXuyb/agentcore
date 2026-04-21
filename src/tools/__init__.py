"""Tool registry — central lookup for every tool.

Provides ToolRegistry class and a global `registry` instance.
"""

from __future__ import annotations

from collections.abc import ItemsView

from src.types import ToolDef
from src.tools.bash import tool as bash_tool
from src.tools.read_file import tool as read_file_tool
from src.tools.grep import tool as grep_tool
from src.tools.skill import tool as skill_tool
from src.tools.agent import tool as agent_tool


class ToolRegistry:
    """Central tool lookup — single source of truth for all tool access.

    Tracks tool source (builtin/mcp:server_name) for future per-server
    hot-reload support.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._sources: dict[str, str] = {}

    def register(self, name: str, tool: ToolDef, source: str = "builtin") -> None:
        self._tools[name] = tool
        self._sources[name] = source

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._sources.pop(name, None)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def items(self) -> ItemsView[str, ToolDef]:
        return self._tools.items()

    def unregister_by_source(self, source: str) -> list[str]:
        """Remove all tools from a given source. Returns removed names."""
        to_remove = [n for n, s in self._sources.items() if s == source]
        for n in to_remove:
            self.unregister(n)
        return to_remove


# ---------------------------------------------------------------------------
# Global instance
# ---------------------------------------------------------------------------

registry = ToolRegistry()
registry.register(bash_tool.name, bash_tool)
registry.register(read_file_tool.name, read_file_tool)
registry.register(grep_tool.name, grep_tool)
registry.register(skill_tool.name, skill_tool)
registry.register(agent_tool.name, agent_tool)
