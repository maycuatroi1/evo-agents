"""The tier a change of the Curator gets on the hub besides what its kind and its paths give (``evo_agents.hub.tiers``):
what the project's knowledge graph knows of those paths, and what the pull request really does to knowledge.yaml.

``graph_view`` reads ``kg_impact`` of some paths in the project's latest graph through the project's hub sink: the files
their edits reach, and those it has no file for (``unmatched``). A proposal reads it as its review run's owner
(``evo_agents.hub.server.proposals``); the job curator.changes and a judge run's verdict read it as the owner of the
project's night shift (``impact_as_owner``), from a cache of graphs of their own under the hub's data directory
(``GraphReader``).

``unknown_paths`` says, of each path, why the graph cannot tell what editing it reaches, which ``tiers.tier_of`` makes
at least tier 1: a path it has no file for; one the latest successful measure of the graph's health
(``evo_agents.hub.server.kg_health``, its sources' ``off``) found missing from the graph, older in it than on the
default branch, or deleted from that branch, or a path of a source whose list of those the measure cut; and every path
while the graph of a project that has one cannot be read. A project without a graph on the hub gets none of these: its
tier stays what its kind and its paths give, as before the graph.

``pull_facts`` gathers, for a pull request, what the graph says of the paths it changes and, for a change of kind
``kg_source``, what it does to each knowledge.yaml at the root of its repo, read with the workers' App (contents: read)
at the pull request's merge base and at its head and compared key by key (``knowledge_diff.classify``); a file it cannot
read is kept as None, which the tier rules take as at least tier 1. ``retier`` gives the change's proposal its tier
again from the paths its diff touches with those facts: it only ever raises it, with the reasons, and a path the
charter protects still makes it tier 3. The pull request's knowledge.yaml facts stay on the change
(curator_changes.knowledge, schema 0057): a judge run of the change reads them (``judge_knowledge``), runs the checks
they name besides the plan's verify (``knowledge_diff.judge_checks``), and its verdict, whose paths give the tier again,
is judged against them; on GitLab, where the hub reads no pull request, a knowledge.yaml of a change of kind
``kg_source`` counts as not read.

``retier`` also gives the tier the change merges with: tier 0 when its kind is one of ``tiers.REPLAY_KINDS``, the
diff edits a harness alone (``replay.harness_only``) and its replays won at the head of its pull request
(``replay_verdict``, ``replay.verdict``), unless a rule made it tier 3 (``tiers.replay_tier``); the proposal keeps the
tier its rules gave. ``merge_tier`` is what the job takes a judged change at before it reads GitHub again: its
proposal's tier, or 0 when its replays won, with why it waits for its owner otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import knowledge_diff, replay, tables, tiers
from evo_agents.hub.kg_build import kg_root
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphCache, GraphUnavailable, Handles, answer
from evo_agents.hub.server.github import GitHubRefused
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

GRAPH_DEPTH = 2  # hops of kg_impact a change's paths are followed
CACHE = "tiers"  # the directory of the hub's kg data (``kg_root``) the job's graphs are fetched into
NIGHT_SHIFT = "night_shift"
UNREADABLE = "of the project could not be read now"


# What the graph says


@dataclass(frozen=True)
class GraphView:
    """What the project's knowledge graph says of some paths: those it has no file for, and the files their edits
    reach."""

    unmatched: frozenset[str]
    impacted: frozenset[tuple[str, str]]


def _view(result: dict) -> GraphView | None:
    data = result.get("structuredContent")
    if result.get("isError") or not isinstance(data, dict):
        return None
    reached = set()
    for item in data.get("impacted") or []:
        props = ((item or {}).get("node") or {}).get("props") or {}
        if isinstance(props.get("repo"), str) and isinstance(props.get("path"), str):
            reached.add((props["repo"], props["path"]))
    unmatched = {str(path) for path in data.get("unmatched") or []}
    return GraphView(frozenset(unmatched), frozenset(reached))


async def graph_view(request, owner: Principal, access: ProjectAccess, targets: list[str]) -> GraphView | None:
    """``kg_impact`` of ``targets`` (``repo:path`` each) in the project's latest graph, read as ``owner`` through the
    project's hub sink with no audience, as every member of the project reads what the review keeps; None when there
    is no target, or the project has no graph or it cannot be read now."""
    if not targets:
        return None
    from evo_agents.hub.server import kg  # its routes import the plans and runs modules

    sink = access.rules.hub_sink.sink if access.rules.hub_sink else kg.CLI_SINK
    arguments = {"paths": targets, "depth": GRAPH_DEPTH}
    try:
        result = await kg.tool_result(
            request.app.state, owner, access.name, "kg_impact", arguments, sink, audience=frozenset()
        )
    except HTTPException as exc:
        log.info("no knowledge graph for the review", extra={"project": access.name, "status": exc.status_code})
        return None
    return _view(result)


@dataclass(frozen=True)
class GraphReader:
    """Where a job reads graphs from: the hub's blob store and a cache of its own; nothing without a data directory."""

    blobs: object | None = None
    cache: GraphCache | None = None

    @classmethod
    def at(cls, blobs, data_dir: Path | None) -> GraphReader:
        return cls(blobs, GraphCache(kg_root(data_dir) / CACHE, keep=1) if data_dir is not None else None)


def _night_owner(project_id: int):
    s, p, u = tables.schedules, tables.projects, tables.users
    return (
        select(p.c.name.label("project"), s.c.owner_id, u.c.login.label("owner"))
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT)
        .order_by(s.c.id)
        .limit(1)
    )


async def impact_as_owner(
    engine: AsyncEngine, project_id: int, targets: list[str], reader: GraphReader
) -> GraphView | None:
    """``kg_impact`` of ``targets`` in the project's latest graph, read as the owner of its night shift through its hub
    sink from ``reader``; None without a target, a night shift, a graph or a cache, or when the graph cannot be read."""
    if not targets or reader.cache is None:
        return None
    from evo_agents.hub.server import kg
    from evo_agents.hub.server.kg_health import owner_access

    async with engine.begin() as conn:
        row = (await conn.execute(_night_owner(project_id))).one_or_none()
        access = await owner_access(conn, row.owner_id, row.owner, row.project) if row is not None else None
        graphs = await kg.built_graphs(conn, project_id, kg.GRAPHS_TRIED) if access is not None else []
    if not graphs:
        return None
    sink = access.rules.hub_sink.sink if access.rules.hub_sink else kg.CLI_SINK
    ceiling = access.rules.ceiling(access.max_level, sink)
    arguments = {"paths": targets, "depth": GRAPH_DEPTH}
    owner = (row.owner_id, row.project)
    try:
        result = await asyncio.to_thread(
            answer, reader.cache, reader.blobs, Handles(), owner, row.project, access.rules.policy, ceiling, sink,
            graphs, "kg_impact", arguments,
        )  # fmt: skip
    except (GraphUnavailable, ArtifactMismatch) as exc:
        log.info("the knowledge graph cannot be read for a tier", extra={"project": row.project, "why": str(exc)})
        return None
    return _view(result)


async def _has_graph(conn: AsyncConnection, project_id: int) -> bool:
    from evo_agents.hub.server.kg import built_graphs

    return bool(await built_graphs(conn, project_id, 1))


async def _measured_off(conn: AsyncConnection, project_id: int, paths: list[tuple[str, str]]) -> dict:
    """{(repo, path): why} of the paths the latest successful measure of the graph found off the default branch."""
    h = tables.kg_health
    row = (
        await conn.execute(
            select(h.c.measured_at, h.c.body)
            .where(h.c.project_id == project_id, h.c.error.is_(None))
            .order_by(h.c.measured_at.desc(), h.c.id.desc())
            .limit(1)
        )
    ).one_or_none()
    body = row.body if row is not None and isinstance(row.body, dict) else {}
    found: dict[tuple[str, str], str] = {}
    for entry in body.get("sources") or []:
        off = entry.get("off") if isinstance(entry, dict) else None
        repo = entry.get("repo") if isinstance(off, dict) else None
        if not isinstance(repo, str):
            continue
        branch = f"{entry.get('default_branch') or 'the default branch'} at {str(entry.get('head') or '')[:12]}"
        when = f"its measure of {row.measured_at.isoformat(timespec='minutes')}"
        listed = {key: set(off.get(key) or []) for key in ("missing", "stale", "deleted")}
        for named, path in paths:
            if named != repo or (named, path) in found:
                continue
            if path in listed.get("missing", ()):
                found[(named, path)] = f"lacks {repo}:{path}, which {branch} has ({when})"
            elif path in listed.get("stale", ()):
                found[(named, path)] = f"holds {repo}:{path} older than {branch} has it ({when})"
            elif path in listed.get("deleted", ()):
                found[(named, path)] = f"holds {repo}:{path}, which {branch} deleted ({when})"
            elif off.get("cut") is True:
                found[(named, path)] = f"holds more files of {repo} off {branch} than {when} lists"
    return found


async def unknown_paths(
    conn: AsyncConnection, project_id: int, paths: list[tuple[str, str]], graph: GraphView | None
) -> dict[tuple[str, str], str]:
    """Why the knowledge graph cannot tell what editing each of ``paths`` reaches, for ``tiers.tier_of``'s
    ``unknown`` (see the module's docstring); ``graph`` is what ``graph_view`` read of them."""
    if not paths:
        return {}
    if graph is None:
        return {key: UNREADABLE for key in paths} if await _has_graph(conn, project_id) else {}
    found = {(repo, path): f"has no file {repo}:{path}" for repo, path in paths if f"{repo}:{path}" in graph.unmatched}
    for key, why in (await _measured_off(conn, project_id, paths)).items():
        found.setdefault(key, why)
    return found


# What a pull request does to knowledge.yaml


@dataclass(frozen=True)
class PullFacts:
    """What the hub read of a change's diff: the paths it touches, the files the graph says they reach, why it cannot
    tell for some, and for a change of kind kg_source each knowledge.yaml it changes (``repo:path`` ->
    ``KnowledgeDiff.to_json()``, None when it could not be read; None for another kind)."""

    paths: tuple[str, ...] = ()
    impacted: tuple[tuple[str, str], ...] = ()
    unknown: dict = field(default_factory=dict)
    knowledge: dict | None = None


def named_paths(repo: str, paths: list[str]) -> list[tuple[str, str]]:
    """(repo, path) of each of ``paths`` a diff of ``repo`` touches, sorted and each once; one that is no repo path
    is named so, which the tier rules take as code."""
    named = set()
    for path in paths:
        normal = tiers.normalize_path(path)
        named.add((repo, normal if normal is not None else "(a path that is no repo path)"))
    return sorted(named)


async def _kind(conn: AsyncConnection, proposal_id: int) -> str:
    pr = tables.proposals
    return (await conn.execute(select(pr.c.kind).where(pr.c.id == proposal_id))).scalar_one()


async def _graph_facts(engine: AsyncEngine, change, named: list[tuple[str, str]], reader: GraphReader) -> tuple:
    graph = await impact_as_owner(engine, change.project_id, [f"{repo}:{path}" for repo, path in named], reader)
    async with engine.begin() as conn:
        unknown = await unknown_paths(conn, change.project_id, named, graph)
        kind = await _kind(conn, change.proposal_id)
    return tuple(sorted(graph.impacted)) if graph is not None else (), unknown, kind


async def read_knowledge(app, target, repo: str, named: list[tuple[str, str]], *, base: str | None, head: str) -> dict:
    """{``repo:path``: what the pull request does to it, or None when it cannot be read} of each knowledge.yaml at a
    root of ``named``, read at the merge base of ``base`` (the pull request's base commit) and ``head``."""
    from evo_agents.hub.server import pulls

    wanted = [path for name, path in named if name == repo and path in tiers.KNOWLEDGE_PATHS]
    if not wanted:
        return {}

    async def read(token: str) -> dict:
        merge_base = None
        if base is not None:
            merge_base = (await pulls.compare(app, token, target.owner, target.name, base, head)).get("merge_base")
        found = {}
        for path in wanted:
            if merge_base is None:
                found[f"{repo}:{path}"] = None
                continue
            before = await pulls.file_text(app, token, target.owner, target.name, path, merge_base)
            after = await pulls.file_text(app, token, target.owner, target.name, path, head)
            found[f"{repo}:{path}"] = knowledge_diff.classify(before, after).to_json()
        return found

    try:
        return await pulls.with_token(app, target.owner, target.name, "tree", read)
    except GitHubRefused as exc:
        log.info("knowledge.yaml of a pull request could not be read", extra={"repo": repo, "why": str(exc)})
        return dict.fromkeys((f"{repo}:{path}" for path in wanted), None)


async def pull_facts(
    engine: AsyncEngine, app, change, target, paths: list[str], pull: dict, reader: GraphReader
) -> PullFacts:
    """What the graph says of ``paths``, those pull request ``pull`` (as GitHub answers it) of ``change`` changes, and
    for a change of kind kg_source what it does to knowledge.yaml (see the module's docstring)."""
    named = named_paths(change.repo, paths)
    impacted, unknown, kind = await _graph_facts(engine, change, named, reader)
    knowledge = None
    if kind == tiers.KNOWLEDGE_KIND:
        base, head = (pull.get("base") or {}).get("sha"), (pull.get("head") or {}).get("sha") or change.head_sha
        knowledge = await read_knowledge(app, target, change.repo, named, base=base, head=head)
    return PullFacts(tuple(paths), impacted, unknown, knowledge)


async def verdict_facts(engine: AsyncEngine, change, paths: list[str], reader: GraphReader) -> PullFacts:
    """``pull_facts`` of the paths a judge run's diff touches: knowledge.yaml as the pull request showed it when it
    opened (the change's ``knowledge``), nothing of it on GitLab."""
    impacted, unknown, kind = await _graph_facts(engine, change, named_paths(change.repo, paths), reader)
    stored = change.knowledge if isinstance(change.knowledge, dict) else {}
    return PullFacts(tuple(paths), impacted, unknown, stored if kind == tiers.KNOWLEDGE_KIND else None)


def _knowledge_others(knowledge: dict | None) -> dict | None:
    """``tier_of``'s ``knowledge`` of ``PullFacts.knowledge``: what each file changes besides the sources' keys."""
    if knowledge is None:
        return None
    found = {}
    for key, value in knowledge.items():
        target = tiers.split_target(key)
        diff = knowledge_diff.KnowledgeDiff.from_json(value)
        if target is not None:
            found[target] = None if diff is None else list(diff.others)
    return found


async def retier(conn: AsyncConnection, change, facts: PullFacts, charter: dict) -> int:
    """The tier of the change's proposal again, from ``facts`` of the diff it really has (its paths first), with the
    charter's protected paths (``tiers.tier_of``): raised when they give a higher one, with the reasons, never
    lowered. The tier the change has now: that one, or 0 when its replays won (``_replayed``)."""
    pr = tables.proposals
    row = (
        await conn.execute(
            select(pr.c.kind, pr.c.tier, pr.c.tier_reasons).where(pr.c.id == change.proposal_id).with_for_update()
        )
    ).one()
    protected = [item for item in charter.get("protected_paths") or [] if isinstance(item, str)]
    try:
        found = tiers.tier_of(
            row.kind,
            named_paths(change.repo, list(facts.paths)),
            protected,
            list(facts.impacted),
            unknown=facts.unknown,
            knowledge=_knowledge_others(facts.knowledge) if row.kind == tiers.KNOWLEDGE_KIND else None,
        )
    except ValueError:
        found = tiers.Tier(3, [f"kind {row.kind} is not a kind of change: tier 3"])
    if found.tier <= row.tier:
        return await _replayed(conn, change, row.kind, max(row.tier, change.tier), facts.paths)
    kept = list(row.tier_reasons or [])
    for reason in found.reasons[1:]:
        if reason not in kept:
            kept.append(reason)
    kept.append(f"the files the change really touches give it tier {found.tier}, not {row.tier}")
    await conn.execute(update(pr).values(tier=found.tier, tier_reasons=kept).where(pr.c.id == change.proposal_id))
    log.info(
        "curator change raised",
        extra={"change_id": change.id, "proposal_id": change.proposal_id, "tier": found.tier, "was": row.tier},
    )
    return await _replayed(conn, change, row.kind, found.tier, facts.paths)


# Replays that won


async def replay_verdict(conn: AsyncConnection, change_id: int, head_sha: str | None) -> dict:
    """``replay.verdict`` of change ``change_id``'s replays at ``head_sha``, with ``replays``, how many it has in
    all."""
    rp, t = tables.curator_replays, tables.replay_tasks
    query = (
        select(t.c.split, rp.c.state, rp.c.result_a, rp.c.result_b)
        .join_from(rp, t, t.c.id == rp.c.task_id)
        .where(rp.c.change_id == change_id)
        .order_by(rp.c.id)
    )
    rows = [dict(row._mapping) for row in await conn.execute(query)]
    return {**replay.verdict(rows, head_sha), "replays": len(rows)}


async def _replayed(conn: AsyncConnection, change, kind: str, tier: int, paths) -> int:
    """The tier the change merges with, its rules giving it ``tier`` and its diff touching ``paths``
    (``tiers.replay_tier``)."""
    if tier in (0, 3) or kind not in tiers.REPLAY_KINDS:
        return tier
    harness = replay.harness_only(path for _, path in named_paths(change.repo, list(paths)))
    if harness is None:
        return tier
    found = await replay_verdict(conn, change.id, change.head_sha)
    won = found["reason"] if found["won"] else None
    final = tiers.replay_tier(kind, tiers.Tier(tier), harness=harness, won=won).tier
    if final != tier:
        log.info("curator change tier 0 by its replays", extra={"change_id": change.id, "was": tier})
    return final


async def merge_tier(conn: AsyncConnection, change) -> tuple[int, str]:
    """(the tier the job takes judged ``change`` at before it reads GitHub again, why it waits for its owner): its
    proposal's tier, or 0 when the change's replays won at its head, its files then read again (``retier``)."""
    pr = tables.proposals
    row = (await conn.execute(select(pr.c.kind, pr.c.tier).where(pr.c.id == change.proposal_id))).one()
    why = f"tier {row.tier} waits for its owner to merge it"
    if row.tier in (0, 3) or row.kind not in tiers.REPLAY_KINDS:
        return row.tier, why
    found = await replay_verdict(conn, change.id, change.head_sha)
    if found["won"]:
        return 0, ""
    return row.tier, f"{why}; its replays did not win: {found['reason']}" if found["replays"] else why


# The Judge's checks of knowledge.yaml


def knowledge_sources(knowledge) -> list[str]:
    """The sources a change's knowledge.yaml facts say it edits, in their order, each once."""
    sources: list[str] = []
    for value in (knowledge or {}).values() if isinstance(knowledge, dict) else ():
        diff = knowledge_diff.KnowledgeDiff.from_json(value)
        sources += [source for source in diff.sources if source not in sources] if diff is not None else []
    return sources


def judge_checks(change) -> list[str]:
    """The checks a judge run of ``change`` runs besides the plan's verify: none unless its pull request changed a
    knowledge.yaml under kind kg_source (its ``knowledge``)."""
    if not isinstance(change.knowledge, dict) or not change.knowledge:
        return []
    return knowledge_diff.judge_checks(knowledge_sources(change.knowledge))


def judge_knowledge(change) -> dict | None:
    """What a judge run of ``change`` reads of knowledge.yaml (``JudgeInputs.knowledge``): the files, the sources to
    sync on trial and the checks' names; None for a change whose pull request changed none under kind kg_source."""
    checks = judge_checks(change)
    if not checks:
        return None
    files = [key.partition(":")[2] for key in change.knowledge]
    return {"files": sorted(set(files)), "sources": knowledge_sources(change.knowledge), "checks": checks}
