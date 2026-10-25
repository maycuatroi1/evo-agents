"""The probes of a plugin's own rules (API 1.4): a probe the plugin adds for a rule it renders into a repo's harness
files, with no proposal or change of the Curator (``evo_agents.hub.probes``, source ``plugin``), measured each week by
the probe runs as a change's probe is, and removed by the plugin. Private to ``core_plugin_api``."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import probes as shape
from evo_agents.hub import tables
from evo_agents.hub.server import probes
from evo_agents.hub.server.core_plugin_api._reads import enabled_project
from evo_agents.hub.server.core_plugin_api._types import Plugin

MAX_PROBE_REF_CHARS = shape.MAX_REF_CHARS  # (1.4) the key of the plugin's rule a probe names
REPLACED = "replaced by a new probe of the plugin's rule"
REMOVED = "removed by its plugin"


async def _plugin_id(conn: AsyncConnection, plugin: Plugin) -> int:
    pl = tables.plugins
    return (await conn.execute(select(pl.c.id).where(pl.c.name == plugin.id))).scalar_one()


async def _retire(conn: AsyncConnection, project_id: int, plugin_id: int, ref: str, note: str) -> int | None:
    """Retire the active probe of rule ``ref`` of the plugin in the project; its id, or None without one."""
    pr = tables.curator_probes
    found = await conn.execute(
        update(pr)
        .values(state="retired", retired_at=func.now(), retired_note=note, updated_at=func.now())
        .where(pr.c.project_id == project_id, pr.c.plugin_id == plugin_id, pr.c.ref == ref, pr.c.state == "active")
        .returning(pr.c.id)
    )
    return found.scalar_one_or_none()


def _arm(name: str, files: object) -> dict[str, str]:
    if not isinstance(files, Mapping):
        raise ValueError(f"arm {name} is a mapping of a path to the file's text")
    return dict(files)


async def add_probe(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    ref: str,
    *,
    repo: str,
    task: str,
    verify: str,
    on: Mapping[str, str],
    off: Mapping[str, str],
    commit: str | None = None,
) -> int:
    """Add the probe of the plugin's rule ``ref`` (at most ``MAX_PROBE_REF_CHARS`` characters of letters, digits and
    ``._:/@-``) in ``project``, in place of the active one of the same rule: its id. Each week the probe runs give an
    agent ``task`` in ``repo`` at ``commit`` (none: the tip of the default branch now) k times with the files of ``on``
    (path to text: what the plugin renders with the rule) and k times with those of ``off`` (rendered without it), the
    files placed as a change's harness files are (AGENTS.md, CLAUDE.md, a skill's file, .claude/settings.json, a file
    under .claude/hooks/; a path an arm lacks is absent in its sessions), and run ``verify``, one shell line that exits
    0 when the agent kept the rule. The verify is the answer key: the project's admins alone read it, never an agent.
    The probe report and ``evo-agents hub curator probe list|report`` show it with the plugin and ``ref``. ValueError
    for what the hub would not store."""
    project_id = await enabled_project(conn, plugin, project)
    why = shape.ref_problem(ref)
    if why is not None:
        raise ValueError(f"the probe's ref {why}")
    on, off = _arm("on", on), _arm("off", off)
    why = shape.arms_problem(on, off)
    if why is not None:
        raise ValueError(f"the probe's arms {why}")
    try:
        checked = probes.ProbeIn(repo=repo, commit=commit, task=task, verify=verify)
    except ValidationError as exc:
        raise ValueError("; ".join(error["msg"] for error in exc.errors())) from None
    pj, rp = tables.projects, tables.project_repos
    known = await conn.execute(select(rp.c.name).where(rp.c.project_id == project_id, rp.c.name == checked.repo))
    if known.scalar_one_or_none() is None:
        raise ValueError(f"project {project} has no repo {checked.repo}")
    default = (await conn.execute(select(pj.c.default_label).where(pj.c.id == project_id))).scalar_one()
    plugin_id = await _plugin_id(conn, plugin)
    replaced = await _retire(conn, project_id, plugin_id, ref, REPLACED)
    probe_id = await probes.store(
        conn,
        project_id=project_id,
        probe=checked,
        source=shape.PLUGIN,
        label={**dict(default or {}), "projects": [project]},
        replaces=replaced,
        plugin_id=plugin_id,
        ref=ref,
        arms={"on": on, "off": off},
    )
    return probe_id


async def remove_probe(
    conn: AsyncConnection, plugin: Plugin, project: str, ref: str, *, note: str | None = None
) -> bool:
    """Retire the active probe of the plugin's rule ``ref`` in ``project``, with ``note`` (why) if given: no probe run
    takes it from then on, and the report keeps its weeks. False when it has none."""
    project_id = await enabled_project(conn, plugin, project)
    if note is not None and (not isinstance(note, str) or not 1 <= len(note) <= shape.MAX_NOTE_CHARS):
        raise ValueError(f"a note is 1 to {shape.MAX_NOTE_CHARS} characters")
    plugin_id = await _plugin_id(conn, plugin)
    return await _retire(conn, project_id, plugin_id, ref, note or REMOVED) is not None
