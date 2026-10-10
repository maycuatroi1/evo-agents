"""Optional paging of the lists the API answers with a bare JSON array.

GET /v1/projects, /v1/projects/{project}/plans, /v1/skills, /v1/workers and /v1/admin/users take ``limit`` (1 to
MAX_LIMIT) and ``offset``. Without either the answer is the whole list, as it always was, so a client that does not
page (the command line, ``mirror.py``, the web) reads what it read before. With them it is that slice of the list, in
the list's own order. The body stays a list either way, and the header X-Total-Count holds the length of the whole
list, so a client asks for the next page until ``offset`` plus what it got reaches that count.

A list the database filters is cut in SQL (``Page.rows``); a list filtered after the query, as the label rule filters
plans, is cut after that filter (``Page.of``), so the count and the page are what the caller may see.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query, Response
from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

MAX_LIMIT = 1000
MAX_OFFSET = 1_000_000  # far past any list here, and an OFFSET Postgres always takes
TOTAL_COUNT = "X-Total-Count"

# The 200 of a paged list, for a route's ``responses``: the X-Total-Count header it carries.
PAGED: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            TOTAL_COUNT: {
                "description": "the length of the whole list, whatever limit and offset cut from it",
                "schema": {"type": "integer", "minimum": 0},
            }
        }
    }
}


@dataclass(frozen=True)
class Page:
    """The slice of a list a request asked for: at most ``limit`` items (None: no limit) after the first ``offset``.
    ``response`` carries X-Total-Count; a handler called as a function, as the MCP tools call it, has none."""

    limit: int | None = None
    offset: int = 0
    response: Response | None = None

    @property
    def whole(self) -> bool:
        return self.limit is None and self.offset == 0

    def of(self, items: list) -> list:
        """The page of ``items``, the whole list in its order, with X-Total-Count set to the length of ``items``."""
        self._total(len(items))
        end = None if self.limit is None else self.offset + self.limit
        return items[self.offset : end]

    async def rows(self, conn: AsyncConnection, query: Select) -> Sequence[Row]:
        """The rows of ``query`` on the page, cut in SQL, with X-Total-Count set to the number of rows of the whole
        query. Only a paged request runs the count."""
        if self.whole:
            rows = (await conn.execute(query)).all()
            self._total(len(rows))
            return rows
        counted = select(func.count()).select_from(query.order_by(None).subquery())
        total = (await conn.execute(counted)).scalar_one()
        self._total(total)
        if self.offset >= total:
            return []
        return (await conn.execute(query.limit(self.limit).offset(self.offset))).all()

    def _total(self, count: int) -> None:
        if self.response is not None:
            self.response.headers[TOTAL_COUNT] = str(count)


# The whole list: the default of a paged handler called as a function.
WHOLE = Page()


def _asked(
    response: Response,
    limit: Annotated[
        int | None, Query(ge=1, le=MAX_LIMIT, description="at most this many items; without it, all of them")
    ] = None,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET, description="skip this many items first")] = 0,
) -> Page:
    return Page(limit, offset, response)


# The page a request asks for, as a handler's parameter: ``page: Paging = WHOLE``.
Paging = Annotated[Page, Depends(_asked)]
