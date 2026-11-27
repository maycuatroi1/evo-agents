"""A project's data loss prevention policy (``evo_agents.hub.server.dlp_policy``): its enforcement before a model call
leaves the hub, and what its routes read, set and inspect."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import dlp
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.hub_admin.models.dlp_policy import (
    Blocked,
    DlpCount,
    DlpFinding,
    DlpInspection,
    DlpPolicy,
    DlpPolicyWrite,
)
from evo_agents.hub.server.hub_admin.repository import dlp_policy as repository
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import holds

log = logging.getLogger("evo_agents.hub.server.dlp_policy")  # the name its log lines had


async def policy_of(conn: AsyncConnection, project_id: int) -> dlp.Policy:
    """The policy of project ``project_id``: off when its admin set none."""
    row = await repository.policy_rules(conn, project_id)
    if row is None:
        return dlp.Policy()
    return dlp.Policy(row.mode, tuple(row.kinds), tuple(row.keywords))


async def count(conn: AsyncConnection, project_id: int, surface: str, action: str, counts) -> None:
    """Add ``counts`` (by rule kind) to dlp_counts of ``project_id``, ``surface`` and ``action``."""
    await repository.add_counts(conn, project_id, surface, action, counts)


async def enforce(engine: AsyncEngine, project_id: int, surface: str, content: bytes, *, run_id: int) -> bytes:
    """The JSON body ``content`` of a model call of run ``run_id`` as its project's policy sends it; ``Blocked`` when
    the policy blocks it. What the policy found is counted on ``surface``."""
    async with UnitOfWork(engine) as conn:
        policy = await policy_of(conn, project_id)
        if not policy.on:
            return content
        made, scan = dlp.apply_bytes(policy, content)
        if not scan.counts:
            return content
        action = "blocked" if scan.blocked else "redacted"
        await count(conn, project_id, surface, action, scan.counts)
    log.info("dlp " + action, extra={"run_id": run_id, "surface": surface, "kinds": dict(scan.counts)})
    if scan.blocked:
        raise Blocked(scan.counts)
    return made


async def enforce_json(
    engine: AsyncEngine, project_id: int, surface: str, body: dict, *, run_id: int, fresh: list[str] | None = None
) -> dict:
    """``enforce`` on a body not encoded yet. With ``fresh``, the texts of ``body`` new to the provider (the message
    that woke a hub turn and its inbox), their matches alone count and block: the rest of ``body``, the chat as the
    store keeps it, which an earlier turn counted or was blocked on, goes redacted and counts nothing."""
    async with UnitOfWork(engine) as conn:
        policy = await policy_of(conn, project_id)
        if not policy.on:
            return body
        if fresh is None:
            made, scan = dlp.apply_json(policy, body)
        else:
            scan = dlp.Scan("")
            for text in fresh:
                scan.add(dlp.apply(policy, text))
            made, _ = dlp.apply_json(dlp.Policy("redact", policy.kinds, policy.keywords), body)
        if not scan.counts:
            return made
        action = "blocked" if scan.blocked else "redacted"
        await count(conn, project_id, surface, action, scan.counts)
    log.info("dlp " + action, extra={"run_id": run_id, "surface": surface, "kinds": dict(scan.counts)})
    if scan.blocked:
        raise Blocked(scan.counts)
    return made


def reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the DLP policy of project {access.name} needs a grant on it")


async def view(conn: AsyncConnection, access: ProjectAccess) -> DlpPolicy:
    row = await repository.policy_row(conn, access.project_id)
    counted = await repository.count_rows(conn, access.project_id)
    counts = [DlpCount(kind=r.kind, surface=r.surface, action=r.action, count=r.count) for r in counted]  # pyright: ignore[reportArgumentType]
    builtin: list[str] = list(dlp.CONTRACT_KEYWORDS)
    if row is None:
        return DlpPolicy(
            project=access.name,
            mode="off",
            kinds=list(dlp.KINDS),
            keywords=[],
            builtin_keywords=builtin,
            updated_at=None,
            counts=counts,
        )
    return DlpPolicy(
        project=access.name,
        mode=row.mode,
        kinds=list(row.kinds),
        keywords=list(row.keywords),
        builtin_keywords=builtin,
        updated_at=row.updated_at.isoformat(),
        counts=counts,
    )


async def show(engine: AsyncEngine, user, project: str) -> DlpPolicy:
    """The project's DLP policy, off when none was set, and the counts of what it redacted and blocked."""
    async with repository.reading(engine) as conn:
        access = await project_access(conn, user, project)
        reader(access)
        return await view(conn, access)


async def write(engine: AsyncEngine, user, project: str, body: DlpPolicyWrite) -> DlpPolicy:
    """Set the project's DLP policy, for its admin, with its audit row."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "admin"):
            held = access.role or "no grant"
            raise HTTPException(
                403, f"setting the DLP policy of project {project} needs the admin role; you hold {held}"
            )
        values = {
            "mode": body.mode,
            "kinds": sorted(set(body.kinds), key=dlp.KINDS.index),
            "keywords": sorted({word.strip() for word in body.keywords if word.strip()}),
            "updated_by": user.user_id,
        }
        await repository.put_policy(conn, access.project_id, values)
        target = f"{project} mode={body.mode} kinds={len(values['kinds'])} keywords={len(values['keywords'])}"
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.DLP_POLICY,
            target=target,
            project_id=access.project_id,
        )
        return await view(conn, access)


async def inspect(engine: AsyncEngine, user, project: str, text: str) -> DlpInspection:
    """What the project's policy makes of ``text``; nothing is stored or counted."""
    async with repository.reading(engine) as conn:
        access = await project_access(conn, user, project)
        reader(access)
        policy = await policy_of(conn, access.project_id)
    scan = dlp.apply(policy, text)
    findings = [DlpFinding(kind=kind, count=n) for kind, n in sorted(scan.counts.items(), key=lambda k: k[0])]
    return DlpInspection(mode=policy.mode, blocked=scan.blocked, text=scan.text, findings=findings)  # type: ignore[arg-type]
