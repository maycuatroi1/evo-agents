"""The tokens the hub gives the backend of an isolated plugin, and the payload the plugin host starts it with.

A plugin token is ``evp_`` and 32 random bytes in base64url, like the hub's other tokens; Postgres keeps only its
SHA-256 (``plugin_tokens``, schema 0036). The hub mints one when the plugin host is about to start a backend, and only
then (``issue_token``, through ``POST /internal/plugin-host/plugins/{name}/token``): minting revokes the plugin's
older tokens, so a token lives as long as the backend it was made for, and a host that pulls the same plugins again
gets the same payload and restarts nothing. Removing a plugin and the hub turning it off revoke its tokens at once
(``repository.tokens.revoke_tokens``, from ``service.installing`` and ``service.startup``), before its backend has stopped; minting locks the plugin's row as they do,
and ``plugin_of_token`` takes no token of a plugin that is not installed or is turned off. A plugin token is not a user
token: it never signs in, and it reaches the hub only on the routes of the plugin API under ``/v1/plugin-api/`` (a
plugin hands tasks to its agent types there, ``evo_agents.hub.server.task_dispatch``), which check with
``plugin_of_token`` on each request. What it may do is what the plugin's installed version asks for
and the projects that enabled it granted (``repository.grants.project_grants``), and nothing else.

``backend_payload`` is what the host gives the backend in ``initialize`` (``evo_agents.plugins.protocol``), the token
left for the host to add: the plugin's id and version, the hub's URL, the effective config of the hub's own layer
(``instance``) and of each project that enabled it (``projects``), with the secret fields decrypted here, where the key
is, and never anywhere near another plugin, and what each project granted (``grants``). A hub without
EVO_HUB_SECRETS_KEY leaves a secret field out and says so in ``missing_secrets``, rather than handing the backend a
value it cannot open.

Only the plugin host reads these, over the shared secret (``routes.host``); a member never does.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import asked_for
from evo_agents.hub.server.plugins.models.configs import Place
from evo_agents.hub.server.plugins.models.tokens import PLUGIN_PREFIX, TOKEN_BYTES
from evo_agents.hub.server.plugins.repository import configs as config_rows
from evo_agents.hub.server.plugins.repository import grants as grant_rows
from evo_agents.hub.server.plugins.repository import tokens as repository
from evo_agents.hub.server.plugins.service.configs import stored_stacks, unseal
from evo_agents.hub.server.sealing import Sealer, Unsealable
from evo_agents.hub.server.security import b64, hash_token
from evo_agents.plugins import config as plugin_config

log = logging.getLogger(__name__)


def new_plugin_token() -> str:
    import secrets

    return PLUGIN_PREFIX + b64(secrets.token_bytes(TOKEN_BYTES))


async def issue_token(conn: AsyncConnection, plugin_id: int) -> str:
    """A fresh token for the backend of plugin ``plugin_id``, the plugin's older tokens revoked; the plaintext, which
    the hub never stores and never shows again."""
    await repository.revoke_tokens(conn, plugin_id)
    token = new_plugin_token()
    await repository.add_token(conn, plugin_id, hash_token(token))
    return token


async def plugin_of_token(conn: AsyncConnection, token: str) -> int | None:
    """The plugin id a live plugin token belongs to, or None: the token not revoked, and its plugin installed and not
    turned off, so a token that outlived its plugin's removal still opens nothing. A malformed value never reaches a
    comparison here."""
    if not token.startswith(PLUGIN_PREFIX):
        return None
    return await repository.plugin_of_hash(conn, hash_token(token))


async def backend_payload(conn: AsyncConnection, sealer: Sealer | None, hub_url: str, plugin_row) -> dict:
    """The ``initialize`` payload for the backend of ``plugin_row`` (a row of ``plugin_grants.enabled_query`` form or
    of the store): its config by layer with secrets decrypted and the grants of each project. The host adds the
    token it gets at the backend's start under ``hub``."""
    manifest = plugin_row.manifest
    schema = manifest.get("config")
    instance, instance_missing = await layer_config(conn, sealer, plugin_row.id, plugin_row.name, schema, None)
    grants = await grant_rows.project_grants(conn, plugin_row.name)
    projects: dict[str, dict] = {}
    missing: dict[str, list[str]] = {"instance": instance_missing} if instance_missing else {}
    for row in await grant_rows.enabled_rows_of(conn, plugin_row.name):
        cfg, lost = await layer_config(conn, sealer, row.id, row.name, schema, row.project_id, row.project)
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


async def layer_config(conn, sealer, plugin_id, name, schema, project_id, project=None):
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
    rows = await config_rows.secret_rows(conn, plugin_id, project_id)
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
