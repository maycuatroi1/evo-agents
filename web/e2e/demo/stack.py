"""The demo's side of the Playwright stack (``hub_stack.py``), for ``pnpm screenshots`` (``screenshots.shots.ts``).

The spec seeds the demo through the API; three things it cannot do that way are done here, on the stack's database and
in its process:

- POST /demo/kg {project, login, id, plans}: write the shop's three repos (git, with fixed commit times) and its
  harness under the stack's state directory, with ``plans`` as the harness's plans, sync them with the git and harness
  connectors, push the runs as that member (a writer of the project), and run the build the push queued
  (``kg_seed.KgSeeder``). Answers the build.
- POST /demo/retime {now, rows, runs}: set the times the hub stamped while the spec seeded, so every image shows the
  same times and durations whatever day it is taken. ``rows`` first: each {table, where, set} sets the columns of
  ``set`` on the rows ``where`` matches. Then the events of each run of ``runs``: a state event takes the time of its
  run's column for that state (leased_at, started_at, waiting_since), another event the hub stamped after ``now`` two
  seconds after the event before it. Last, every other time the hub stamped after ``now`` becomes a minute before it,
  except the times that must stay real for the stack to work: the lease and expiry times, a worker's last heartbeat,
  tokens and the job queue. The append-only tables (run events, revisions, audit) refuse an update through their
  ``hub_reject_update`` trigger, so those triggers are off while it runs, inside its one transaction, and back on
  before it commits. Answers how many values of each column moved.
- POST /demo/keepalive {workers, runs}: a heartbeat for each worker of ``workers`` and a lease an hour long for each
  run of ``runs``, written in SQL. The heartbeat route would also count the runs' agent time up to the real now, and
  the stack runs no job that reaps runs, so this is what keeps the demo's workers online and its runs held.

Nothing here is shared with the other specs: the routes are the demo's, on the stack ``pnpm screenshots`` starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql

API, WEB, WORKER, HARNESS = "larkspur-api", "larkspur-web", "larkspur-worker", "larkspur-harness"
ORIGIN = "https://github.com/larkspur-shop"
PEOPLE = {
    "maya": ("Maya Lindqvist", "maya@larkspur.example"),
    "tomas": ("Tomás Reyes", "tomas@larkspur.example"),
    "priya": ("Priya Raman", "priya@larkspur.example"),
}

# Each repo: its commits, oldest first, as (who, when, message, {path: text}).
REPOS: dict[str, list[tuple[str, str, str, dict[str, str]]]] = {
    API: [
        (
            "maya",
            "2026-09-02T10:14:00+00:00",
            "Carts and checkout",
            {
                "README.md": (
                    "# Larkspur API\n\nThe shop's HTTP API: carts, checkout, orders and product search.\n\n"
                    "## Running\n\n```\nuv run uvicorn app.main:app --reload\n```\n\n"
                    "## Checkout\n\nA cart becomes an order in app/checkout.py. A declined card is retried by the "
                    "worker (CHK-02); the API only queues the retry.\n"
                ),
                "app/__init__.py": "",
                "app/main.py": (
                    "from fastapi import FastAPI\n\nfrom app import carts, checkout, search\n\n"
                    'app = FastAPI(title="Larkspur API")\n'
                    "app.include_router(carts.router)\napp.include_router(checkout.router)\n"
                    "app.include_router(search.router)\n"
                ),
                "app/carts.py": (
                    '"""Carts: the one a shopper is filling, and the carts they saved for later (CHK-01)."""\n\n'
                    "from fastapi import APIRouter\n\nfrom app.db import session\n\n"
                    'router = APIRouter(prefix="/carts")\n\n\n'
                    "def load_cart(customer_id: int):\n"
                    '    return session().execute("SELECT * FROM carts WHERE customer_id = %s", (customer_id,)).one()\n\n\n'
                    '@router.get("/current")\ndef current(customer_id: int):\n    return load_cart(customer_id)\n'
                ),
                "app/checkout.py": (
                    '"""Checkout: a cart becomes an order; a declined card is queued for retry (CHK-02)."""\n\n'
                    "from fastapi import APIRouter\n\nfrom app.carts import load_cart\nfrom app.queue import enqueue\n\n"
                    'router = APIRouter(prefix="/checkout")\n\n\n'
                    "def queue_payment_retry(order_id: int, attempt: int) -> None:\n"
                    '    enqueue("payments.retry", {"order_id": order_id, "attempt": attempt})\n\n\n'
                    '@router.post("")\ndef place_order(customer_id: int):\n    cart = load_cart(customer_id)\n'
                    "    order = cart.to_order()\n    if order.payment.declined:\n"
                    "        queue_payment_retry(order.id, attempt=1)\n    return order\n"
                ),
                "app/db.py": "def session():\n    raise NotImplementedError\n",
                "app/queue.py": "def enqueue(task: str, payload: dict) -> None:\n    raise NotImplementedError\n",
            },
        ),
        (
            "tomas",
            "2026-10-14T15:42:00+00:00",
            "Saved cart tables (migration 0042)",
            {
                "migrations/0042_saved_carts.sql": (
                    "-- CHK-01: carts a shopper keeps for later, for 30 days\n"
                    "CREATE TABLE saved_carts (\n    id bigserial PRIMARY KEY,\n"
                    "    customer_id bigint NOT NULL REFERENCES customers (id),\n"
                    "    saved_at timestamptz NOT NULL DEFAULT now(),\n"
                    "    expires_at timestamptz NOT NULL DEFAULT now() + interval '30 days'\n);\n\n"
                    "CREATE TABLE saved_cart_items (\n"
                    "    saved_cart_id bigint NOT NULL REFERENCES saved_carts (id) ON DELETE CASCADE,\n"
                    "    product_id bigint NOT NULL REFERENCES products (id),\n    quantity integer NOT NULL\n);\n"
                ),
                "tests/test_carts.py": (
                    "from app.carts import load_cart\n\n\ndef test_load_cart_of_new_customer_is_empty(db):\n"
                    "    assert load_cart(db.customer().id).items == []\n"
                ),
            },
        ),
        (
            "maya",
            "2026-10-19T09:05:00+00:00",
            "Search vector column and GIN index (migration 0043)",
            {
                "app/search.py": (
                    '"""Product search on Postgres full-text (SRCH-01), in place of the hosted index."""\n\n'
                    "from fastapi import APIRouter\n\nfrom app.db import session\n\n"
                    'router = APIRouter(prefix="/search")\n\n'
                    'QUERY = """\nSELECT id, name, ts_rank(search_vector, query) AS rank\n'
                    "FROM products, websearch_to_tsquery('english', %s) AS query\n"
                    'WHERE search_vector @@ query\nORDER BY rank DESC\nLIMIT 20\n"""\n\n\n'
                    '@router.get("")\ndef search_products(q: str):\n'
                    "    return session().execute(QUERY, (q,)).all()\n"
                ),
                "migrations/0043_search_vector.sql": (
                    "-- SRCH-01: weighted name over description\nALTER TABLE products ADD COLUMN search_vector tsvector;\n"
                    "CREATE INDEX products_search_vector ON products USING gin (search_vector);\n"
                ),
            },
        ),
    ],
    WEB: [
        (
            "priya",
            "2026-09-03T13:20:00+00:00",
            "Checkout page",
            {
                "README.md": (
                    "# Larkspur web\n\nThe storefront: product pages, search, cart and checkout, in React.\n\n"
                    "## Running\n\n```\npnpm install\npnpm dev\n```\n\nIt talks to the API through src/api/client.ts.\n"
                ),
                "package.json": json.dumps(
                    {"name": "larkspur-web", "private": True, "scripts": {"dev": "vite", "test": "vitest run"}},
                    indent=2,
                )
                + "\n",
                "src/api/client.ts": (
                    "export type Cart = { id: number; items: { productId: number; quantity: number }[] };\n\n"
                    'const BASE = "/api";\n\n'
                    "export async function currentCart(): Promise<Cart> {\n"
                    "  const response = await fetch(`${BASE}/carts/current`);\n  return response.json();\n}\n\n"
                    "export async function placeOrder(): Promise<{ id: number }> {\n"
                    '  const response = await fetch(`${BASE}/checkout`, { method: "POST" });\n'
                    "  return response.json();\n}\n"
                ),
                "src/checkout/CheckoutPage.tsx": (
                    'import { useEffect, useState } from "react";\n\n'
                    'import { type Cart, currentCart, placeOrder } from "../api/client";\n\n'
                    "export function CheckoutPage() {\n  const [cart, setCart] = useState<Cart | null>(null);\n"
                    "  useEffect(() => {\n    currentCart().then(setCart);\n  }, []);\n"
                    "  if (!cart) return <p>Loading your cart</p>;\n"
                    "  return <button onClick={() => placeOrder()}>Place order</button>;\n}\n"
                ),
            },
        ),
        (
            "tomas",
            "2026-10-16T11:30:00+00:00",
            "Search box",
            {
                "src/search/SearchBox.tsx": (
                    "// SRCH-01: the box moves to the API's /search once the Postgres index is live\n"
                    'import { useState } from "react";\n\n'
                    "export function SearchBox({ onResults }: { onResults: (ids: number[]) => void }) {\n"
                    '  const [query, setQuery] = useState("");\n'
                    "  async function submit() {\n"
                    "    const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);\n"
                    "    onResults((await response.json()).map((row: { id: number }) => row.id));\n  }\n"
                    "  return (\n    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>\n"
                    "      <input value={query} onChange={(event) => setQuery(event.target.value)} />\n"
                    "    </form>\n  );\n}\n"
                ),
                "src/checkout/SavedCarts.tsx": (
                    "// CHK-01: the shopper's saved carts, above the current one\n"
                    'import type { Cart } from "../api/client";\n\n'
                    "export function SavedCarts({ carts, onRestore }: { carts: Cart[]; onRestore: (id: number) => void }) {\n"
                    "  return (\n    <ul>\n      {carts.map((cart) => (\n"
                    "        <li key={cart.id}>\n"
                    "          <button onClick={() => onRestore(cart.id)}>Restore {cart.items.length} items</button>\n"
                    "        </li>\n      ))}\n    </ul>\n  );\n}\n"
                ),
            },
        ),
    ],
    WORKER: [
        (
            "priya",
            "2026-09-05T08:45:00+00:00",
            "Payment and email tasks",
            {
                "README.md": (
                    "# Larkspur worker\n\nBackground tasks of the shop: payment retries (CHK-02), the emails that go "
                    "with them (CHK-03) and search reindexing (SRCH-01).\n\n## Running\n\n```\nuv run python -m worker\n```\n"
                ),
                "worker/__init__.py": "",
                "worker/payments.py": (
                    '"""Retrying a declined card (CHK-02): three tries over 24 hours, then the order fails."""\n\n'
                    "from datetime import timedelta\n\nfrom worker.emails import send_retry_notice\n\n"
                    "BACKOFF = [timedelta(hours=1), timedelta(hours=6), timedelta(hours=17)]\n\n\n"
                    "def retry_payment(order_id: int, attempt: int, charge) -> str:\n"
                    "    if charge(order_id).ok:\n"
                    '        return "paid"\n'
                    "    if attempt >= len(BACKOFF):\n"
                    '        return "failed"\n'
                    "    send_retry_notice(order_id, BACKOFF[attempt])\n"
                    '    return "retrying"\n'
                ),
                "worker/emails.py": (
                    '"""The shopper\'s emails about a payment (CHK-03)."""\n\n\n'
                    "def send_retry_notice(order_id: int, after) -> None:\n"
                    '    print(f"order {order_id}: we will try your card again in {after}")\n'
                ),
            },
        ),
        (
            "maya",
            "2026-10-20T14:11:00+00:00",
            "Backfill job for product vectors",
            {
                "worker/reindex.py": (
                    '"""Fill products.search_vector in batches (SRCH-01); a stopped run resumes after its last id."""\n\n'
                    "BATCH = 500\n\n\n"
                    "def reindex(conn, after_id: int = 0) -> int:\n    done = 0\n    while True:\n"
                    "        rows = conn.execute(\n"
                    "            \"UPDATE products SET search_vector = setweight(to_tsvector('english', name), 'A')\"\n"
                    "            \" || setweight(to_tsvector('english', coalesce(description, '')), 'B')\"\n"
                    '            " WHERE id IN (SELECT id FROM products WHERE id > %s ORDER BY id LIMIT %s) RETURNING id",\n'
                    "            (after_id, BATCH),\n        ).all()\n"
                    "        if not rows:\n            return done\n"
                    "        after_id = max(row.id for row in rows)\n        done += len(rows)\n"
                ),
            },
        ),
    ],
}

CONTRACTS = f"""\
seams:
  - name: saved-carts-api
    owner: {API}
    consumers: [{WEB}]
    source: {{repo: {API}, path: app/carts.py}}
    verify: uv run pytest tests/test_carts.py
  - name: payment-retry-queue
    owner: {API}
    consumers: [{WORKER}]
    source: {{repo: {API}, path: app/checkout.py}}
    verify: uv run pytest tests/test_payments.py
  - name: product-search
    owner: {API}
    consumers: [{WEB}, {WORKER}]
    source: {{repo: {API}, path: app/search.py}}
    verify: uv run pytest tests/test_search.py
"""

CLUSTER = f"""\
# Larkspur

The shop's code: {API} (HTTP API), {WEB} (storefront) and {WORKER} (background tasks). Plans live in plans/active.

- CHK-01: a shopper keeps a cart across devices (saved carts).
- CHK-02: a declined card is retried before the order fails.
- CHK-03: the shopper hears about each retry by email.
- SRCH-01: product search runs on Postgres full-text.
"""


BINDINGS = f"""\
version: 1
bindings:
  - {{from: "{API}:app/carts.py::load_cart", rel: implements, to: usecase:CHK-01}}
  - {{from: "{API}:app/checkout.py::queue_payment_retry", rel: implements, to: usecase:CHK-02}}
  - {{from: "{WORKER}:worker/payments.py::retry_payment", rel: implements, to: usecase:CHK-02}}
  - {{from: "{WORKER}:worker/emails.py::send_retry_notice", rel: implements, to: usecase:CHK-03}}
  - {{from: "{API}:app/search.py::search_products", rel: implements, to: usecase:SRCH-01}}
  - {{from: "{WORKER}:worker/reindex.py::reindex", rel: implements, to: usecase:SRCH-01}}
"""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    command = ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args]
    subprocess.run(command, check=True, capture_output=True, text=True, env={**os.environ, **(env or {})})


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def write_repos(base: Path) -> None:
    """The three repos under ``base``, each a git repo on main whose origin is on github.com."""
    for name, commits in REPOS.items():
        repo = base / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "remote", "add", "origin", f"{ORIGIN}/{name}")
        for who, when, message, files in commits:
            _write(repo, files)
            person, email = PEOPLE[who]
            env = {
                "GIT_AUTHOR_NAME": person,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": when,
                "GIT_COMMITTER_NAME": person,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_COMMITTER_DATE": when,
            }
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", message, env=env)


def write_harness(base: Path, project: str, plans: list[dict]) -> Path:
    """The harness beside the repos, with ``plans`` (bodies as the hub holds them) in plans/active."""
    root = base / HARNESS
    repos = "".join(
        f"  - name: {name}\n    path: {name}\n    origin: {ORIGIN}/{name}\n    default_branch: main\n" for name in REPOS
    )
    sources = "".join(
        f"  - id: {name}\n    connector: git\n    repo: {name}\n    pulls: false\n"
        "    label: {level: internal, integrity: U}\n"
        for name in REPOS
    )
    files = {
        "harness.yaml": f"name: {project}\nworkspace: ..\nknowledge_file: knowledge.yaml\nrepos:\n{repos}",
        "knowledge.yaml": (
            f"version: 1\nproject: {project}\npolicy:\n  levels: [public, internal, customer, secret]\n  sinks:\n"
            "    - {id: claude-code, kind: agent-session, clearance: {level: customer}}\n"
            "    - {id: hub, kind: hub, clearance: {level: internal}}\n"
            "identifiers:\n  - {kind: UseCase, pattern: '\\b(CHK|SRCH)-\\d{2}\\b'}\n"
            "sources:\n  - id: harness\n    connector: harness\n    label: {level: internal, integrity: U}\n"
            f"{sources}"
        ),
        "contracts.yaml": CONTRACTS,
        "CLUSTER.md": CLUSTER,
        "bindings/checkout.yaml": BINDINGS,
        **{f"plans/active/{plan['id']}.yaml": json.dumps(plan, indent=2) + "\n" for plan in plans},
    }
    _write(root, files)
    return root


class Demo:
    """The routes above, on the stack's database ``dsn``; ``seeder`` pushes and builds as ``kg_seed`` does."""

    def __init__(self, seeder, dsn: str, work_dir: Path):
        self.seeder = seeder
        self.dsn = dsn
        self.work_dir = work_dir

    def handle(self, path: str, body: dict) -> dict:
        if path == "/demo/kg":
            return self.kg(body)
        if path == "/demo/retime":
            return self.retime(body)
        if path == "/demo/keepalive":
            return self.keepalive(body)
        raise KeyError(path)

    def kg(self, body: dict) -> dict:
        from evo_agents.hub.kg_push import Pusher
        from evo_agents.kg.project import load_project_at
        from evo_agents.kg.sync import sync_project

        project = str(body["project"])
        base = self.work_dir / project
        shutil.rmtree(base, ignore_errors=True)
        write_repos(base)
        loaded = load_project_at(write_harness(base, project, list(body["plans"])), base / "kg-home")
        problems = [result.issues for result in sync_project(loaded) if not result.ok]
        if problems:
            raise RuntimeError(f"the demo's sync failed: {problems}")
        hub = self.seeder.member(body)
        report = Pusher(hub, loaded).push()
        if not report.ok:
            raise RuntimeError(f"the demo's push failed: {report.errors}")
        return self.seeder.finish(project, hub, report.build)

    def retime(self, body: dict) -> dict:
        now = datetime.fromisoformat(str(body["now"]).replace("Z", "+00:00"))
        moved: dict[str, int] = {}
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            columns = _columns(cur)
            append_only = _append_only(cur)
            _set_triggers(cur, append_only, "DISABLE")
            for row in body.get("rows", []):
                table, where, values = str(row["table"]), dict(row["where"]), dict(row["set"])
                unknown = {*where, *values} - columns.get(table, {}).keys()
                if unknown:
                    raise ValueError(f"no such columns of {table}: {sorted(unknown)}")
                statement = sql.SQL("UPDATE {} SET {} WHERE {}").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in values),
                    sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in where),
                )
                cur.execute(statement, [*values.values(), *where.values()])
                if cur.rowcount == 0:
                    raise ValueError(f"no row of {table} where {where}")
            for run_id in body.get("runs", []):
                moved["run_events.at"] = moved.get("run_events.at", 0) + _retime_events(cur, int(run_id), now)
            for table, names in columns.items():
                for name, kind in names.items():
                    if kind != "timestamp with time zone" or _kept(table, name):
                        continue
                    cur.execute(
                        sql.SQL("UPDATE {} SET {} = %s WHERE {} > %s AND {} <= now()").format(
                            sql.Identifier(table), sql.Identifier(name), sql.Identifier(name), sql.Identifier(name)
                        ),
                        (now - timedelta(minutes=1), now),
                    )
                    if cur.rowcount:
                        moved[f"{table}.{name}"] = moved.get(f"{table}.{name}", 0) + cur.rowcount
            _set_triggers(cur, append_only, "ENABLE")
        return {"moved": moved}

    def keepalive(self, body: dict) -> dict:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE workers SET last_heartbeat_at = now() WHERE id = ANY(%s)", (list(body["workers"]),))
            workers = cur.rowcount
            cur.execute(
                "UPDATE runs SET lease_expires_at = now() + interval '1 hour' WHERE id = ANY(%s)",
                (list(body["runs"]),),
            )
            return {"workers": workers, "runs": cur.rowcount}


def _columns(cur) -> dict[str, dict[str, str]]:
    """Each table of the public schema: its columns and their types."""
    cur.execute(
        "SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c"
        " JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name"
        " WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
    )
    found: dict[str, dict[str, str]] = {}
    for table, name, kind in cur.fetchall():
        found.setdefault(table, {})[name] = kind
    return found


def _append_only(cur) -> list[tuple[str, str]]:
    """The tables whose rows are never updated, each with the trigger that refuses it (``hub_reject_update``)."""
    cur.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_proc p ON p.oid = t.tgfoid WHERE p.proname = 'hub_reject_update' AND NOT t.tgisinternal"
    )
    return [(table, trigger) for table, trigger in cur.fetchall()]


def _set_triggers(cur, triggers: list[tuple[str, str]], action: str) -> None:
    """DISABLE or ENABLE each of ``triggers``: the stack's role owns its tables, so no superuser is needed."""
    for table, trigger in triggers:
        cur.execute(
            sql.SQL("ALTER TABLE {} {} TRIGGER {}").format(
                sql.Identifier(table), sql.SQL(action), sql.Identifier(trigger)
            )
        )


# Times that must stay real: the job queue, tokens and leases, expiries, and a worker's last heartbeat (keepalive).
KEPT_TABLES = ("procrastinate_", "tokens", "plugin_tokens", "credential_leases")
KEPT_COLUMNS = {("workers", "last_heartbeat_at"), ("runs", "lease_expires_at")}


def _kept(table: str, column: str) -> bool:
    return table.startswith(KEPT_TABLES) or "expires" in column or (table, column) in KEPT_COLUMNS


def _retime_events(cur, run_id: int, now: datetime) -> int:
    """The events of run ``run_id`` at the times of the story: see the module's /demo/retime. How many moved."""
    cur.execute("SELECT queued_at, leased_at, started_at, waiting_since FROM runs WHERE id = %s", (run_id,))
    queued, leased, started, waiting = cur.fetchone()
    at_state = {"queued": queued, "leased": leased, "running": started, "waiting": waiting}
    cur.execute("SELECT seq, at, kind, body FROM run_events WHERE run_id = %s ORDER BY seq", (run_id,))
    previous, moved = queued, 0
    for seq, at, kind, body in cur.fetchall():
        target = at
        if kind == "state" and isinstance(body, dict) and at_state.get(body.get("to")) is not None:
            target = at_state[body["to"]]
        elif at > now:
            target = previous + timedelta(seconds=2)
        if target != at:
            cur.execute("UPDATE run_events SET at = %s WHERE run_id = %s AND seq = %s", (target, run_id, seq))
            moved += 1
        previous = target
    return moved
