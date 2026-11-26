"""One transaction of a request: ``async with UnitOfWork(engine) as conn`` commits when the block ends, and rolls
back when it raises."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class UnitOfWork:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._begun: AbstractAsyncContextManager[AsyncConnection] | None = None

    async def __aenter__(self) -> AsyncConnection:
        self._begun = self.engine.begin()
        return await self._begun.__aenter__()

    async def __aexit__(self, *exc) -> bool | None:
        assert self._begun is not None
        begun, self._begun = self._begun, None
        return await begun.__aexit__(*exc)
