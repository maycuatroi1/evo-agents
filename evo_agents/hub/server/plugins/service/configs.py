"""The config of a plugin as the hub keeps it (table plugin_configs, schema 0035, and ``evo_agents.plugins.config``):
versions of each stored layer, the effective config of a project, and what each run ran with.

Three layers are stored: the hub's own (instance, which hub admins write), a project's and an agent profile's of that
project (agent), which admins of the project write and its members read. Below them are the defaults of the config
schema of the installed version; above them a run's own layer (``runs.plugin_config``), given when it is dispatched,
plain values only. A field takes its value from the highest layer that sets it.

A write names the fields it sets and those it unsets; the others stay as the layer's current version has them, its
secrets included, so a secret is given once and never sent back. Each write that changes something makes the layer's
next version, never changed after; a rollback makes the next version a copy of an older one, which names it
(``rolled_back_to``). Values are checked against the schema of the installed version (``plugin_config.check_values``):
each problem names its field, and none ever holds a secret's value.

A secret field is sealed with the hub's key (``evo_agents.hub.server.sealing``), bound to its plugin, layer, project,
agent profile and field, and kept as ciphertext, nonce and key_id in base64; the hub never shows or logs it, and
without EVO_HUB_SECRETS_KEY a write that sets one is a 503. The digest of an effective config counts a secret by a
fingerprint of its sealed value (``fingerprint``), so it changes with the secret and tells nothing of it.

When a worker claims a run, ``record_run_plugins`` writes, for each plugin enabled on the run's project that is
installed and turned on, the installed version and the digest of the effective config of the run (its project, its
agent profile and its own layer) into run_plugins.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import enabled_plugins
from evo_agents.hub.server import audit
from evo_agents.hub.server.plugins.models.configs import (
    NO_KEY,
    ConfigField,
    EffectiveConfig,
    Place,
    PluginConfigLayer,
    PluginConfigRollback,
    PluginConfigWrite,
    RunPlugin,
)
from evo_agents.hub.server.plugins.repository import configs as repository
from evo_agents.hub.server.sealing import Sealed, Sealer
from evo_agents.hub.server.security import Principal
from evo_agents.plugins import config as plugin_config
from evo_agents.plugins.config import Layer

log = logging.getLogger(__name__)


# Sealed values


def seal(sealer: Sealer, place: Place, name: str, value: str) -> dict:
    sealed = sealer.seal(value, place.aad(name))
    return {
        "ciphertext": base64.b64encode(sealed.ciphertext).decode(),
        "nonce": base64.b64encode(sealed.nonce).decode(),
        "key_id": sealed.key_id,
    }


def unseal(sealer: Sealer, place: Place, name: str, stored: dict) -> str:
    """The value of secret field ``name`` as ``stored``; ``sealing.Unsealable`` when it does not open here."""
    sealed = Sealed(base64.b64decode(stored["ciphertext"]), base64.b64decode(stored["nonce"]), stored["key_id"])
    return sealer.open(sealed, place.aad(name))


def fingerprint(stored: dict) -> str:
    """A fingerprint of a sealed value, which the digest counts in place of the secret: it changes with every value
    sealed, and a rollback that copies it keeps it."""
    try:
        raw = base64.b64decode(stored["nonce"]) + base64.b64decode(stored["ciphertext"])
    except (KeyError, TypeError, binascii.Error):
        raw = json.dumps(stored, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def as_layer(name: str, values: dict, secrets: dict, version: int | None) -> Layer:
    prints = {field: fingerprint(stored) for field, stored in secrets.items()}
    return Layer(name, values, prints, version)


# Reading a layer


def layer_model(place: Place, row) -> PluginConfigLayer:
    common = {"plugin": place.plugin, "layer": place.layer, "project": place.project, "agent": place.handle}
    if row is None:
        empty = {"values": {}, "secrets": [], "rolled_back_to": None, "changed_by": None, "changed_at": None}
        return PluginConfigLayer(**common, version=0, **empty)
    return PluginConfigLayer(
        **common,
        version=row.version,
        values=row.plain,
        secrets=sorted(row.secrets),
        rolled_back_to=row.rolled_back_to,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
    )


async def read_layer(conn: AsyncConnection, place: Place) -> PluginConfigLayer:
    return layer_model(place, await repository.current(conn, place))


async def layer_versions(conn: AsyncConnection, place: Place) -> list[PluginConfigLayer]:
    """Every version of the layer, the newest first."""
    return [layer_model(place, row) for row in await repository.versions(conn, place)]


# Writing a layer


def _check_version(place: Place, now: int, if_version: int | None) -> None:
    if if_version is not None and if_version != now:
        raise HTTPException(
            409,
            f"the {place.layer} layer of plugin {place.plugin} is at version {now}, not {if_version}; nothing changed",
        )


def _changes(schema: dict | None, held, body: PluginConfigWrite) -> list[str]:
    """What is wrong with ``body`` as the next version of ``held`` (the current row, or None): each problem."""
    problems = []
    both = sorted(set(body.values) & set(body.unset))
    if both:
        problems.append(f"{', '.join(both)}: both set and unset")
    if len(set(body.unset)) != len(body.unset):
        problems.append("unset names a field more than once")
    set_now = set() if held is None else set(held.plain) | set(held.secrets)
    for name in sorted(set(body.unset) - set_now - set(both)):
        problems.append(f"{name}: the layer does not set it, so there is nothing to unset")
    problems.extend(plugin_config.check_values(schema, body.values))
    return problems


def _next(schema: dict | None, held, body: PluginConfigWrite, sealer: Sealer | None, place: Place):
    """The values and sealed secrets of the next version, and the names of the fields that change."""
    secret = plugin_config.fields(schema)
    values = dict(held.plain) if held is not None else {}
    secrets = dict(held.secrets) if held is not None else {}
    changed = []
    for name, value in body.values.items():
        if secret.get(name):
            secrets[name] = seal(sealer, place, name, value)  # pyright: ignore[reportArgumentType]
            changed.append(name)
        elif name not in values or values[name] != value:
            values[name] = value
            changed.append(name)
    for name in body.unset:
        values.pop(name, None)
        secrets.pop(name, None)
        changed.append(name)
    if len(json.dumps(values, ensure_ascii=False).encode()) > plugin_config.MAX_LAYER_BYTES:
        raise HTTPException(
            422,
            f"the values of one layer are at most {plugin_config.MAX_LAYER_BYTES // 1024} KiB as JSON; nothing changed",
        )
    return values, secrets, sorted(changed)


async def _audit(conn: AsyncConnection, user: Principal, place: Place, target: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.PLUGIN_CONFIG,
        target=target,
        project_id=place.project_id,
    )


async def write_layer(
    conn: AsyncConnection, user: Principal, place: Place, schema: dict | None, body: PluginConfigWrite, sealer
) -> PluginConfigLayer:
    """Write ``body`` as the next version of the layer at ``place``, checked against ``schema``; the layer as it is
    now. The caller holds a lock that serialises the writes of the layer."""
    held = await repository.current(conn, place)
    now = 0 if held is None else held.version
    _check_version(place, now, body.if_version)
    problems = _changes(schema, held, body)
    if problems:
        raise HTTPException(422, f"{'; '.join(problems)}; nothing changed")
    secret = plugin_config.fields(schema)
    if sealer is None and any(secret.get(name) for name in body.values):
        raise HTTPException(503, NO_KEY)
    values, secrets, changed = _next(schema, held, body, sealer, place)
    if not changed:
        return layer_model(place, held)
    await repository.insert_version(conn, user.user_id, place, now + 1, values, secrets)
    await _audit(conn, user, place, f"{place.target(now + 1)} fields={','.join(changed)}")
    log.info(
        "plugin config changed",
        extra={"plugin": place.plugin, "layer": place.layer, "project": place.project, "version": now + 1},
    )
    return layer_model(place, await repository.current(conn, place))


async def rollback_layer(
    conn: AsyncConnection, user: Principal, place: Place, body: PluginConfigRollback
) -> PluginConfigLayer:
    """Copy version ``body.version`` of the layer at ``place`` as its next version; the layer as it is now."""
    held = await repository.current(conn, place)
    now = 0 if held is None else held.version
    _check_version(place, now, body.if_version)
    if body.version >= now:
        raise HTTPException(
            409, f"the {place.layer} layer of plugin {place.plugin} is at version {now}: roll back to an older one"
        )
    older = await repository.version_of(conn, place, body.version)
    if older is None:
        raise HTTPException(404, f"the {place.layer} layer of plugin {place.plugin} has no version {body.version}")
    await repository.insert_version(conn, user.user_id, place, now + 1, older.plain, older.secrets, back=body.version)
    await _audit(conn, user, place, f"{place.target(now + 1)} rollback-to=v{body.version}")
    log.info(
        "plugin config rolled back",
        extra={"plugin": place.plugin, "layer": place.layer, "project": place.project, "version": body.version},
    )
    return layer_model(place, await repository.current(conn, place))


# The effective config


async def stored_stacks(
    conn: AsyncConnection, plugin_ids: list[int], project_id: int, profile_id: int | None
) -> dict[int, list[Layer]]:
    """plugin id -> its stored layers a run of the project under ``profile_id`` gets, lowest first."""
    found: dict[int, dict[str, Layer]] = {}
    if plugin_ids:
        for row in await repository.stack_rows(conn, plugin_ids, project_id, profile_id):
            found.setdefault(row.plugin_id, {})[row.layer] = as_layer(row.layer, row.plain, row.secrets, row.version)
    order = plugin_config.STORED_LAYERS
    return {plugin_id: [layers[name] for name in order if name in layers] for plugin_id, layers in found.items()}


def effective_model(plugin, project: str, agent: str, merged: plugin_config.Effective) -> EffectiveConfig:
    fields = [
        ConfigField(name=name, value=item.value, secret=item.secret, layer=item.layer, version=item.version)
        for name, item in sorted(merged.fields.items())
    ]
    return EffectiveConfig(
        plugin=plugin.name,
        version=plugin.installed_version,
        project=project,
        agent=agent,
        fields=fields,
        problems=list(merged.problems),
        digest=merged.digest,
    )


async def effective(
    conn: AsyncConnection, plugin, project_id: int, project: str, profile_id: int, handle: str
) -> EffectiveConfig:
    """The effective config of ``plugin`` (a row of ``plugin_grants.enabled_query``) in the project, for runs under
    agent profile ``profile_id`` that give no layer of their own."""
    stacks = await stored_stacks(conn, [plugin.id], project_id, profile_id)
    merged = plugin_config.merge(plugin.manifest.get("config"), stacks.get(plugin.id, []))
    return effective_model(plugin, project, handle, merged)


# Runs


async def check_run_layer(conn: AsyncConnection, project_id: int, project: str, given: dict | None) -> dict | None:
    """A run's own layer, ``given`` at its dispatch (plugin name -> the values it sets), once each plugin is enabled
    on the project and its values fit its schema and hold no secret; None for none. 422 otherwise."""
    if not given:
        return None
    enabled = {row.name: row for row in await enabled_plugins(conn, project_id)}
    problems = []
    for name, values in given.items():
        if name not in enabled:
            problems.append(f"{name}: not a plugin enabled on project {project}")
            continue
        schema = enabled[name].manifest.get("config")
        problems.extend(f"{name}: {problem}" for problem in plugin_config.check_values(schema, values, secrets=False))
    if problems:
        raise HTTPException(422, f"plugin_config: {'; '.join(problems)}; nothing was dispatched")
    return {name: values for name, values in given.items() if values}


async def record_run_plugins(conn: AsyncConnection, run_id: int) -> int:
    """Write, for each plugin enabled on the project of run ``run_id``, installed and turned on, its version and the
    digest of the run's effective config; how many."""
    run = await repository.run_row(conn, run_id)
    enabled = await enabled_plugins(conn, run.project_id)
    if not enabled:
        return 0
    stacks = await stored_stacks(conn, [plugin.id for plugin in enabled], run.project_id, run.profile_id)
    own = run.plugin_config or {}
    for plugin in enabled:
        layers = stacks.get(plugin.id, [])
        if own.get(plugin.name):
            layers = [*layers, Layer("run", own[plugin.name])]
        merged = plugin_config.merge(plugin.manifest.get("config"), layers)
        await repository.put_run_plugin(conn, run_id, plugin.id, plugin.installed_version, merged.digest)
    return len(enabled)


async def run_plugins(conn: AsyncConnection, run_id: int) -> list[RunPlugin]:
    return [RunPlugin(**row._mapping) for row in await repository.run_plugin_rows(conn, run_id)]


def place_of(plugin, layer: str, **where) -> Place:
    return replace(Place(plugin.id, plugin.name, layer), **where)
