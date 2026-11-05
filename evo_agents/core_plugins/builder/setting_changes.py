"""The proposals of @builder of a setting the hub keeps in layers or versions (``proposals``): ``branding`` and
``features`` (``hub_settings``), ``plugin_config`` (``plugin_configs``), ``agent`` (an agent profile) and ``policy``
(the autonomy policy): where one applies, how it is checked, applied and rolled back, as the ``Kind`` of each.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.core_plugins.builder.kinds import (
    BRANDING,
    Change,
    Kind,
    Target,
    Writing,
    layer_problems,
    refuse,
)
from evo_agents.hub import autonomy, branding, features
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import audit, autonomy_policies, hub_settings, plugin_configs
from evo_agents.hub.server.agent_packs import AgentPackPin, pinned
from evo_agents.hub.server.agent_profiles import AgentEdit, new_version, profile_changes
from evo_agents.hub.server.plugin_grants import installed_row
from evo_agents.hub.server.profile_store import Profile, by_handle, version_rules
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.plugins import config as plugin_config

AGENT_FIELDS = ("name", "description", "instructions", "packs")


# Branding and features: a layer of the hub's settings


def _values(change: Change) -> dict:
    return branding.normalized(change.sets) if change.kind == BRANDING else change.sets


def _settings_place(target: Target) -> hub_settings.Place:
    assert target.settings is not None  # located for branding or features
    return target.settings


async def _settings_target(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    if change.kind == BRANDING:
        place = hub_settings.Place(hub_settings.BRANDING)
    elif change.target["layer"] == "hub":
        place = hub_settings.Place(hub_settings.FEATURES)
    else:
        place = hub_settings.Place(hub_settings.FEATURES, access.project_id, access.name)
    row = await hub_settings.current(conn, place)
    held = dict(row.plain) if row is not None else {}
    return Target(place.title(), 0 if row is None else row.version, held, settings=place)


def _branding_problems(change: Change, target: Target) -> list[str]:
    images = sorted(set(change.sets) & set(branding.ASSETS))
    if images:
        return [f"{name}: an image, which an admin uploads; @builder may unset it, never set it" for name in images]
    values = _values(change)
    after = {name: value for name, value in target.held.items() if name not in change.unset} | values
    return branding.problems(values, after)


def _features_problems(change: Change, target: Target) -> list[str]:
    layer = features.INSTANCE if _settings_place(target).project_id is None else features.PROJECT
    return features.problems(change.sets, layer)


def _kind_problems(change: Change, target: Target) -> list[str]:
    return _branding_problems(change, target) if change.kind == BRANDING else _features_problems(change, target)


def _settings_problems(change: Change, target: Target) -> list[str]:
    return _kind_problems(change, target) or layer_problems(change, target, _values(change))


async def _check_settings(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    refuse(_settings_problems(change, target))


def _no_problems(sets: dict, after: dict) -> list[str]:
    return []


async def _apply_settings(w: Writing) -> int:
    def checked(sets: dict, after: dict) -> list[str]:
        return _kind_problems(w.change, w.target)

    await _check_settings(w.conn, w.access, w.change, w.target, w.hub_version)
    place = _settings_place(w.target)
    unset = list(w.change.unset)
    return (await hub_settings.apply(w.conn, w.user, place, _values(w.change), unset, w.version, checked)).version


async def _roll_back_settings(w: Writing) -> int:
    """The next version of the layer, a copy of version ``applied - 1``; version 0 is no layer."""
    place, before = _settings_place(w.target), w.version - 1
    if before >= 1:
        body = hub_settings.SettingsRollback(version=before, if_version=w.version)
        return (await hub_settings.rollback_layer(w.conn, w.user, place, body)).version
    layer = await hub_settings.apply(w.conn, w.user, place, {}, sorted(w.target.held), w.version, _no_problems)
    return layer.version


SETTINGS = Kind(_settings_target, _check_settings, _apply_settings, _roll_back_settings)


# The config of a plugin: its layer of the project or of an agent profile


async def _profile(conn: AsyncConnection, access: ProjectAccess, handle: str, *, lock: bool = False) -> Profile:
    profile = await by_handle(conn, handle, lock=lock)
    if profile is None or profile.project_id != access.project_id or profile.deleted_at is not None:
        raise ProposalError(
            404,
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`",
        )
    return profile


def _config_place(target: Target) -> plugin_configs.Place:
    assert target.config is not None  # located for plugin_config
    return target.config


async def _config_target(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    """The layer of the config ``change`` names as it is now; ``lock``: one write of the plugin's config at a time."""
    name, handle = change.target["plugin"], change.target.get("agent")
    if lock:
        await installed_row(conn, access, name, lock=True)
    plugin = await enabled_in(conn, name, access.project_id)
    if plugin is None:
        raise ProposalError(409, f"plugin {name} is not enabled in project {access.name}, or it is turned off")
    place = plugin_configs.place_of(plugin, "project", project_id=access.project_id, project=access.name)
    title = f"the config of plugin {name} in project {access.name}"
    if handle is not None:
        profile = await _profile(conn, access, handle)
        place = plugin_configs.place_of(
            plugin, "agent", project_id=access.project_id, project=access.name, profile_id=profile.id, handle=handle
        )
        title = f"the config of plugin {name} for @{handle} in project {access.name}"
    row = await plugin_configs.current(conn, place)
    held = {**dict.fromkeys(row.secrets), **row.plain} if row is not None else {}
    return Target(title, 0 if row is None else row.version, held, config=place, schema=plugin.manifest.get("config"))


def _config_problems(change: Change, target: Target) -> list[str]:
    secret = plugin_config.fields(target.schema)
    secrets = sorted(name for name in change.sets if secret.get(name))
    if secrets:
        return [
            f"{name}: a secret, which @builder never sets, since it would see its value; an admin sets it with "
            "`evo-agents hub plugin config`"
            for name in secrets
        ]
    return plugin_config.check_values(target.schema, change.sets)


async def _check_config(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    refuse(_config_problems(change, target) or layer_problems(change, target))


async def _apply_config(w: Writing) -> int:
    await _check_config(w.conn, w.access, w.change, w.target, w.hub_version)
    body = plugin_configs.PluginConfigWrite(values=w.change.sets, unset=list(w.change.unset), if_version=w.version)
    return (
        await plugin_configs.write_layer(w.conn, w.user, _config_place(w.target), w.target.schema, body, None)
    ).version


async def _roll_back_config(w: Writing) -> int:
    """The next version of the layer, a copy of version ``applied - 1``; version 0 is no layer."""
    place, before = _config_place(w.target), w.version - 1
    if before >= 1:
        body = plugin_configs.PluginConfigRollback(version=before, if_version=w.version)
        return (await plugin_configs.rollback_layer(w.conn, w.user, place, body)).version
    body = plugin_configs.PluginConfigWrite(unset=sorted(w.target.held), if_version=w.version)
    return (await plugin_configs.write_layer(w.conn, w.user, place, w.target.schema, body, None)).version


CONFIG = Kind(_config_target, _check_config, _apply_config, _roll_back_config)


# An agent profile of the project


def _profile_of(target: Target) -> Profile:
    assert target.profile is not None  # located for agent
    return target.profile


async def _agent_target(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    profile = await _profile(conn, access, change.target["agent"], lock=lock)
    if profile.is_default:
        raise ProposalError(
            422, f"@{profile.handle} is the default agent of project {access.name}, which @builder does not change"
        )
    return Target(
        f"agent @{profile.handle} of project {access.name}", profile.version, profile.snapshot(), profile=profile
    )


async def _agent_changes(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> dict:
    """The columns ``change`` sets on the profile of ``target``, checked as an edit of the profile is."""
    extra = sorted(set(change.sets) - set(AGENT_FIELDS))
    if extra or change.unset:
        raise ProposalError(
            422,
            f"an agent proposal sets {', '.join(AGENT_FIELDS)} and unsets nothing; {', '.join(extra or change.unset)}",
        )
    try:
        body = AgentEdit.model_validate(change.sets)
    except ValidationError as exc:
        said = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())
        raise ProposalError(422, said) from None
    try:
        packs = await pinned(conn, access.project_id, access.name, body.packs, hub_version)
        changes = profile_changes(_profile_of(target), body, packs)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    if not changes:
        raise ProposalError(422, f"{target.title} has these values already: it changes nothing")
    return changes


async def _check_agent(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    await _agent_changes(conn, access, change, target, hub_version)


async def _write_profile(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, profile: Profile, changes: dict
) -> int:
    version = await new_version(conn, profile, changes, user.user_id)
    target = f"{access.name}/@{profile.handle} v{version} fields={','.join(sorted(changes))}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.AGENT_UPDATE,
        target=target,
        project_id=access.project_id,
    )
    return version


async def _apply_agent(w: Writing) -> int:
    changes = await _agent_changes(w.conn, w.access, w.change, w.target, w.hub_version)
    return await _write_profile(w.conn, w.user, w.access, _profile_of(w.target), changes)


async def _roll_back_agent(w: Writing) -> int:
    profile, access, target = _profile_of(w.target), w.access, w.target
    before = await version_rules(w.conn, profile.id, profile.version - 1)
    if before is None:
        raise ProposalError(409, f"{target.title} has no version {profile.version - 1}; {NOTHING}")
    wanted = {name: before[name] for name in w.change.sets if name != "packs"}
    if "packs" in w.change.sets:
        pins = [AgentPackPin(**item) for item in before["packs"] or ()]
        try:
            wanted["packs"] = await pinned(w.conn, access.project_id, access.name, pins, w.hub_version)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, str(exc.detail)) from None
    now = profile.snapshot()
    changes = {name: value for name, value in wanted.items() if now[name] != value}
    if not changes:
        raise ProposalError(409, f"{target.title} is as it was before the proposal already; {NOTHING}")
    return await _write_profile(w.conn, w.user, access, profile, changes)


AGENT_PROFILE = Kind(_agent_target, _check_agent, _apply_agent, _roll_back_agent)


# The autonomy policy of the project


async def _policy_target(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    row = await autonomy_policies.newest(conn, access.project_id)
    body = {} if row is None else dict(row.body)
    title = f"the autonomy policy of project {access.name}"
    return Target(title, 0 if row is None else row.revision, autonomy.flat(body), policy=body)


async def _policy_body(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target) -> dict:
    """The policy ``change`` makes of the revision ``target`` holds, checked as a write of the policy is."""
    assert target.policy is not None  # located for policy
    body, problems = autonomy.with_changes(target.policy, change.sets, change.unset)
    if not problems:
        problems = layer_problems(change, target)
    if not problems:
        problems = autonomy.problems(body, await autonomy_policies.project_repos(conn, access.project_id))
    refuse(problems)
    return body


async def _check_policy(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    await _policy_body(conn, access, change, target)


async def _write_policy(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, body: dict, base: int, note: str, via: str
) -> int:
    """Write ``body`` as the policy's next revision while it is at revision ``base``; that revision."""
    revision, written = await autonomy_policies.write(conn, user, access, body, note=note, if_revision=base, via=via)
    if not written:
        raise ProposalError(409, f"the autonomy policy of project {access.name} is this already; {NOTHING}")
    return revision


async def _apply_policy(w: Writing) -> int:
    body = await _policy_body(w.conn, w.access, w.change, w.target)
    return await _write_policy(w.conn, w.user, w.access, body, w.version, w.note, f" proposal={w.proposal_id}")


async def _roll_back_policy(w: Writing) -> int:
    """The policy's next revision, a copy of revision ``applied - 1``; revision 0 is no policy."""
    access, before = w.access, w.version - 1
    body = await autonomy_policies.body_at(w.conn, access.project_id, before)
    if body is None:
        raise ProposalError(409, f"the autonomy policy of project {access.name} has no revision {before}; {NOTHING}")
    note = f"rollback of proposal {w.proposal_id} of @builder: a copy of revision {before}"
    via = f" proposal={w.proposal_id} copy-of={before}"
    return await _write_policy(w.conn, w.user, access, body, w.version, note, via)


AUTONOMY_POLICY = Kind(_policy_target, _check_policy, _apply_policy, _roll_back_policy)
