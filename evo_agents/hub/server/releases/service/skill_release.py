"""The global skills the hub publishes from the Curator's changes it merged, and the version before it publishes again.

A change the hub merged because its replays won (``evo_agents.hub.server.changes.service.tiers``) keeps their verdict on
its ledger line ``merged`` (``evo_agents.hub.server.ledger``) and, when it releases a skill, what (``wanted``): the one
skill directory arm b of its replays ran with (``replay.verdict``'s paths), when the hub has a global skill of that
directory's name whose latest version came from the change's repo, ``source_repo`` as ``evo-agents hub skills publish
--source-repo`` writes it. A skill of a project, or a global skill published from elsewhere or from nowhere named, is
not released.

``release`` is part of each pass of the job curator.changes, at most MAX_RELEASES changes a pass: for each such merge
without a line ``skill_release`` of its change yet, it reads the skill's directory at the merge commit with the workers'
App (contents: read: the commit's tree whole, then each file's blob; ``pulls.directory_files``), packs it as ``hub
skills publish`` does (``evo_agents.hub.skills.pack``: the same bytes for the same files), puts the bundle in the blob
store as the hub's own (a blob of no project, kind skill-bundle, under the blob lock shared, ``blob_gc``) and records
it as the next version of the global skill, published by the owner of the project's night shift from the change's repo
at the merge commit, audited as skill.publish. The version before it stays, as every version does, and the line
``skill_release`` names both. A directory that cannot be a bundle, one GitHub does not hand whole, a repo with no origin
on GitHub, a project with no night shift or a bundle that is the latest version already makes the line say why nothing
was published; GitHub or the blob store failing leaves the change to the next pass.

When the figures of such a change got worse after its merge, the job curator.outcomes proposes its revert and calls
``republish`` in the same transaction: the version before the hub's becomes the latest again, a new version holding
that version's bundle, published by the owner of the night shift, unless a version newer than the hub's came since,
which it leaves as it is; a line ``skill_release`` again says what it did.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blob_gc, judge, replay, skills
from evo_agents.hub.blobs import BLOB_PREFIX, BlobStoreUnavailable
from evo_agents.hub.server import audit, pulls
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.forge.models.forges import github_target
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.releases.models.skill_release import ACTION, PUBLISH
from evo_agents.hub.server.releases.repository import skills as skills_repo

log = logging.getLogger(__name__)


async def wanted(conn: AsyncConnection, change, found: dict) -> dict | None:
    """What the hub's merge of ``change`` (a row of curator_changes), whose replays won as ``found`` says
    (``replay.verdict``), releases: {skill, directory, scope, version}, the global skill and its latest version now;
    None when it releases none (see the module's docstring)."""
    directories = sorted({found for found in map(replay.skill_dir, found.get("paths") or []) if found})
    if len(directories) != 1:
        return None
    name = PurePosixPath(directories[0]).name
    row = await skills_repo.latest_global(conn, name)
    if row is None or row.name != name or row.source_repo != change.repo:
        return None
    return {"skill": name, "directory": directories[0], "scope": "global", "version": row.version}


async def _line(conn: AsyncConnection, change, change_id: int, what: str, details: dict) -> None:
    """The line ``skill_release`` of change ``change_id`` (``change`` holds its project, proposal and merge)."""
    await ledger_lines.append(
        conn,
        project_id=change.project_id,
        proposal_id=change.proposal_id,
        change_id=change_id,
        action=ACTION,
        actor="curator",
        commit_sha=change.merge_sha,
        what=what,
        details=details,
    )


# Publishing from the merge commit


def _pack(name: str, files: list[tuple[str, bool, bytes]]) -> skills.Bundle:
    """The bundle of skill ``name`` holding ``files`` ((path, executable, bytes) each), packed from a directory of its
    name as ``hub skills publish`` packs one."""
    with tempfile.TemporaryDirectory(prefix="evo-skill-release-") as scratch:
        root = Path(scratch) / name
        root.mkdir()
        for path, executable, data in files:
            problem = skills.path_problem(path)
            if problem:
                raise skills.BundleError(f"{path}: {problem}")
            target = root.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(skills.EXEC_MODE if executable else skills.FILE_MODE)
        return skills.pack(root)


def _put(blobs, bundle: skills.Bundle) -> None:
    handle, path = tempfile.mkstemp(prefix="evo-skill-release-", suffix=".tar.gz")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(bundle.data)
        blobs.put_file(bundle.sha256, Path(path), replace=True)
    finally:
        os.unlink(path)


async def _version(conn: AsyncConnection, change, bundle: skills.Bundle, owner_id: int) -> tuple[int | None, int]:
    """(the version of the global skill made from ``bundle``, None when it is the latest one already; the one before),
    the skill's row locked so the version numbers follow one at a time."""
    skill = await skills_repo.latest_global(conn, bundle.name)
    latest = await skills_repo.lock_latest(conn, skill.id, bundle.name)  # pyright: ignore[reportOptionalMemberAccess]
    if latest.sha256 == bundle.sha256:
        return None, latest.version
    await skills_repo.insert_version(
        conn,
        {
            "skill_id": latest.id,
            "version": latest.version + 1,
            "name": bundle.contents.name,
            "description": bundle.contents.description[: skills.MAX_DESCRIPTION],
            "sha256": bundle.sha256,
            "size": bundle.size,
            "r2_key": BLOB_PREFIX + bundle.sha256,
            "source_repo": change.repo,
            "source_commit": change.merge_sha,
            "published_by": owner_id,
        },
    )
    return latest.version + 1, latest.version


async def _store(engine: AsyncEngine, blobs, change, bundle: skills.Bundle, owner_id: int) -> str:
    await asyncio.to_thread(_put, blobs, bundle)
    async with engine.begin() as conn:
        await blob_gc.lock_shared(conn)
        if await blob_gc.revive(conn, [bundle.sha256]):  # its object may be gone: put the bytes back first
            await asyncio.to_thread(_put, blobs, bundle)
        values = {"project_id": None, "sha256": bundle.sha256, "size": bundle.size, "kind": skills.KIND}
        await skills_repo.insert_blob(conn, {**values, "created_by": owner_id})
        made, before = await _version(conn, change, bundle, owner_id)
        name, merge = bundle.name, change.merge_sha
        details = {"kind": "publish", "skill": name, "scope": "global", "sha256": bundle.sha256, "previous": before}
        if made is None:
            what = f"The hub published nothing: global skill {name} at {merge} of {change.repo} is version {before}"
            await _line(conn, change, change.change_id, what, {**details, "version": before, "published": False})
            return "release_unchanged"
        what = (
            f"The hub published version {made} of global skill {name} from {merge} of {change.repo}; version "
            f"{before} before it stays"
        )
        await _line(conn, change, change.change_id, what, {**details, "version": made, "published": True})
        target = f"skill:global/{name}"
        await audit.record(
            conn, actor_id=None, token_id=None, action=PUBLISH, target=target, project_id=change.project_id
        )
    log.info("curator skill released", extra={"change_id": change.change_id, "skill": name, "version": made})
    return "released"


async def _refusal(engine: AsyncEngine, change, release: dict, why: str) -> str:
    async with engine.begin() as conn:
        what = f"The hub published nothing of global skill {release['skill']} from {change.merge_sha}: {why}"
        details = {"kind": "publish", "skill": release["skill"], "scope": "global", "published": False, "why": why}
        await _line(conn, change, change.change_id, what, details)
    return "release_refused"


async def _publish(engine: AsyncEngine, app, blobs, change) -> str:
    release = change.details["release"]
    target = github_target(change.origin)
    async with engine.begin() as conn:
        owner_id = await skills_repo.night_shift_owner(conn, change.project_id)
    if target is None or not judge.is_sha(change.merge_sha):
        return await _refusal(engine, change, release, "its repo has no origin on GitHub, or no merge commit")
    if owner_id is None:
        return await _refusal(engine, change, release, "the project has no night shift, whose owner publishes it")

    async def read(token: str) -> list[tuple[str, bool, bytes]]:
        return await pulls.directory_files(
            app,
            token,
            target.owner,
            target.name,
            change.merge_sha,
            release["directory"],
            most=skills.MAX_FILES,
            most_bytes=skills.MAX_UNPACKED,
        )

    try:
        files = await pulls.with_token(app, target.owner, target.name, "tree", read)
        bundle = await asyncio.to_thread(_pack, release["skill"], files)
    except (GitHubRefused, skills.BundleError) as exc:
        return await _refusal(engine, change, release, str(exc))
    return await _store(engine, blobs, change, bundle, owner_id)


async def release(engine: AsyncEngine, app, blobs, outcomes: Counter) -> None:
    """Publish the global skills of the hub's merges that release one (see the module's docstring), counting what each
    came to in ``outcomes``; nothing without the workers' App or a blob store."""
    if app is None or blobs is None:
        return
    async with engine.begin() as conn:
        due = await skills_repo.due(conn)
    for change in due:
        try:
            outcomes[await _publish(engine, app, blobs, change)] += 1
        except (GitHubUnavailable, BlobStoreUnavailable) as exc:
            log.warning("a skill release waits", extra={"change_id": change.change_id, "why": str(exc)})
            outcomes["release_waits"] += 1
        except Exception:  # one release failing leaves the others their turn
            log.exception("a skill release failed", extra={"change_id": change.change_id})
            outcomes["release_failed"] += 1


# Publishing the version before again


async def republish(conn: AsyncConnection, change, owner_id: int) -> str | None:
    """After the figures of merged ``change`` (a row of curator_changes) got worse, make the version of its global skill
    before the one the hub published from it the latest again (see the module's docstring), in the caller's
    transaction, as ``owner_id``; what it did, in words for the ledger, or None when the change released no skill."""
    details = await skills_repo.release_details(conn, change.id)
    if not isinstance(details, dict) or details.get("kind") != "publish" or details.get("published") is not True:
        return None
    name, made, before = str(details.get("skill")), details.get("version"), details.get("previous")
    latest = await skills_repo.latest_global(conn, name)
    if latest is not None:  # locked, then read again, so the version numbers follow one at a time
        latest = await skills_repo.lock_latest(conn, latest.id, name)
    done = {"kind": "republish", "skill": name, "scope": "global", "reverted": made, "previous": before}
    why = None
    if latest is None or latest.version != made:
        why = f"its latest version is no longer the {made} the hub published, so it is left as it is"
    elif not isinstance(before, int):
        why = f"version {made} has no version before it"
    if why is not None:
        what = f"The hub published no version of global skill {name} again: {why}"
        await _line(conn, change, change.id, what, {**done, "published": False, "why": why})
        return what
    old = await skills_repo.version_of(conn, latest.id, before)  # pyright: ignore[reportOptionalMemberAccess, reportArgumentType]
    copied = {key: getattr(old, key) for key in ("name", "description", "sha256", "size", "r2_key")}
    copied |= {"source_repo": old.source_repo, "source_commit": old.source_commit}
    version = latest.version + 1  # pyright: ignore[reportOptionalMemberAccess]
    await skills_repo.insert_version(
        conn,
        {"skill_id": latest.id, "version": version, "published_by": owner_id, **copied},  # pyright: ignore[reportOptionalMemberAccess]
    )
    what = f"The hub published version {before} of global skill {name} again as version {version}, after {made}"
    await _line(conn, change, change.id, what, {**done, "version": version, "published": True, "sha256": old.sha256})
    target = f"skill:global/{name}"
    await audit.record(conn, actor_id=None, token_id=None, action=PUBLISH, target=target, project_id=change.project_id)
    log.info("curator skill published again", extra={"change_id": change.id, "skill": name, "version": version})
    return what
