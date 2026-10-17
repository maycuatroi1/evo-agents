"""The config of a plugin as the hub keeps it (table plugin_configs, schema 0034, and ``evo_agents.plugins.config``):
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
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.plugin_grants import enabled_plugins
from evo_agents.hub.server.sealing import Sealed, Sealer, plugin_config_aad
from evo_agents.hub.server.security import Principal
from evo_agents.plugins import config as plugin_config
from evo_agents.plugins.config import Layer

log = logging.getLogger(__name__)

MAX_FIELDS = 200  # fields one write sets or unsets
NO_KEY = (
    "this hub keeps no secrets: it needs EVO_HUB_SECRETS_KEY, the key that seals them; ask its operator to set it "
    "(docs/credentials.md); nothing changed"
)


class PluginConfigLayer(BaseModel):
    """One stored layer of a plugin's config at one version."""

    plugin: str
    layer: Literal[plugin_config.STORED_LAYERS]
    project: str | None
    agent: str | None = Field(description="the handle of the agent profile of an agent layer")
    version: int = Field(description="0 while it was never written")
    values: dict[str, Any] = Field(description="the plain values it sets")
    secrets: list[str] = Field(description="the secret fields it sets; the hub never shows their values")
    rolled_back_to: int | None = Field(description="the older version this one copies, for a rollback")
    changed_by: str | None
    changed_at: datetime | None


class PluginConfigWrite(BaseModel):
    """The next version of a layer: what ``values`` sets and ``unset`` clears; every other field stays."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_FIELDS,
        description="field -> value; a secret field's value is a string the hub seals and never shows again",
    )
    unset: list[str] = Field(default_factory=list, max_length=MAX_FIELDS, description="fields the layer sets now")
    if_version: int | None = Field(None, ge=0, description="409 unless the layer is at this version now")


class PluginConfigRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="the older version of the layer to copy as its next one")
    if_version: int | None = Field(None, ge=0, description="409 unless the layer is at this version now")


class ConfigField(BaseModel):
    name: str
    value: Any = Field(None, description="null for a secret, whose value the hub never shows")
    secret: bool
    layer: Literal[plugin_config.LAYERS] = Field(description="the highest layer that sets it")
    version: int | None = Field(description="that layer's version; null for the defaults and a run's own layer")


class EffectiveConfig(BaseModel):
    """The config a plugin gets in a project, for runs of an agent profile: each field from the highest layer."""

    plugin: str
    version: str = Field(description="the installed version, whose schema the layers are checked against")
    project: str
    agent: str = Field(description="the agent profile whose layer it includes")
    fields: list[ConfigField]
    problems: list[str] = Field(description="what does not fit the schema now, such as a required field none sets")
    digest: str = Field(description="sha256 of the effective config, a secret counted by its sealed value")


class RunPlugin(BaseModel):
    name: str
    version: str = Field(description="the version installed when a worker claimed the run")
    config_digest: str = Field(description="the digest of the run's effective config then")
    recorded_at: datetime


@dataclass(frozen=True)
class Place:
    """Where a stored layer lives: its plugin, its layer, and the project and agent profile it belongs to."""

    plugin_id: int
    plugin: str
    layer: str
    project_id: int | None = None
    project: str | None = None
    profile_id: int | None = None
    handle: str | None = None

    def clause(self):
        c = tables.plugin_configs.c
        return and_(
            c.plugin_id == self.plugin_id,
            c.layer == self.layer,
            c.project_id.is_not_distinct_from(self.project_id),
            c.profile_id.is_not_distinct_from(self.profile_id),
        )

    def aad(self, name: str) -> bytes:
        return plugin_config_aad(self.plugin_id, self.layer, self.project_id, self.profile_id, name)

    def target(self, version: int) -> str:
        where = {"instance": "", "project": f"{self.project}/", "agent": f"{self.project}/@{self.handle}/"}
        return f"{where[self.layer]}plugin:{self.plugin} config {self.layer} v{version}"


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


def _versions_query(place: Place):
    c, u = tables.plugin_configs, tables.users
    return (
        select(
            c.c.version,
            c.c["values"].label("plain"),
            c.c.secrets,
            c.c.rolled_back_to,
            u.c.login.label("changed_by"),
            c.c.changed_at,
        )
        .join_from(c, u, u.c.id == c.c.changed_by)
        .where(place.clause())
        .order_by(c.c.version.desc())
    )


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


async def current(conn: AsyncConnection, place: Place):
    """The current version of the layer at ``place`` (version, values, secrets, ...); None when never written."""
    return (await conn.execute(_versions_query(place).limit(1))).first()


async def read_layer(conn: AsyncConnection, place: Place) -> PluginConfigLayer:
    return layer_model(place, await current(conn, place))


async def layer_versions(conn: AsyncConnection, place: Place) -> list[PluginConfigLayer]:
    """Every version of the layer, the newest first."""
    return [layer_model(place, row) for row in await conn.execute(_versions_query(place))]


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
            secrets[name] = seal(sealer, place, name, value)
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


async def _insert(conn: AsyncConnection, user: Principal, place: Place, version: int, values, secrets, back=None):
    row = {
        "plugin_id": place.plugin_id,
        "layer": place.layer,
        "project_id": place.project_id,
        "profile_id": place.profile_id,
        "version": version,
        "values": values,
        "secrets": secrets,
        "rolled_back_to": back,
        "changed_by": user.user_id,
    }
    await conn.execute(insert(tables.plugin_configs).values(row))


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
    held = await current(conn, place)
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
    await _insert(conn, user, place, now + 1, values, secrets)
    await _audit(conn, user, place, f"{place.target(now + 1)} fields={','.join(changed)}")
    log.info(
        "plugin config changed",
        extra={"plugin": place.plugin, "layer": place.layer, "project": place.project, "version": now + 1},
    )
    return layer_model(place, await current(conn, place))


async def rollback_layer(
    conn: AsyncConnection, user: Principal, place: Place, body: PluginConfigRollback
) -> PluginConfigLayer:
    """Copy version ``body.version`` of the layer at ``place`` as its next version; the layer as it is now."""
    held = await current(conn, place)
    now = 0 if held is None else held.version
    _check_version(place, now, body.if_version)
    if body.version >= now:
        raise HTTPException(
            409, f"the {place.layer} layer of plugin {place.plugin} is at version {now}: roll back to an older one"
        )
    older = (await conn.execute(_versions_query(place).where(tables.plugin_configs.c.version == body.version))).first()
    if older is None:
        raise HTTPException(404, f"the {place.layer} layer of plugin {place.plugin} has no version {body.version}")
    await _insert(conn, user, place, now + 1, older.plain, older.secrets, back=body.version)
    await _audit(conn, user, place, f"{place.target(now + 1)} rollback-to=v{body.version}")
    log.info(
        "plugin config rolled back",
        extra={"plugin": place.plugin, "layer": place.layer, "project": place.project, "version": body.version},
    )
    return layer_model(place, await current(conn, place))


# The effective config


def _stacks_query(plugin_ids: list[int], project_id: int, profile_id: int | None):
    """The current version of each stored layer of ``plugin_ids`` that a run of the project under agent profile
    ``profile_id`` gets."""
    c = tables.plugin_configs.c
    scope = or_(
        c.layer == "instance",
        and_(c.layer == "project", c.project_id == project_id),
        and_(c.layer == "agent", c.project_id == project_id, c.profile_id == profile_id),
    )
    return (
        select(c.plugin_id, c.layer, c.version, c["values"].label("plain"), c.secrets)
        .where(c.plugin_id.in_(plugin_ids), scope)
        .ext(distinct_on(c.plugin_id, c.layer))
        .order_by(c.plugin_id, c.layer, c.version.desc())
    )


async def stored_stacks(
    conn: AsyncConnection, plugin_ids: list[int], project_id: int, profile_id: int | None
) -> dict[int, list[Layer]]:
    """plugin id -> its stored layers a run of the project under ``profile_id`` gets, lowest first."""
    found: dict[int, dict[str, Layer]] = {}
    if plugin_ids:
        for row in await conn.execute(_stacks_query(plugin_ids, project_id, profile_id)):
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


async def run_layer_of(conn: AsyncConnection, run_id: int) -> dict | None:
    r = tables.runs
    return (await conn.execute(select(r.c.plugin_config).where(r.c.id == run_id))).scalar_one_or_none()


async def record_run_plugins(conn: AsyncConnection, run_id: int) -> int:
    """Write, for each plugin enabled on the project of run ``run_id``, installed and turned on, its version and the
    digest of the run's effective config; how many."""
    r = tables.runs
    run = (await conn.execute(select(r.c.project_id, r.c.profile_id, r.c.plugin_config).where(r.c.id == run_id))).one()
    enabled = await enabled_plugins(conn, run.project_id)
    if not enabled:
        return 0
    stacks = await stored_stacks(conn, [plugin.id for plugin in enabled], run.project_id, run.profile_id)
    own = run.plugin_config or {}
    rp = tables.run_plugins
    for plugin in enabled:
        layers = stacks.get(plugin.id, [])
        if own.get(plugin.name):
            layers = [*layers, Layer("run", own[plugin.name])]
        merged = plugin_config.merge(plugin.manifest.get("config"), layers)
        row = {"run_id": run_id, "plugin_id": plugin.id, "version": plugin.installed_version}
        recorded = {"version": plugin.installed_version, "config_digest": merged.digest, "recorded_at": func.now()}
        await conn.execute(
            pg_insert(rp)
            .values(**row, config_digest=merged.digest)
            .on_conflict_do_update(index_elements=[rp.c.run_id, rp.c.plugin_id], set_=recorded)
        )
    return len(enabled)


async def run_plugins(conn: AsyncConnection, run_id: int) -> list[RunPlugin]:
    rp, p = tables.run_plugins, tables.plugins
    query = (
        select(p.c.name, rp.c.version, rp.c.config_digest, rp.c.recorded_at)
        .join_from(rp, p, p.c.id == rp.c.plugin_id)
        .where(rp.c.run_id == run_id)
        .order_by(p.c.name)
    )
    return [RunPlugin(**row._mapping) for row in await conn.execute(query)]


def place_of(plugin, layer: str, **where) -> Place:
    return replace(Place(plugin.id, plugin.name, layer), **where)
