"""The MCP tools of a feature of the hub (canvas, srs, tasks, docs), each an object of one table: ``Tool`` names a tool,
its title, what it does, its input schema and the function of the feature's agent module that answers it;
``Tools`` holds them by name and gives the manifest's ``mcp_tools`` (``contributions``), each pointing at
``<module>:<handler>``. The hub lists a tool on /mcp as ``<feature>__<name>`` and checks its arguments against the
schema first (``evo_agents.hub.plugin_calls``)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: str | None = None  # the function of the agent module; the tool's name when None

    def contribution(self, module: str) -> dict:
        """The tool as the manifest's ``mcp_tools`` declares it, answered by ``module``."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "python": f"{module}:{self.handler or self.name}",
            "input_schema": self.input_schema,
        }


class Tools:
    """The tools of a feature, by name, each answered by a function of ``module``."""

    def __init__(self, module: str, *tools: Tool):
        self.module = module
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError(f"two tools of {module} share a name")

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __getitem__(self, name: str) -> Tool:
        return self._tools[name]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def contributions(self) -> list[dict]:
        """The manifest's ``mcp_tools``, in the table's order."""
        return [tool.contribution(self.module) for tool in self]
