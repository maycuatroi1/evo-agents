"""The kg_* tools of a session as commands: a ``Tool`` holds what one call of one tool needs, and a ``ToolTable`` is
the one register of the tools a session answers, each by its name and with the schema MCP lists for it
(``evo_agents.kg.tools.TOOLS``).

A call goes through its tool in two steps: ``prepare`` reads the arguments as the tool takes them (the names agents
guess, ``refs.arguments``) and applies those every read takes (``branch``, ``deprecated`` and ``graph``) to the
session's reader; ``run`` answers with the text and the structured result, which the session wraps in its envelope
(``evo_agents.kg.serve.Session.call``). A session extends the tools by registering more in a copy of the table
(``ToolTable.with_``): the hub answers kg_status (``evo_agents.hub.kg_graph.HubSession``), and a result the hub
computed goes through the envelope as a tool of its own (``Answered``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import ClassVar

from evo_agents.kg.refs import arguments
from evo_agents.kg.serve_text import NodeText
from evo_agents.kg.serve_view import SessionView, ToolError
from evo_agents.kg.snapshot import SnapshotView
from evo_agents.kg.tools import SIGNATURES, TOOLS
from evo_agents.kg.visibility import Visibility

SCHEMAS = {tool["name"]: tool for tool in TOOLS}


class Tool:
    """One call of the tool ``name`` for ``session``: the schema MCP lists for it, the arguments it takes
    (``prepare``) and its answer (``run``)."""

    name: ClassVar[str] = ""
    schema: ClassVar[dict | None] = None  # the tool as tools/list gives it; None for a tool MCP does not list

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__:
            cls.schema = SCHEMAS.get(cls.name)

    def __init__(self, session: SessionView):
        self.session = session
        self.text = NodeText(session)

    @property
    def visibility(self) -> Visibility:
        """What the session may see in this call: the store's once ``store`` read it."""
        return self.session.visibility

    def store(self) -> SnapshotView:
        """The graph this call reads, as the session's reader reads it (``SessionView.store``)."""
        return self.session.store()

    def prepare(self, args: dict) -> dict:
        """``args`` as ``run`` takes them: the aliases read as the tool's own names, and the draft branch, the
        deprecated nodes and the named graph a call asks for set on the session's reader. Raises ToolError for a
        draft branch, which the hub resolves before a session, and for a graph the session does not know."""
        if self.name in SIGNATURES:
            args = arguments(self.name, args, *SIGNATURES[self.name])
        else:
            args = dict(args)
        if args.pop("branch", None) not in (None, "", "main"):
            raise ToolError("draft graph branches are read on the hub, which resolves them before a session")
        deprecated = bool(args.pop("deprecated", False))
        scope = self.session.named_scope(args.pop("graph", None))  # a named graph narrows, never widens
        self.session.visibility = self.visibility.asking(deprecated=deprecated, scope=scope)
        return args

    def run(self, **args) -> tuple[str, dict]:
        """The text of the answer and its structured result."""
        raise NotImplementedError

    def held(self) -> frozenset[str] | None:
        """The audience the kg_more handle of a cut answer asks of its reader; None for the audience of what the call
        revealed."""
        return None


class Answered(Tool):
    """A result computed before the call (``session.answered``: its text and data), through the envelope of the
    session; any arguments. Its kg_more handles ask for ``session.held`` when the session has one."""

    def prepare(self, args: dict) -> dict:
        return {}

    def run(self, **args) -> tuple[str, dict]:
        text, data = self.session.answered
        return text, data

    def held(self) -> frozenset[str] | None:
        return getattr(self.session, "held", None)


ToolMaker = Callable[[SessionView], Tool]


class ToolTable(Mapping[str, ToolMaker]):
    """The tools a session answers, by name: a tool class, which names itself and holds its schema, or any maker of
    a tool registered under a name of its own."""

    def __init__(self, tools: Mapping[str, ToolMaker]):
        for name, tool in tools.items():
            if not name or (isinstance(tool, type) and issubclass(tool, Tool) and tool.name not in ("", name)):
                raise ValueError(f"tool {tool!r} is registered as {name!r}, not by its own name")
        self._tools = dict(tools)

    @classmethod
    def of(cls, *tools: type[Tool]) -> ToolTable:
        return cls({tool.name: tool for tool in tools})

    def with_(self, *tools: type[Tool], **named: ToolMaker) -> ToolTable:
        """This table with ``tools`` and ``named`` added, each in place of a tool of the same name."""
        return ToolTable({**self._tools, **{tool.name: tool for tool in tools}, **named})

    def schemas(self) -> list[dict]:
        """The tools MCP lists, in the order of the table."""
        return [tool.schema for tool in self._tools.values() if getattr(tool, "schema", None) is not None]

    def __getitem__(self, name: str) -> ToolMaker:
        return self._tools[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)
