"""The hub's side of the core plugin guard (``evo_agents.core_plugins.guard``, ``docs/guard.md``): the policy every
worker and member gets, made of the private names and the published repos of the projects that enable the plugin, and
the scrub of the text the hub itself sends to a guarded repo.

- ``policy`` reads both lists of every project that enables the plugin, and hashes the terms
  (``evo_agents.private_names.entries``). A policy with no term or no target guards nothing; its ``digest`` is then
  empty, which a worker reads as "no guard".
- ``cached_policy`` is ``policy`` through the hub's cache (``evo_agents.hub.cache``), for the heartbeat of every worker
  and GET /v1/guard/policy, which read it most: it is kept POLICY_TTL seconds and dropped when a transaction that
  wrote a term, a target, a plugin or an enablement commits (``cache.watch``).
- ``scrub`` replaces each private name in the title or the body of a pull request the hub opens on a guarded repo (a
  chat or task run's draft, a Curator change, a release) with ``[private]``: the hub's own text never stops a run, it
  only loses the name, and the log says how many it took out.

Outside ``evo_agents.hub.server``: the heartbeat (``runs.service.reports``), the pull requests the hub opens and the
plugin's routes share it, and none of them may import the plugin (contract core-plugins-by-entry-point).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents import private_names
from evo_agents.hub import cache, tables
from evo_agents.hub.plugin_calls import enabled_query
from evo_agents.private_names import Policy

log = logging.getLogger(__name__)

PLUGIN = "guard"
MASK = "[private]"
POLICY_KEY = "guard:policy"
POLICY_TTL = 300.0  # seconds the cache keeps the policy; a write of what it is read from drops it before

cache.watch(
    "guard", tables.guard_terms, tables.guard_targets, tables.plugins, tables.plugin_versions, tables.project_plugins
)


def _projects():
    """The ids of the projects that enable the plugin while it is installed and on."""
    on = enabled_query().where(tables.plugins.c.name == PLUGIN).subquery()
    return select(on.c.project_id)


async def policy(conn: AsyncConnection) -> Policy:
    """The policy of the hub now: every term of the projects that enable the plugin, hashed, and their targets."""
    t, g = tables.guard_terms, tables.guard_targets
    terms = (await conn.execute(select(t.c.term).where(t.c.project_id.in_(_projects())))).scalars()
    targets = (await conn.execute(select(g.c.pattern).where(g.c.project_id.in_(_projects())))).scalars()
    entries = {e["h"]: e for term in terms for e in private_names.entries(term)}
    return Policy(terms=list(entries.values()), targets=list(targets))


async def cached_policy(conn: AsyncConnection, store: cache.Cache | None) -> Policy:
    """``policy``, through ``store`` when there is one."""
    if store is None:
        return await policy(conn)

    async def load() -> dict:
        found = await policy(conn)
        return {"terms": found.terms, "targets": found.targets}

    kept = await store.fetch(cache.HUB, POLICY_KEY, POLICY_TTL, load)
    return Policy(terms=kept["terms"], targets=kept["targets"])


def digest_of(found: Policy) -> str:
    """The digest a worker compares with the policy it holds; empty for a policy that guards nothing."""
    return found.digest() if found.terms and found.targets else ""


def scrub(found: Policy | None, repo: str, *texts: str) -> list[str]:
    """``texts`` with each private name replaced by MASK when ``repo`` (a URL or ``host/owner/repo``) is guarded;
    as they are otherwise."""
    if found is None or not found.terms or not found.guards(repo):
        return list(texts)
    matcher, out, taken = found.matcher(), [], 0
    for text in texts:
        hits = matcher.find(text, limit=1000)
        if hits:
            text = private_names.normalized(text)
            for start, end in reversed(private_names.spans(hits)):
                text = text[:start] + MASK + text[end:]
            taken += len(hits)
        out.append(text)
    if taken:
        log.warning("private names taken out of text for a guarded repo", extra={"repo": repo, "names": taken})
    return out
