"""The hub runtime: the turns of a chat run of an agent whose profile runs on the hub (``runs_on: hub``), each taken by
the job ``hub.hub_turn`` (``evo_agents.hub.worker``) with no worker and no pool. docs/hub.md, section Hub runtime.

A run of runtime ``hub`` queues as any chat run (``chat_runs._start``); ``run_state.notify_queued`` defers its job in
the same transaction. ``take_turn`` holds the run (queued, leased and running as the actor worker, leased for
TURN_SECONDS and a margin, so the lease reaper ends a turn whose job died), then:

1. Without a model gateway the agent replies that the hub runtime is not configured, and the run ends done.
2. A system whose hub agents spent its ``limits.daily_usd`` today (``daily_problem``) takes no turn: the agent says so
   and the system's owner gets the notice ``system_daily_cap`` once a day.
3. The model is the run's (the profile's model axis, as ``chat_runs._start`` picked it), through the provider accounts
   the run may use (``gateway_accounts.usable_accounts``), with a key of the gateway made for the turn alone and
   revoked at its end.
4. The prompt is the worker's (``chat_runs.chat_prompt``, the same rules), the profile's instructions and ON_HUB, and
   the messages of the run's inbox, which the turn takes. The model calls the tools of ``hub_tools`` for at most
   MAX_ROUNDS rounds and TURN_SECONDS; messages that reach the inbox meanwhile join before the next round. A round
   whose tools asked an agent or escalated ends the turn, as the worker's agent ends its turn after the command: the
   answer then wakes a new run instead of reaching a turn that goes on searching.
5. Its last answer, masked as the worker's reply is, is the agent's reply in the thread (``chat_runs.agent_reply``).
   The turn's tokens and the key's cost go to run_spend, runs.usage and the attributes of the run's invoke_agent span.
6. A run that asked an agent and waits for the answer parks (waiting, then parked), and the answer wakes it in a new
   run (``run_waits.wake``); any other ends done, and messages left in its inbox go on in a new run
   (``chat_runs.chat_run_moved``). A turn that raises fails the run with the error's kind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import runs, system_context, system_spec, tables
from evo_agents.hub.server import gateway_accounts, gateway_sync, hub_tools, orchestrator, system_traces
from evo_agents.hub.server.agent_account_mask import mask_run, mask_values
from evo_agents.hub.server.chat_runs import agent_reply, chat_prompt
from evo_agents.hub.server.gateway_client import GatewayClient, GatewayError, GatewayOff
from evo_agents.hub.server.gateway_leases import used_with
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.profile_store import by_id, version_rules
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.spend_store import write_figures

log = logging.getLogger(__name__)

TURN_SECONDS = 180  # the time of one turn, the model's answers and the tools' included
MAX_ROUNDS = 8  # the model's answers in one turn
LEASE_MARGIN = 120  # seconds the run's lease outlives TURN_SECONDS, before the lease reaper ends a turn whose job died
KEY_GRACE = 300  # seconds the turn's gateway key outlives TURN_SECONDS, in case its revocation fails
KEY_NAME = "evo-hub-run-{run_id}"
CAP_NOTICE = "system_daily_cap"
PARKED = "it waits for the answer of an agent it asked, which resumes it"
OFF = (
    "The hub runtime is not configured: this hub has no model gateway (EVO_HUB_GATEWAY_SECRET, docs/hub.md section "
    "Hub runtime), so @{handle}, an agent that runs on the hub, cannot answer here."
)
ON_HUB = """# On the hub

You run on the hub, without a worker, a checkout or a shell: the commands above are tools you call.

- `evo-agents worker chat post`: the tool chat_post. `--interactive` with an HTML file: the tool interactive_answer,
  with the same rules.
- `evo-agents worker agent ask`: the tool agent_ask, with the same brief. `evo-agents worker agent forward`: the tool
  agent_forward. Escalating to your system's Orchestrator: the tool escalate.
- The knowledge graph: the tool kg_search. A memory of the project: the tool memory_write.
- Repos, branches, pull requests and other commands are not on the hub: say what you cannot do.

Your last answer without a tool call is your reply in the thread, and ends your turn. A call of agent_ask or escalate
ends your turn too: the answer wakes you in a new turn."""


@dataclass(frozen=True)
class Held:
    """The run a turn holds, as it read it."""

    run_id: int
    project_id: int
    project: str
    profile_id: int
    profile_version: int | None
    model: str | None
    prefix: str | None
    dispatched_by: int
    principal: int
    branch: str | None
    repos: list
    session: str


def _row_query(run_id: int):
    r, p = tables.runs, tables.projects
    return (
        select(
            r.c.state,
            r.c.requested_runtime,
            r.c.project_id,
            p.c.name.label("project"),
            r.c.profile_id,
            r.c.profile_version,
            r.c.model,
            r.c.gateway_provider,
            r.c.dispatched_by,
            r.c.principal_id,
            r.c.branch,
            r.c.repos,
            r.c.session_id,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )


async def _hold(engine: AsyncEngine, run_id: int) -> Held | None:
    """Take run ``run_id`` from queued to running for a turn; None when it is not a queued run of the hub runtime."""
    async with engine.begin() as conn:
        row = (await conn.execute(_row_query(run_id))).one_or_none()
        if row is None or row.state != "queued" or row.requested_runtime != runs.HUB_RUNTIME:
            return None
        session = row.session_id or f"hub-{run_id}"
        columns = {"runtime": runs.HUB_RUNTIME, "session_id": session}
        lease = timedelta(seconds=TURN_SECONDS + LEASE_MARGIN)
        why = "the hub runtime takes its turn"
        await move_run(conn, run_id, "queued", "leased", "worker", reason=why, columns=columns, lease=lease)
        await move_run(conn, run_id, "leased", "running", "worker", reason=why)
    return Held(
        run_id,
        row.project_id,
        row.project,
        row.profile_id,
        row.profile_version,
        row.model,
        row.gateway_provider,
        row.dispatched_by,
        row.principal_id or row.dispatched_by,
        row.branch,
        list(row.repos or []),
        session,
    )


# The daily cap of a system


async def spent_today(conn, system_id: int) -> float:
    """What the hub agents of system ``system_id`` spent today (UTC): run_spend of the runs of runtime hub queued
    today that have an invoke_agent span in a trace of the system."""
    s, t, r, rs = tables.system_spans, tables.system_traces, tables.runs, tables.run_spend
    in_system = (
        select(s.c.run_id)
        .join_from(s, t, t.c.id == s.c.trace_id)
        .where(t.c.system_id == system_id, s.c.name == system_context.INVOKE, s.c.run_id.is_not(None))
    )
    cost = func.coalesce(func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)), 0)
    query = (
        select(cost)
        .join_from(rs, r, r.c.id == rs.c.run_id)
        .where(
            r.c.runtime == runs.HUB_RUNTIME,
            r.c.queued_at >= func.date_trunc("day", func.now()),
            r.c.id.in_(in_system),
        )
    )
    return float((await conn.execute(query)).scalar_one() or 0)


def daily_limit(spec: dict) -> float:
    """``limits.daily_usd`` of a system's spec, the default of ``system_spec.LIMITS`` when it sets none."""
    return float((spec.get("limits") or {}).get("daily_usd", system_spec.LIMITS["daily_usd"][0]))


async def daily_problem(conn, run_id: int) -> str | None:
    """Why run ``run_id``'s agent takes no turn today: its system's hub agents reached ``limits.daily_usd``. None while
    they have not, or for a run in no system; the system's owner is told once a day."""
    found = await system_traces.of_run(conn, run_id)
    if found is None or found.system.id is None:
        return None
    limit, spent = daily_limit(found.spec), await spent_today(conn, found.system.id)
    if spent < limit:
        return None
    why = (
        f"the hub agents of system {found.system.slug} spent {spent:.2f} USD today, its limits.daily_usd of "
        f"{limit:.2f} USD: they take turns again tomorrow (UTC), or when its owner raises the limit"
    )
    await _tell_owner(conn, found.system, why)
    return why


async def _tell_owner(conn, system, why: str) -> None:
    sy, n = tables.agent_systems, tables.notifications
    owner = (await conn.execute(select(sy.c.created_by).where(sy.c.id == system.id))).scalar_one_or_none()
    if owner is None:
        return
    told = select(n.c.id).where(
        n.c.user_id == owner,
        n.c.notice_kind == CAP_NOTICE,
        n.c.created_at >= func.date_trunc("day", func.now()),
        n.c.details["system_id"].as_integer() == system.id,
    )
    if (await conn.execute(told.limit(1))).first() is not None:
        return
    await notify(
        conn,
        user_id=owner,
        kind="notice",
        notice_kind=CAP_NOTICE,
        project_id=system.project_id,
        title=f"System {system.slug} reached its daily limit",
        body=why,
        details={"system_id": system.id, "slug": system.slug},
    )


# Ending a turn


async def _end(engine: AsyncEngine, run_id: int, reply: str | None, *, park: bool = False) -> str:
    """Post ``reply`` as the agent's and end the turn of run ``run_id``: parked when ``park``, else done. The state."""
    async with engine.begin() as conn:
        state = (
            await conn.execute(select(tables.runs.c.state).where(tables.runs.c.id == run_id).with_for_update())
        ).scalar_one()
        if state != "running":
            return state
        if reply:
            await agent_reply(conn, run_id, reply)
        if park:
            await move_run(conn, run_id, "running", "waiting", "worker", reason="its agent asked another agent")
            await move_run(conn, run_id, "waiting", "parked", "reaper", reason=PARKED)
            return "parked"
        await move_run(conn, run_id, "running", "verifying", "worker", reason="the hub runtime's turn ended")
        await move_run(conn, run_id, "verifying", "done", "worker", reason="the hub runtime's turn ended")
    return "done"


async def _fail(engine: AsyncEngine, run_id: int, error: str) -> None:
    async with engine.begin() as conn:
        r = tables.runs
        state = (await conn.execute(select(r.c.state).where(r.c.id == run_id).with_for_update())).scalar_one()
        if state in runs.HELD_STATES:
            await move_run(conn, run_id, state, "failed", "worker", reason="the hub runtime failed", error=error)


async def _open_waits(engine: AsyncEngine, run_id: int) -> bool:
    """Whether run ``run_id`` waits for an answer: a wait decision of it still open."""
    d = tables.decisions
    query = select(d.c.id).where(d.c.run_id == run_id, d.c.state == "open", d.c.category == runs.WAIT_DECISION_CATEGORY)
    async with engine.connect() as conn:
        return (await conn.execute(query.limit(1))).first() is not None


# The model


async def _connections(client: GatewayClient, engine: AsyncEngine, held: Held) -> tuple[list[str], str | None]:
    """(the gateway connections of the provider accounts the run may use, why there are none)."""
    if held.prefix is None:
        return [], f"its model {held.model or '(none)'} is not one the hub's gateway serves (the profile's model axis)"
    async with engine.connect() as conn:
        profile = await by_id(conn, held.profile_id)
        assert profile is not None  # _hold holds a run of an agent alone
        rules = await version_rules(conn, held.profile_id, held.profile_version) if held.profile_version else None
        permissions = (rules or {}).get("permissions") or profile.permissions
        asker = gateway_accounts.Asker(
            held.run_id,
            held.dispatched_by,
            held.principal,
            held.project_id,
            held.profile_id,
            profile.project_id,
            agent_owned=not profile.is_default,
            permissions=permissions,
        )
        usable = await gateway_accounts.usable_accounts(conn, asker, None, held.prefix)
    if not usable.accounts:
        return [], usable.why(held.prefix)
    found = {}
    for connection in await client.connections():
        match = gateway_sync.NAME.fullmatch(connection.name)
        if match and connection.active:
            found[int(match.group(1))] = connection.id
    reach = [found[account.secret_id] for account in usable.accounts if account.secret_id in found]
    return reach, None if reach else f"no provider account of {held.prefix} has an active connection at the gateway"


async def _prompt(engine: AsyncEngine, held: Held) -> list[dict]:
    """The first messages of the turn: the profile's instructions, then the worker's prompt and the inbox's."""
    async with engine.begin() as conn:
        profile = await by_id(conn, held.profile_id)
        assert profile is not None  # _hold holds a run of an agent alone
        text = await chat_prompt(conn, held.run_id, held.branch, held.repos)
        inbox = await _take_inbox(conn, held.run_id)
    system = "\n\n".join(part for part in (profile.instructions.strip(), ON_HUB) if part)
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join([text, *inbox])}]


async def _take_inbox(conn, run_id: int) -> list[str]:
    """The messages of run ``run_id``'s inbox nobody handed to its agent yet, oldest first, marked handed."""
    i = tables.run_inbox
    query = select(i.c.id, i.c.body).where(i.c.run_id == run_id, i.c.delivered_at.is_(None)).order_by(i.c.id)
    rows = (await conn.execute(query.with_for_update())).all()
    if rows:
        await conn.execute(update(i).values(delivered_at=func.now()).where(i.c.id.in_([row.id for row in rows])))
    return [row.body for row in rows]


async def _answer(client: GatewayClient, key: str, body: dict, seconds: float) -> dict:
    """The model's answer to ``body`` through the gateway, within ``seconds``."""
    content = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    response = await client.relay("POST", "/v1/chat/completions", key, headers, content)
    try:
        data = await asyncio.wait_for(response.aread(), timeout=max(seconds, 1.0))
    finally:
        await response.aclose()
    if response.status_code >= 400:
        raise GatewayError("answer with the model", response.status_code)
    return json.loads(data)


@dataclass
class _Loop:
    tokens: dict
    reply: str | None = None
    rounds: int = 0
    exhausted: bool = False  # the rounds or the time ran out before a final answer


async def _rounds(client, key: str, held: Held, turn: hub_tools.Turn, messages: list[dict]) -> _Loop:
    """The model's rounds of the turn, each tool call answered, until it answers without one or the bounds stop it."""
    found = _Loop({"input": 0, "output": 0})
    deadline = time.monotonic() + TURN_SECONDS
    while found.rounds < MAX_ROUNDS and time.monotonic() < deadline:
        found.rounds += 1
        body = {"model": held.model, "messages": messages, "tools": hub_tools.TOOLS}
        data = await _answer(client, key, body, deadline - time.monotonic())
        usage = data.get("usage") or {}
        found.tokens["input"] += int(usage.get("prompt_tokens") or 0)
        found.tokens["output"] += int(usage.get("completion_tokens") or 0)
        message = ((data.get("choices") or [{}])[0]).get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            found.reply = message.get("content") or ""
            if not await _between(turn.engine, held.run_id, messages, found.reply):
                return found
            continue
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        asked = len(turn.asked)
        for call in calls:
            function = call.get("function") or {}
            result = await hub_tools.call(turn, function.get("name") or "", function.get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
        if len(turn.asked) > asked:  # an ask or an escalation: the turn ends here, the answer wakes a new one
            found.reply = message.get("content") or ""
            return found
        await _between(turn.engine, held.run_id, messages, None)
    found.exhausted = found.reply is None
    found.reply = found.reply or f"I stopped after {found.rounds} round(s) of this turn without a final answer."
    return found


async def _between(engine: AsyncEngine, run_id: int, messages: list[dict], reply: str | None) -> bool:
    """Add what reached the inbox meanwhile to ``messages`` (after the answer ``reply`` when one ended the round);
    whether something did, so the model answers it in another round."""
    async with engine.begin() as conn:
        inbox = await _take_inbox(conn, run_id)
    if not inbox:
        return False
    if reply is not None:
        messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": "\n\n".join(inbox)})
    return True


async def _spent(engine: AsyncEngine, client: GatewayClient, held: Held, key_id: str, found: _Loop) -> float:
    """Record what the turn spent: the key's cost, the turn's tokens; the cost in USD."""
    try:
        counted = await client.key_usage(key_id)
        cost, requests = counted.cost_usd, counted.requests
    except GatewayError:
        cost, requests = 0.0, found.rounds
    used = {"requests": requests, "prompt_tokens": found.tokens["input"], "completion_tokens": found.tokens["output"]}
    async with engine.begin() as conn:
        r, s = tables.runs, tables.system_spans
        usage = (await conn.execute(select(r.c.usage).where(r.c.id == held.run_id).with_for_update())).scalar_one()
        await conn.execute(
            update(r).values(usage=used_with(usage, {**used, "cost_usd": cost})).where(r.c.id == held.run_id)
        )
        await write_figures(conn, held.run_id, held.session, held.model or "", found.tokens, cost)
        attributes = {
            "gen_ai.request.model": held.model,
            "gen_ai.usage.input_tokens": found.tokens["input"],
            "gen_ai.usage.output_tokens": found.tokens["output"],
            "evo.cost_usd": round(cost, 6),
        }
        span = (s.c.run_id == held.run_id, s.c.name == system_context.INVOKE)
        await conn.execute(
            update(s)
            .values(attributes=func.coalesce(s.c.attributes, literal({}, JSONB)).op("||")(literal(attributes, JSONB)))
            .where(*span)
        )
    return cost


async def _converse(engine, config, blobs, sealer, held: Held, client: GatewayClient) -> dict:
    """The turn of ``held`` through ``client``: its state and figures."""
    reach, why = await _connections(client, engine, held)
    if not reach:
        return {"state": await _end(engine, held.run_id, f"I cannot answer on the hub: {why}.")}
    expires = datetime.now(UTC) + timedelta(seconds=TURN_SECONDS + KEY_GRACE)
    key = await client.create_key(KEY_NAME.format(run_id=held.run_id), reach, expires)
    turn = hub_tools.Turn(engine, config, blobs, sealer, held.run_id, held.project)
    try:
        found = await _rounds(client, key.value.get_secret_value(), held, turn, await _prompt(engine, held))
        cost = await _spent(engine, client, held, key.id, found)
        if found.exhausted:  # the rule rounds: the Orchestrator of its agent, if any, is told
            await orchestrator.on_rounds(engine, held.run_id, found.rounds)
    finally:
        try:
            await client.revoke_key(key.id)
        except GatewayError:
            log.warning("hub turn key not revoked", extra={"run_id": held.run_id})
    async with engine.connect() as conn:
        reply = await mask_values(conn, sealer, held.profile_id, found.reply or "")
        reply = await mask_run(conn, sealer, held.run_id, reply)
    state = await _end(engine, held.run_id, reply, park=await _open_waits(engine, held.run_id))
    return {"state": state, "rounds": found.rounds, **found.tokens, "cost_usd": cost, "asked": turn.asked}


async def take_turn(engine: AsyncEngine, config, blobs, sealer, run_id: int, *, transport=None) -> dict:
    """One turn of chat run ``run_id`` of the hub runtime (see the module); what it did."""
    held = await _hold(engine, run_id)
    if held is None:
        return {"run_id": run_id, "state": None}
    client = GatewayClient.from_config(config, transport)
    try:
        if client is None:
            async with engine.connect() as conn:
                profile = await by_id(conn, held.profile_id)
            assert profile is not None  # _hold holds a run of an agent alone
            handle = profile.handle
            return {"run_id": run_id, "state": await _end(engine, run_id, OFF.format(handle=handle))}
        async with engine.begin() as conn:
            capped = await daily_problem(conn, run_id)
        if capped is not None:
            return {"run_id": run_id, "state": await _end(engine, run_id, f"I take no turn now: {capped}.")}
        return {"run_id": run_id, **await _converse(engine, config, blobs, sealer, held, client)}
    except (GatewayOff, GatewayError, TimeoutError) as exc:
        await _fail(engine, run_id, f"the hub runtime failed: {exc or type(exc).__name__}")
        return {"run_id": run_id, "state": "failed"}
    except Exception as exc:
        log.exception("hub turn failed", extra={"run_id": run_id})
        await _fail(engine, run_id, f"the hub runtime failed: {type(exc).__name__}")
        return {"run_id": run_id, "state": "failed"}
    finally:
        if client is not None:
            await client.aclose()
