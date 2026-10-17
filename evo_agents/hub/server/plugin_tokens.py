"""The tokens the hub gives the backend of an isolated plugin, and the payload the plugin host starts it with.

A plugin token is ``evp_`` and 32 random bytes in base64url, like the hub's other tokens; Postgres keeps only its
SHA-256 (``plugin_tokens``, schema 0035). The hub mints one when the plugin host is about to start a backend, and only
then (``issue_token``, through ``POST /internal/plugin-host/plugins/{name}/token``): minting revokes the plugin's
older tokens, so a token lives as long as the backend it was made for, and a host that pulls the same plugins again
gets the same payload and restarts nothing. Removing a plugin and the hub turning it off revoke its tokens at once
(``revoke_tokens``, from ``plugin_store``), before its backend has stopped; minting locks the plugin's row as they do,
and ``plugin_of_token`` takes no token of a plugin that is not installed or is turned off. A plugin token is not a user
token: it never signs in, and it reaches the hub only on the routes the extension points of a plugin use (step 5),
which check with ``plugin_of_token`` on each request. What it may do is what the plugin's installed version asks for
and the projects that enabled it granted (``plugin_grants.project_grants``), and nothing else.

``backend_payload`` is what the host gives the backend in ``initialize`` (``evo_agents.plugins.protocol``), the token
left for the host to add: the plugin's id and version, the hub's URL, the effective config of the hub's own layer
(``instance``) and of each project that enabled it (``projects``), with the secret fields decrypted here, where the key
is, and never anywhere near another plugin, and what each project granted (``grants``). A hub without
EVO_HUB_SECRETS_KEY leaves a secret field out and says so in ``missing_secrets``, rather than handing the backend a
value it cannot open.

Only the plugin host reads these, over the shared secret (``evo_agents.hub.server.plugin_host``); a member never does.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plugin_configs import Place, stored_stacks, unseal
from evo_agents.hub.server.plugin_grants import asked_for, enabled_query, project_grants
from evo_agents.hub.server.sealing import Sealer, Unsealable
from evo_agents.hub.server.security import b64, hash_token
from evo_agents.plugins import config as plugin_config

log = logging.getLogger(__name__)

PLUGIN_PREFIX = "evp_"
TOKEN_BYTES = 32


def new_plugin_token() -> str:
    import secrets

    return PLUGIN_PREFIX + b64(secrets.token_bytes(TOKEN_BYTES))


async def revoke_tokens(conn: AsyncConnection, plugin_id: int) -> None:
    """Revoke every live token of plugin ``plugin_id``: when it is removed or turned off, and before a new one."""
    pt = tables.plugin_tokens
    await conn.execute(
        update(pt).where(pt.c.plugin_id == plugin_id, pt.c.revoked_at.is_(None)).values(revoked_at=func.now())
    )


async def issue_token(conn: AsyncConnection, plugin_id: int) -> str:
    """A fresh token for the backend of plugin ``plugin_id``, the plugin's older tokens revoked; the plaintext, which
    the hub never stores and never shows again."""
    pt = tables.plugin_tokens
    await revoke_tokens(conn, plugin_id)
    token = new_plugin_token()
    await conn.execute(insert(pt).values(plugin_id=plugin_id, token_hash=hash_token(token)))
    return token


async def plugin_of_token(conn: AsyncConnection, token: str) -> int | None:
    """The plugin id a live plugin token belongs to, or None: the token not revoked, and its plugin installed and not
    turned off, so a token that outlived its plugin's removal still opens nothing. A malformed value never reaches a
    comparison here."""
    if not token.startswith(PLUGIN_PREFIX):
        return None
    pt, p = tables.plugin_tokens, tables.plugins
    query = (
        select(pt.c.plugin_id)
        .join(p, p.c.id == pt.c.plugin_id)
        .where(
            pt.c.token_hash == hash_token(token),
            pt.c.revoked_at.is_(None),
            p.c.installed_version.is_not(None),
            p.c.disabled_reason.is_(None),
        )
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def backend_payload(conn: AsyncConnection, sealer: Sealer | None, hub_url: str, plugin_row) -> dict:
    """The ``initialize`` payload for the backend of ``plugin_row`` (a row of ``plugin_grants.enabled_query`` form or
    of plugin_store): its config by layer with secrets decrypted and the grants of each project. The host adds the
    token it gets at the backend's start under ``hub``."""
    manifest = plugin_row.manifest
    schema = manifest.get("config")
    instance, instance_missing = await _layer_config(conn, sealer, plugin_row.id, plugin_row.name, schema, None)
    grants = await project_grants(conn, plugin_row.name)
    projects: dict[str, dict] = {}
    missing: dict[str, list[str]] = {"instance": instance_missing} if instance_missing else {}
    for row in await _enabled_rows(conn, plugin_row.name):
        cfg, lost = await _layer_config(conn, sealer, row.id, row.name, schema, row.project_id, row.project)
        projects[row.project] = cfg
        if lost:
            missing[row.project] = lost
    return {
        "protocol": "plugin/1",
        "plugin": {"id": plugin_row.name, "version": _version(plugin_row)},
        "plugin_id": plugin_row.id,
        "hub": {"url": hub_url, "version": manifest["version"]},
        "config": {"instance": instance, "projects": projects},
        "grants": {project: list(permissions) for project, permissions in grants.items()},
        "permissions": asked_for(manifest),
        "missing_secrets": missing,
    }


def _version(plugin_row) -> str:
    return getattr(plugin_row, "installed_version", None) or plugin_row.version


async def _enabled_rows(conn: AsyncConnection, name: str) -> list:
    query = enabled_query().where(tables.plugins.c.name == name).order_by(tables.projects.c.name)
    return list(await conn.execute(query))


async def _layer_config(conn, sealer, plugin_id, name, schema, project_id, project=None):
    """The effective config of one plugin for one project (or the instance layer when ``project_id`` is None), its
    secrets decrypted; the fields the hub cannot open for want of the key."""
    stacks = await stored_stacks(conn, [plugin_id], project_id or 0, None)
    layers = stacks.get(plugin_id, [])
    merged = plugin_config.merge(schema, layers)
    plain = {field: item.value for field, item in merged.fields.items() if not item.secret}
    secrets_values, missing = await _decrypt_secrets(conn, sealer, plugin_id, name, project_id, project)
    return {**plain, **secrets_values}, missing


async def _decrypt_secrets(conn, sealer, plugin_id, name, project_id, project):
    """The secret fields of a plugin's instance and project layers, decrypted; the names left out for want of the
    key. The highest layer that sets a field wins, as the effective config has it."""
    c = tables.plugin_configs.c
    scope = c.layer == "instance"
    if project_id:
        scope = scope | ((c.layer == "project") & (c.project_id == project_id))
    rows = await conn.execute(
        select(c.plugin_id, c.layer, c.version, c.secrets)
        .where(c.plugin_id == plugin_id, scope)
        .ext(_distinct_current())
        .order_by(c.plugin_id, c.layer, c.version.desc())
    )
    by_layer = {row.layer: row for row in rows}
    values, missing = {}, []
    for layer in plugin_config.STORED_LAYERS:  # defaults, instance, project: a later layer wins
        row = by_layer.get(layer)
        if row is None:
            continue
        place = Place(plugin_id, name, layer, project_id if layer != "instance" else None, project)
        for field, sealed in (row.secrets or {}).items():
            if sealer is None:
                missing.append(field)
                continue
            try:
                values[field] = unseal(sealer, place, field, sealed)
            except Unsealable:
                missing.append(field)
    return values, sorted(set(missing))


def _distinct_current():
    from sqlalchemy.dialects.postgresql import distinct_on

    c = tables.plugin_configs.c
    return distinct_on(c.plugin_id, c.layer)
