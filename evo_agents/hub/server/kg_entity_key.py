"""The key of the entities step of a build on the hub (plan kg-zip-to-answers, step 2): a strategy whose ``entities``
mapping reaches an OpenAI-compatible ``url`` (such as OpenRouter) through an llm-backend sink names the variable of its
key in ``key_env``; the hub's build (``evo_agents.hub.kg_build.run_build``) opens it here from a member's secret and
hands it to the step in memory (``evo_agents.kg.entities.run``, ``keys``), never through the environment of the worker.

The secret is a live one, not expired, sealed, of a member (never an agent profile's), and either bound to the project
for any worker (a binding with no worker and no pool), or owned by the member who created the project
(``projects.created_by``), so a project made from an upload uses its creator's key without a step by hand. Two kinds
answer: an env secret whose ``env_var`` is ``key_env``, and a provider account whose base URL is on the host of the
mapping's url. A bound secret comes before the creator's own, the env kind before the provider kind, the newest first.

A key goes only where it belongs, since a writer of the project's settings names both the url and ``key_env``: the url
is https, an env secret answers only when its variable is one of ``KEY_HOSTS`` and the url is on that variable's host
(OPENROUTER_API_KEY to openrouter.ai, never GITHUB_TOKEN anywhere), and a provider account only on its base URL's host.

Nothing here logs, returns into coverage or writes the value: the build records ``secret:<id>`` alone, as the audit
names a secret. Without EVO_HUB_SECRETS_KEY (no sealer) or without a secret that answers, the step stops before any
call and says why in its coverage (``evo_agents.kg.entities``).
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, secret_aad

ENV_KIND = "env"

# The variables of an env secret the build hands to an entities url, each with the hosts its key belongs to.
KEY_HOSTS = {
    "OPENROUTER_API_KEY": frozenset({"openrouter.ai"}),
    "OPENAI_API_KEY": frozenset({"api.openai.com"}),
}


def wants_key(mapping) -> str | None:
    """The variable a mapping's url model reads its key from, when the build has to find it; else None."""
    if not isinstance(mapping, dict) or "url" not in mapping or not mapping.get("key_env"):
        return None
    return mapping["key_env"]


async def find(
    conn: AsyncConnection, sealer: Sealer | None, project_id: int, mapping
) -> tuple[dict[str, str], str | None]:
    """``({key_env: key}, "secret:<id>")`` of the secret that answers for ``mapping`` in project ``project_id``;
    ``({}, None)`` when none does or the hub has no sealer."""
    name = wants_key(mapping)
    if name is None or sealer is None:
        return {}, None
    s, b, p, pa = tables.secrets, tables.secret_bindings, tables.projects, tables.provider_accounts
    url = urlparse(mapping["url"]) if isinstance(mapping["url"], str) else None
    if url is None or url.scheme != "https" or not url.hostname:
        return {}, None
    host = url.hostname.lower()
    env_answers = (s.c.kind == ENV_KIND) & (s.c.env_var == name) if host in KEY_HOSTS.get(name, ()) else false()
    bound = exists().where(
        b.c.secret_id == s.c.id, b.c.project_id == project_id, b.c.worker_id.is_(None), b.c.pool_id.is_(None)
    )
    creator = select(p.c.created_by).where(p.c.id == project_id).scalar_subquery()
    rows = await conn.execute(
        select(
            s.c.id,
            s.c.owner_id,
            s.c.name,
            s.c.kind,
            s.c.sealed,
            s.c.nonce,
            s.c.key_id,
            pa.c.base_url,
            bound.label("bound"),
        )
        .join_from(s, pa, pa.c.secret_id == s.c.id, isouter=True)
        .where(
            s.c.owner_id.is_not(None),
            s.c.deleted_at.is_(None),
            s.c.sealed.is_not(None),
            or_(s.c.expires_at.is_(None), s.c.expires_at > func.now()),
            or_(bound, s.c.owner_id == creator),
            or_(
                env_answers,
                (s.c.kind == PROVIDER_KIND) & pa.c.base_url.is_not(None),
            ),
        )
        .order_by(bound.desc(), (s.c.kind == ENV_KIND).desc(), s.c.updated_at.desc(), s.c.id.desc())
    )
    for row in rows:
        if row.kind == PROVIDER_KIND and (urlparse(row.base_url).hostname or "").lower() != host:
            continue
        try:
            value = sealer.open(Sealed(row.sealed, row.nonce, row.key_id), secret_aad(row.owner_id, row.name, row.kind))
        except Unsealable:
            continue
        if value.strip():
            return {name: value.strip()}, f"secret:{row.id}"
    return {}, None
