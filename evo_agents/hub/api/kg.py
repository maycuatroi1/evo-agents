"""The base the clients of a project's graph share: :class:`KgCollection`, one collection of ``/v1/kg/<project>``
with the call variants the ``hub kg`` commands and the graph's sync ask with (a timeout, characters a part keeps,
a body sent only when there is one), and :func:`asked`, a call with only the arguments given.
"""

from __future__ import annotations

from typing import Any, ClassVar

from evo_agents.hub.api.resource import KgResource
from evo_agents.hub.api.routes import quoted


def asked(hub: Any, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
    """``hub.call`` with the body and the timeout only when there are any."""
    if body is None:
        return hub.call(method, path) if timeout is None else hub.call(method, path, timeout=timeout)
    return hub.call(method, path, body) if timeout is None else hub.call(method, path, body, timeout=timeout)


class KgCollection(KgResource):
    """One collection of a project's graph on the hub (Gateway): ``/v1/kg/<project>/<collection>/<part>...?<query>``.

    ``call`` sends the body only when there is one, unless ``body_always`` (the ``hub kg`` commands' resources pass
    ``None`` as they always have), and the timeout only when one is given."""

    body_always: ClassVar[bool] = False

    def call(
        self,
        method: str,
        *parts: Any,
        body: Any = None,
        timeout: float | None = None,
        safe: str = "",
        **query: Any,
    ) -> Any:
        path = self.path(*parts, safe=safe, **query)
        if body is None and self.body_always:
            return (
                self.hub.call(method, path, None)
                if timeout is None
                else self.hub.call(method, path, None, timeout=timeout)
            )
        return asked(self.hub, method, path, body, timeout)

    def web(self, *parts: Any) -> str:
        """The page of the hub that shows the project's graph, or ``parts`` below it."""
        return f"{self.hub.url}/p" + quoted(self.project) + "/kg" + "".join(f"/{part}" for part in parts)
