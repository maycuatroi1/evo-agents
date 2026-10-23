"""The gates of autonomy (plan autonomy-levels, criterion a6): before the hub merges, deploys or releases for a run, it
reads the mode of the run's principal for the class of the act, the stricter of the run's snapshot and the value now
(``run_autonomy.recheck``), and acts on it.

- auto: the hub acts.
- notify: the hub acts, then tells the principal (``told``, the notice ``autonomy``).
- ask: the hub opens a decision of category ``autonomy`` to the principal (to the person an intern reports to:
  ``principals.asked_of``) and acts once the principal approves; it
  waits while the decision is open, and leaves the act undone when the principal refuses. A decision cancelled with
  its run is asked again.
- deny: the hub leaves the act undone, with the reason.

A K5 cell ``notify:ask_on_risk_paths`` asks when the merge touches a risk path. An ask that a project policy sets (a
repo in mode off caps K5 at ask, prod_release manual caps K6 at ask: source ``policy:<project>/<repo>``) is left to the
flows that already hold that policy, the merge queue that acts on repos in mode auto alone and the approval of a manual
release; the gate acts as before. A run without a snapshot (a local run, one queued before schema 0113) acts as before
too, and so does a principal at A5.

Every refusal names the class, the mode and its source, and writes the audit row ``autonomy.refused`` once for each
act refused; the ask writes ``autonomy.asked``, the notice ``autonomy.notified``. Every verdict, whatever it is, goes
to the ledger S1 (``autonomy_ledger``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy_levels, runs, tables
from evo_agents.hub.autonomy_levels import (
    ACTING,
    ASK,
    ASK_ON_RISK_PATHS,
    CLASS_NAMES,
    DENY,
    NOTIFY,
    POLICY,
    Choice,
    refusal,
)
from evo_agents.hub.server import audit, autonomy_ledger, principals, run_autonomy
from evo_agents.hub.server.autonomy_ledger import ACTED, ASKED, REFUSED
from evo_agents.hub.server.notifications import decision_link, notify, one_line

APPROVE, REFUSE = "approve", "refuse"
OPTIONS = [{"key": APPROVE, "label": "Approve: the hub acts"}, {"key": REFUSE, "label": "Refuse: leave it undone"}]
AGAIN = ("cancelled", "expired")  # a decision in one of these is asked again


@dataclass(frozen=True)
class Verdict:
    """What a gate does: act or not; the mode it read (None for a run without a snapshot); why it does not act; whether
    it waits for an answer; the principal and the project it tells."""

    act: bool
    mode: str | None = None
    reason: str | None = None
    waits: bool = False
    principal_id: int | None = None
    project_id: int | None = None
    run_id: int | None = None
    klass: str | None = None

    @property
    def tells(self) -> bool:
        return self.act and self.mode == NOTIFY and self.principal_id is not None


def mode_of(choice: Choice, klass: str, risk: bool) -> str:
    """The mode a gate of ``klass`` acts on: an ask of a project policy acts as before, ``notify:ask_on_risk_paths``
    asks on a risk path."""
    if choice.mode == ASK and choice.source.startswith(POLICY):
        return autonomy_levels.AUTO
    if klass == "K5" and choice.mode in ACTING and choice.condition == ASK_ON_RISK_PATHS and risk:
        return ASK
    return choice.mode


async def check(
    conn: AsyncConnection, run_id: int | None, klass: str, *, key: str, what: str, repo: str | None = None, risk=False
) -> Verdict:
    """The verdict of the gate of ``klass`` on run ``run_id`` for the act ``what`` (a sentence: "merge !4 of api into
    dev"), named ``key`` among the run's acts; a run of None acts as before."""
    if run_id is None:
        return Verdict(True)
    found = await run_autonomy.recheck(conn, run_id, repo=repo)
    if found is None:
        return Verdict(True)
    r = tables.runs
    row = (await conn.execute(select(r.c.principal_id, r.c.project_id).where(r.c.id == run_id))).one()
    choice = found.classes[klass]
    mode = mode_of(choice, klass, risk)
    said = Verdict(True, mode, None, False, row.principal_id, row.project_id, run_id, klass)
    if mode in ACTING:
        verdict = said
    elif mode == DENY:
        verdict = await _refuse(conn, said, f"{what}: {refusal(klass, mode, choice)}", key)
    else:
        verdict = await _ask(conn, said, choice, key, what)
    result = ACTED if verdict.act else ASKED if verdict.waits else REFUSED
    await autonomy_ledger.record(conn, run_id, klass, mode, result, key)
    return verdict


async def _refuse(conn: AsyncConnection, said: Verdict, reason: str, key: str) -> Verdict:
    """The refusal ``reason``, audited once for the act ``key``."""
    a = tables.audit
    target = f"run:{said.run_id} {said.klass} {key} mode={said.mode}"
    seen = exists().where(a.c.action == audit.AUTONOMY_REFUSED, a.c.target == target)
    if not (await conn.execute(select(seen))).scalar_one():
        await audit.record(
            conn,
            actor_id=said.principal_id,
            token_id=None,
            action=audit.AUTONOMY_REFUSED,
            target=target,
            project_id=said.project_id,
        )
    return Verdict(False, said.mode, reason, False, said.principal_id, said.project_id, said.run_id, said.klass)


async def _ask(conn: AsyncConnection, said: Verdict, choice: Choice, key: str, what: str) -> Verdict:
    """Ask the principal, or read the answer of the decision asked before."""
    d = tables.decisions
    query = (
        select(d.c.id, d.c.state, d.c.answer_option)
        .where(d.c.run_id == said.run_id, d.c.category == runs.AUTONOMY_DECISION_CATEGORY, d.c.step_key == key)
        .order_by(d.c.id.desc())
        .limit(1)
    )
    found = (await conn.execute(query)).one_or_none()
    if found is not None and found.state == "answered":
        if found.answer_option == APPROVE:
            return Verdict(True, ASK, None, False, said.principal_id, said.project_id, said.run_id, said.klass)
        why = f"{what}: the principal refused it in decision #{found.id}; {refusal(said.klass, ASK, choice)}"
        return await _refuse(conn, said, why, key)
    decision_id = found.id if found is not None and found.state not in AGAIN else None
    if decision_id is None:
        decision_id = await _open(conn, said, choice, key, what)
    reason = f"{what} waits for the principal's answer to decision #{decision_id} ({decision_link(decision_id)})"
    return Verdict(False, ASK, reason, True, said.principal_id, said.project_id, said.run_id, said.klass)


async def _open(conn: AsyncConnection, said: Verdict, choice: Choice, key: str, what: str) -> int:
    d = tables.decisions
    question = runs.clip(f"May the hub {what} for run {said.run_id}?", runs.MAX_QUESTION_CHARS)
    context = runs.clip(
        f"{refusal(said.klass, ASK, choice)}. The hub acts once you approve, and leaves it undone if you refuse.",
        runs.MAX_DECISION_CONTEXT_BYTES,
    )
    asked = await principals.asked_of(conn, said.run_id)
    values = {"run_id": said.run_id, "project_id": said.project_id, "category": runs.AUTONOMY_DECISION_CATEGORY}
    inserted = insert(d).values(**values, step_key=key, question=question, context=context, options=OPTIONS)
    decision_id = (await conn.execute(inserted.values(asked_of=asked).returning(d.c.id))).scalar_one()
    await notify(
        conn,
        user_id=asked,
        kind="decision",
        project_id=said.project_id,
        run_id=said.run_id,
        decision_id=decision_id,
        title=one_line(f"{said.klass} asks: {what}"),
        body=f"{question}\n\n{context}",
        link=decision_link(decision_id),
    )
    await audit.record(
        conn,
        actor_id=said.principal_id,
        token_id=None,
        action=audit.AUTONOMY_ASKED,
        target=f"run:{said.run_id} {said.klass} {key} decision:{decision_id}",
        project_id=said.project_id,
    )
    return decision_id


async def told(conn: AsyncConnection, said: Verdict, key: str, title: str, body: str | None = None) -> None:
    """Tell the principal of a class in notify what the hub did for the run (the notice ``autonomy``), and the person
    they report to when they are an intern (``principals.told_to``), audited."""
    if not said.tells:
        return
    for user_id in await principals.told_to(conn, said.principal_id):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
            project_id=said.project_id,
            run_id=said.run_id,
            title=one_line(title),
            body=body
            or f"{said.klass} ({CLASS_NAMES[said.klass]}) is notify for the run's principal: the hub acted, "
            "and tells you.",
        )
    await audit.record(
        conn,
        actor_id=said.principal_id,
        token_id=None,
        action=audit.AUTONOMY_NOTIFIED,
        target=f"run:{said.run_id} {said.klass} {key}",
        project_id=said.project_id,
    )


async def tell_after(
    conn: AsyncConnection, run_id: int | None, klass: str, *, key: str, title: str, repo=None, risk=False
) -> None:
    """After the hub acted for run ``run_id``: tell its principal when ``klass`` is notify for them now."""
    if run_id is None:
        return
    found = await run_autonomy.recheck(conn, run_id, repo=repo)
    if found is None or mode_of(found.classes[klass], klass, risk) != NOTIFY:
        return
    r = tables.runs
    row = (await conn.execute(select(r.c.principal_id, r.c.project_id).where(r.c.id == run_id))).one()
    await told(conn, Verdict(True, NOTIFY, None, False, row.principal_id, row.project_id, run_id, klass), key, title)


def pull_act(repo: str, number: int, base: str) -> dict:
    """The act of merging pull request ``number`` of ``repo`` on GitHub, as the gates name it."""
    return {
        "key": f"merge {repo}#{number}",
        "what": f"merge pull request #{number} of {repo} into {base}",
        "repo": repo,
    }
