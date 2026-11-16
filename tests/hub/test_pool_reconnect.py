"""The hub's pool after a Postgres that went away without closing its connections (``evo_agents.hub.db``): its
container killed on a stalled host leaves every pooled connection open on this side with nobody answering, and a
check on one used to wait forever, so the api took no connection again until it was restarted by hand."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from evo_agents.hub import db


class Blackhole:
    """A TCP relay to Postgres. ``kill()`` stands for the server going away without a reset: the connections relayed
    so far stay open and nothing answers on them any more, and while ``down`` a new connection is closed at once (the
    server restarting); after ``up()`` new connections reach Postgres again, the server that came back."""

    def __init__(self, host: str, port: int):
        self.target = host, port
        self.dead: list[asyncio.Event] = []
        self.down = False

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._relay, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def kill(self) -> None:
        self.down = True
        for dead in self.dead:
            dead.set()

    def up(self) -> None:
        self.down = False

    async def _relay(self, reader, writer):
        if self.down:
            writer.close()
            return
        up_reader, up_writer = await asyncio.open_connection(*self.target)
        dead = asyncio.Event()
        self.dead.append(dead)

        async def pipe(src, dst):
            while data := await src.read(65536):
                if dead.is_set():
                    continue  # read and dropped: the client's bytes reach nobody, and nothing comes back
                dst.write(data)
                await dst.drain()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer), return_exceptions=True)

    async def close(self) -> None:
        self.server.close()


async def settled(coro, seconds: float):
    """What ``coro`` returns or raises within ``seconds``, without cancelling it: a cancelled check would drop the
    dead connection, and the test must see what the pool does on its own."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    assert done, f"still waiting for a connection after {seconds} s"
    return task.result()


async def answered(request, seconds: float):
    """The first answer of ``request``, asked again as traffic would while it fails, within ``seconds`` in all."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        left = deadline - asyncio.get_running_loop().time()
        try:
            return await settled(request(), max(left, 0.1))
        except AssertionError:
            raise
        except Exception:
            if left <= 0:
                raise
            await asyncio.sleep(0.5)


def test_the_pool_reconnects_by_itself_after_postgres_goes_away_without_closing_its_connections(monkeypatch):
    monkeypatch.setattr(db, "CHECK_TIMEOUT", 1.0, raising=False)
    info = psycopg.conninfo.conninfo_to_dict(pg.DSN)

    async def go():
        relay = Blackhole(info.get("host") or "127.0.0.1", int(info.get("port") or 5432))
        port = await relay.start()
        dsn = psycopg.conninfo.make_conninfo(pg.DSN, host="127.0.0.1", port=port)
        config = SimpleNamespace(dsn=dsn, pool_min_size=2, pool_max_size=2, pool_timeout=3.0)
        pool = db.create_pool(config)
        await pool.open(wait=True, timeout=10)
        engine = db.make_engine(pool)

        async def select_one():
            async with engine.begin() as conn:
                return (await conn.execute(text("select 1"))).scalar_one()

        try:
            assert await select_one() == 1
            relay.kill()  # every pooled connection now leads nowhere, and Postgres is down
            with pytest.raises(OperationalError):  # no connection to be had: the request fails within a bounded time
                await settled(select_one(), 10)
            relay.up()  # Postgres is back: the pool connects again within a bounded time, without a restart
            assert await answered(select_one, 45) == 1
        finally:
            await engine.dispose()
            with contextlib.suppress(TimeoutError):  # a check still waiting on main
                await asyncio.wait_for(pool.close(timeout=1), 5)
            await relay.close()

    asyncio.run(asyncio.wait_for(go(), 120))


def test_pooled_connections_carry_tcp_keepalives():
    async def go():
        pool = db.create_pool(SimpleNamespace(dsn=pg.DSN, pool_min_size=1, pool_max_size=1, pool_timeout=5.0))
        await pool.open(wait=True, timeout=10)
        try:
            async with pool.connection() as conn:
                params = conn.info.get_parameters()
                return {k: params.get(k) for k in ("keepalives", "keepalives_idle", "tcp_user_timeout")}
        finally:
            await pool.close()

    assert asyncio.run(go()) == {"keepalives": "1", "keepalives_idle": "10", "tcp_user_timeout": "30000"}
