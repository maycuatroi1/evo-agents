"""The proposals of @builder of a setting the hub keeps in layers or versions (``proposals``): ``branding`` and
``features`` (``hub_settings``), ``plugin_config`` (``plugins.service.configs``), ``agent`` (an agent profile) and ``policy``
(the autonomy policy): where one applies, how it is checked, applied and rolled back, as the ``ProposalKind`` of each.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.core_plugins.builder.kinds import (
    BRANDING,
    Change,
    ProposalKind,
    Target,
    Writing,
    copy_note,
    layer_problems,
    refuse,
)
from evo_agents.hub import autonomy, branding, features
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.agents.models.packs import AgentPackPin
from evo_agents.hub.server.agents.models.profiles import AgentEdit
from evo_agents.hub.server.agents.service.packs import pinned
from evo_agents.hub.server.agents.service.profiles import new_version, profile_changes
from evo_agents.hub.server.autonomy.repository import policies as policy_store
from evo_agents.hub.server.autonomy.service import policies as autonomy_policies
from evo_agents.hub.server.plugins.models.configs import Place, PluginConfigRollback, PluginConfigWrite
from evo_agents.hub.server.plugins.repository.configs import current
from evo_agents.hub.server.plugins.service.configs import place_of, rollback_layer, write_layer
from evo_agents.hub.server.plugins.service.grants import installed_row
from evo_agents.hub.server.profile_store import Profile, by_handle, version_rules
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.plugins import config as plugin_config

AGENT_FIELDS = ("name", "description", "instructions", "packs")


# A layer, of the hub's settings or of a plugin's config


class LayerKind(ProposalKind):
    """A setting kept in layers with versions, where version 0 is no layer: applying checks the change again under the
    lock, and rolling back writes a copy of version ``applied - 1``, or unsets what the layer holds."""

    async def apply(self, w: Writing) -> int:
        await self.check(w.conn, w.access, w.change, w.target, w.hub_version)
        return await super().apply(w)

    async def copy(self, w: Writing, before: int) -> int:
        """The layer's next version, a copy of version ``before``."""
        raise NotImplementedError

    async def clear(self, w: Writing) -> int:
        """The layer's next version, which unsets all it holds."""
        raise NotImplementedError

    async def roll_back(self, w: Writing) -> int:
        """The next version of the layer, a copy of version ``applied - 1``; version 0 is no layer."""
        before = w.version - 1
        return await self.copy(w, before) if before >= 1 else await self.clear(w)


# Branding and features: a layer of the hub's settings


def _no_problems(sets: dict, after: dict) -> list[str]:
    return []


class SettingsKind(LayerKind):
    def values(self, change: Change) -> dict:
        return branding.normalized(change.sets) if change.kind == BRANDING else change.sets

    def place(self, target: Target) -> hub_settings.Place:
        assert target.settings is not None  # located for branding or features
        return target.settings

    async def locate(self, conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
        if change.kind == BRANDING:
            place = hub_settings.Place(hub_settings.BRANDING)
        elif change.target["layer"] == "hub":
            place = hub_settings.Place(hub_settings.FEATURES)
        else:
            place = hub_settings.Place(hub_settings.FEATURES, access.project_id, access.name)
        row = await hub_settings.current(conn, place)
        held = dict(row.plain) if row is not None else {}
        return Target(place.title(), 0 if row is None else row.version, held, settings=place)

    def branding_problems(self, change: Change, target: Target) -> list[str]:
        images = sorted(set(change.sets) & set(branding.ASSETS))
        if images:
            return [f"{name}: an image, which an admin uploads; @builder may unset it, never set it" for name in images]
        values = self.values(change)
        after = {name: value for name, value in target.held.items() if name not in change.unset} | values
        return branding.problems(values, after)

    def features_problems(self, change: Change, target: Target) -> list[str]:
        layer = features.INSTANCE if self.place(target).project_id is None else features.PROJECT
        return features.problems(change.sets, layer)

    def kind_problems(self, change: Change, target: Target) -> list[str]:
        if change.kind == BRANDING:
            return self.branding_problems(change, target)
        return self.features_problems(change, target)

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        return self.kind_problems(change, target) or layer_problems(change, target, self.values(change))

    async def body(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> dict:
        return self.values(change)

    async def write(self, w: Writing, body: Any, note: str, via: str) -> int:
        def checked(sets: dict, after: dict) -> list[str]:
            return self.kind_problems(w.change, w.target)

        place, unset = self.place(w.target), list(w.change.unset)
        return (await hub_settings.apply(w.conn, w.user, place, body, unset, w.version, checked)).version

    async def copy(self, w: Writing, before: int) -> int:
        body = hub_settings.SettingsRollback(version=before, if_version=w.version)
        return (await hub_settings.rollback_layer(w.conn, w.user, self.place(w.target), body)).version

    async def clear(self, w: Writing) -> int:
        place, held = self.place(w.target), sorted(w.target.held)
        return (await hub_settings.apply(w.conn, w.user, place, {}, held, w.version, _no_problems)).version


SETTINGS = SettingsKind()


# The config of a plugin: its layer of the project or of an agent profile


async def _profile(conn: AsyncConnection, access: ProjectAccess, handle: str, *, lock: bool = False) -> Profile:
    profile = await by_handle(conn, handle, lock=lock)
    if profile is None or profile.project_id != access.project_id or profile.deleted_at is not None:
        raise ProposalError(
            404,
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`",
        )
    return profile


class ConfigKind(LayerKind):
    def place(self, target: Target) -> Place:
        assert target.config is not None  # located for plugin_config
        return target.config

    async def locate(self, conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
        """The layer of the config ``change`` names as it is now; ``lock``: one write of the plugin's config at a
        time."""
        name, handle = change.target["plugin"], change.target.get("agent")
        if lock:
            await installed_row(conn, access, name, lock=True)
        plugin = await enabled_in(conn, name, access.project_id)
        if plugin is None:
            raise ProposalError(409, f"plugin {name} is not enabled in project {access.name}, or it is turned off")
        place = place_of(plugin, "project", project_id=access.project_id, project=access.name)
        title = f"the config of plugin {name} in project {access.name}"
        if handle is not None:
            profile = await _profile(conn, access, handle)
            place = place_of(
                plugin, "agent", project_id=access.project_id, project=access.name, profile_id=profile.id, handle=handle
            )
            title = f"the config of plugin {name} for @{handle} in project {access.name}"
        row = await current(conn, place)
        held = {**dict.fromkeys(row.secrets), **row.plain} if row is not None else {}
        return Target(
            title, 0 if row is None else row.version, held, config=place, schema=plugin.manifest.get("config")
        )

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        secret = plugin_config.fields(target.schema)
        secrets = sorted(name for name in change.sets if secret.get(name))
        if secrets:
            return [
                f"{name}: a secret, which @builder never sets, since it would see its value; an admin sets it with "
                "`evo-agents hub plugin config`"
                for name in secrets
            ]
        return plugin_config.check_values(target.schema, change.sets) or layer_problems(change, target)

    async def write(self, w: Writing, body: Any, note: str, via: str) -> int:
        written = PluginConfigWrite(values=body, unset=list(w.change.unset), if_version=w.version)
        return (await write_layer(w.conn, w.user, self.place(w.target), w.target.schema, written, None)).version

    async def copy(self, w: Writing, before: int) -> int:
        body = PluginConfigRollback(version=before, if_version=w.version)
        return (await rollback_layer(w.conn, w.user, self.place(w.target), body)).version

    async def clear(self, w: Writing) -> int:
        body = PluginConfigWrite(unset=sorted(w.target.held), if_version=w.version)
        return (await write_layer(w.conn, w.user, self.place(w.target), w.target.schema, body, None)).version


CONFIG = ConfigKind()


# An agent profile of the project


class AgentKind(ProposalKind):
    """An agent profile: its version is the profile's, and approving writes the columns the change sets as its next
    one."""

    def profile(self, target: Target) -> Profile:
        assert target.profile is not None  # located for agent
        return target.profile

    async def locate(self, conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
        profile = await _profile(conn, access, change.target["agent"], lock=lock)
        if profile.is_default:
            raise ProposalError(
                422, f"@{profile.handle} is the default agent of project {access.name}, which @builder does not change"
            )
        return Target(
            f"agent @{profile.handle} of project {access.name}", profile.version, profile.snapshot(), profile=profile
        )

    async def body(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> dict:
        """The columns ``change`` sets on the profile of ``target``, checked as an edit of the profile is."""
        extra = sorted(set(change.sets) - set(AGENT_FIELDS))
        if extra or change.unset:
            raise ProposalError(
                422,
                f"an agent proposal sets {', '.join(AGENT_FIELDS)} and unsets nothing; "
                f"{', '.join(extra or change.unset)}",
            )
        try:
            body = AgentEdit.model_validate(change.sets)
        except ValidationError as exc:
            said = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())
            raise ProposalError(422, said) from None
        try:
            packs = await pinned(conn, access.project_id, access.name, body.packs, hub_version)
            changes = profile_changes(self.profile(target), body, packs)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, str(exc.detail)) from None
        if not changes:
            raise ProposalError(422, f"{target.title} has these values already: it changes nothing")
        return changes

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        await self.body(conn, access, change, target, hub_version)
        return []

    async def write(self, w: Writing, body: Any, note: str, via: str) -> int:
        profile, access, user = self.profile(w.target), w.access, w.user
        version = await new_version(w.conn, profile, body, user.user_id)
        target = f"{access.name}/@{profile.handle} v{version} fields={','.join(sorted(body))}"
        await audit.record(
            w.conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.AGENT_UPDATE,
            target=target,
            project_id=access.project_id,
        )
        return version

    async def restored(self, w: Writing, before: int) -> tuple[Any, str]:
        """The columns of the change as version ``before`` of the profile had them, those that differ from now."""
        profile, access, target = self.profile(w.target), w.access, w.target
        held = await version_rules(w.conn, profile.id, before)
        if held is None:
            raise ProposalError(409, f"{target.title} has no version {before}; {NOTHING}")
        wanted = {name: held[name] for name in w.change.sets if name != "packs"}
        if "packs" in w.change.sets:
            pins = [AgentPackPin(**item) for item in held["packs"] or ()]
            try:
                wanted["packs"] = await pinned(w.conn, access.project_id, access.name, pins, w.hub_version)
            except HTTPException as exc:
                raise ProposalError(exc.status_code, str(exc.detail)) from None
        now = profile.snapshot()
        changes = {name: value for name, value in wanted.items() if now[name] != value}
        if not changes:
            raise ProposalError(409, f"{target.title} is as it was before the proposal already; {NOTHING}")
        return changes, copy_note(w, before)


AGENT_PROFILE = AgentKind()


# The autonomy policy of the project


class PolicyKind(ProposalKind):
    """The autonomy policy: its version is the policy's revision, and revision 0 is no policy."""

    async def locate(self, conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
        row = await policy_store.newest(conn, access.project_id)
        body = {} if row is None else dict(row.body)
        title = f"the autonomy policy of project {access.name}"
        return Target(title, 0 if row is None else row.revision, autonomy.flat(body), policy=body)

    async def body(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> dict:
        """The policy ``change`` makes of the revision ``target`` holds, checked as a write of the policy is."""
        assert target.policy is not None  # located for policy
        body, problems = autonomy.with_changes(target.policy, change.sets, change.unset)
        if not problems:
            problems = layer_problems(change, target)
        if not problems:
            problems = autonomy.problems(body, await policy_store.project_repos(conn, access.project_id))
        refuse(problems)
        return body

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        await self.body(conn, access, change, target, hub_version)
        return []

    async def write(self, w: Writing, body: Any, note: str, via: str) -> int:
        access = w.access
        revision, written = await autonomy_policies.write(
            w.conn, w.user, access, body, note=note, if_revision=w.version, via=via
        )
        if not written:
            raise ProposalError(409, f"the autonomy policy of project {access.name} is this already; {NOTHING}")
        return revision

    async def restored(self, w: Writing, before: int) -> tuple[Any, str]:
        body = await policy_store.body_at(w.conn, w.access.project_id, before)
        if body is None:
            raise ProposalError(
                409, f"the autonomy policy of project {w.access.name} has no revision {before}; {NOTHING}"
            )
        return body, copy_note(w, before)


AUTONOMY_POLICY = PolicyKind()
