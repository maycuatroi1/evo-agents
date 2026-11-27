"""A connector as a class: one run over a source, iterated for its kg/1 messages (docs/kg-protocol.md).

``Connector`` holds how a run ended: an exception it raised, held so the run counts as failed instead of stopping
the sync, and the exit code and stderr of an external command. ``SourceConnector`` is the builtin connectors'
Template Method: ``hello``, the messages of ``items``, then ``closed``, or a ``config`` error and a failed ``closed``
when ``items`` raises ConfigError. ``FunctionConnector`` adapts a connector written as a function of a
ConnectorContext (an entry point, a core plugin's contribution, ``python:module:function``), which yields its own
``hello`` and ``closed``. The registry of the names lives in ``connectors.registry``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from evo_agents import __version__
from evo_agents.harness import Harness
from evo_agents.kg.protocol import hello


@dataclass
class ConnectorContext:
    project: str
    source: dict
    harness: Harness | None = None
    cursor: object = None
    env: dict[str, str] = field(default_factory=dict)
    # The plans of the hub, for the harness connector of a harness with a hub project: what ``kg sync`` gets from
    # evo_agents.hub.kg_plans.reader(), which this package does not import. None: the copies in plans/ are read.
    plans: Callable | None = None

    @property
    def source_id(self) -> str:
        return self.source["id"]

    @property
    def harness_root(self) -> Path | None:
        return self.harness.root if self.harness else None


class ConfigError(ValueError):
    """The source's configuration does not let the connector read it: no checkout, an unknown repo or ref."""


class Connector(ABC):
    """One run of a connector over the source of ``ctx``."""

    def __init__(self, ctx: ConnectorContext):
        self.ctx = ctx
        self.exception: str | None = None
        self.returncode: int | None = None
        self.stderr_tail: deque[str] = deque(maxlen=40)

    def __iter__(self) -> Iterator[dict]:
        try:
            yield from self.messages()
        except Exception as exc:  # the run is reported as failed, not raised
            self.exception = f"{type(exc).__name__}: {exc}"

    @abstractmethod
    def messages(self) -> Iterator[dict]:
        """The kg/1 messages of the run, ``hello`` first and ``closed`` last."""


class SourceConnector(Connector):
    """A builtin connector: ``name`` in its hello, the features ``features`` adds, and the messages of ``items``."""

    name: ClassVar[str]

    def features(self) -> dict:
        return {}

    def messages(self) -> Iterator[dict]:
        yield hello(self.name, __version__, list_complete=True, rev_exact=True, **self.features())
        try:
            yield from self.items()
        except ConfigError as exc:
            yield {"type": "error", "failure": "config", "message": str(exc)}
            yield {"type": "closed", "status": "error", "exception": str(exc)}
            return
        yield {"type": "closed", "status": "ok"}

    @abstractmethod
    def items(self) -> Iterator[dict]:
        """The messages between hello and closed: items, logs, the cursor's state and the listing."""


class FunctionConnector(Connector):
    """Adapter for a connector that is a function of a ConnectorContext yielding every message of its run."""

    def __init__(self, ctx: ConnectorContext, run: Callable[[ConnectorContext], Iterable[dict]]):
        super().__init__(ctx)
        self.run = run

    def messages(self) -> Iterator[dict]:
        yield from self.run(self.ctx)
