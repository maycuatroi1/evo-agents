"""Where the flow that follows an archive's upload stands (plan kg-zip-to-answers, step 4), without the database: the
upload sent (``uploading``), read (``extracting``), the build it queued (``building``), the starter questions
(``questions``) and the Curator's kg_init run (``curator``) after that build, and the entities step (``entities``),
in the build when the project's strategy runs one already, else in the rebuild the Curator's settings queued.
``evo_agents.hub.server.kg_pipeline`` reads the rows and serves ``derive``'s answer.

Each phase is ``pending``, ``running``, ``done``, ``skipped`` (nothing to do, or it ended without stopping the flow,
its detail says why) or ``failed``. The flow is ``running`` while a phase runs or waits, ``ready`` once every phase is
done or skipped, ``failed`` at the first phase that failed, and ``replaced`` when a later upload of the same source
was read first, so this one queued no build. The entities phase fails only when the step was on and gave no node
while its items failed or it stopped (no key, the ceiling); items deferred to a run with Claude Code skip it.

The phases come in the order they run: entities follows building when that build ran the step, and follows the
Curator otherwise. Details hold counts, states and the hub's own error text, never an item of the data.
"""

from __future__ import annotations

from collections.abc import Mapping

from evo_agents.hub import kg_questions

PHASES = ("uploading", "extracting", "building", "entities", "questions", "curator")
PHASE_STATES = ("pending", "running", "done", "skipped", "failed")
STATES = ("running", "ready", "failed", "replaced")
ENDED = ("ready", "failed", "replaced")
NOTICE = "kg_pipeline"  # runs.NOTICE_KINDS: the flow of an upload ended ready or failed
MOVING_BUILD = ("queued", "running")
DETAIL_CHARS = 500


def _phase(name: str, state: str, detail: str | None = None) -> dict:
    return {"name": name, "state": state, "detail": None if detail is None else str(detail)[:DETAIL_CHARS]}


def _upload(upload: Mapping) -> tuple[list[dict], bool]:
    """(the phases uploading and extracting, whether the archive was read)."""
    state = upload.get("state")
    if state == "uploading":
        return [_phase("uploading", "running"), _phase("extracting", "pending")], False
    if state == "extracting":
        return [_phase("uploading", "done"), _phase("extracting", "running")], False
    if state == "failed":
        error = upload.get("error") or "the upload failed"
        if upload.get("uploaded_at") is None:
            return [_phase("uploading", "failed", error), _phase("extracting", "pending")], False
        return [_phase("uploading", "done"), _phase("extracting", "failed", error)], False
    files = upload.get("files")
    return [
        _phase("uploading", "done"),
        _phase("extracting", "done", None if files is None else f"{files} files"),
    ], True


def _build(build: Mapping | None, what: str) -> tuple[str, str | None]:
    if build is None:
        return "failed", f"{what} is gone"
    status = build.get("status")
    if status in MOVING_BUILD:
        return "running", f"{what} {status}"
    if status == "succeeded":
        return "done", f"{what}: {build.get('nodes') or 0} nodes, {build.get('edges') or 0} edges"
    return "failed", build.get("error") or f"{what} {status}"


def _entities_of(coverage: Mapping | None) -> dict | None:
    found = (coverage or {}).get("entities")
    return found if isinstance(found, dict) and found.get("enabled") else None


def _entities(cov: dict | None, why_none: str) -> dict:
    if cov is None:
        return _phase("entities", "skipped", why_none)
    nodes, edges = int(cov.get("nodes") or 0), int(cov.get("edges") or 0)
    stopped, failed = cov.get("stopped"), int(cov.get("failed") or 0)
    said = f"{nodes} entities, {edges} relations"
    if nodes == 0 and int(cov.get("deferred") or 0):
        return _phase("entities", "skipped", stopped or f"{cov['deferred']} items wait for a run with Claude Code")
    if nodes == 0 and (stopped or failed):
        return _phase("entities", "failed", stopped or f"{failed} items failed: {'; '.join(cov.get('errors') or [])}")
    return _phase("entities", "done", f"{said}; {stopped}" if stopped else said)


def _questions(init: Mapping) -> dict:
    asked = (init.get("body") or {}).get("questions") or {}
    state = asked.get("state")
    if not asked:
        if init.get("state") in ("done", "failed"):
            return _phase("questions", "skipped", "the kg_init run ended without looking")
        return _phase("questions", "running", "looking whether the project needs starter questions")
    if state == kg_questions.QUEUED:
        notes = [str(n) for n in (init.get("body") or {}).get("notes") or [] if str(n).startswith("no review run")]
        return _phase("questions", "running", notes[-1] if notes else "a review run waits to be queued")
    if state == kg_questions.RUNNING:
        return _phase("questions", "running", f"review run #{asked.get('agent_run_id')}")
    if state == kg_questions.DONE:
        return _phase("questions", "done", f"eval set version {asked.get('version')}" if asked.get("version") else None)
    return _phase("questions", "skipped", asked.get("ended") or asked.get("why"))


def _curator(init: Mapping) -> dict:
    state = init.get("state")
    if state == "queued":
        notes = [str(n) for n in (init.get("body") or {}).get("notes") or []]
        return _phase("curator", "running", notes[-1] if notes else f"kg_init run #{init.get('id')} queued")
    if state == "running":
        return _phase("curator", "running", f"review run #{init.get('agent_run_id')}")
    if state == "done":
        return _phase("curator", "done", f"kg_init run #{init.get('id')}")
    return _phase("curator", "failed", init.get("error") or f"kg_init run #{init.get('id')} failed")


def rebuild_of(init: Mapping | None) -> int | None:
    """The build the decision of kg_init run ``init`` queued, None when it queued none."""
    change = (((init or {}).get("body") or {}).get("applied") or {}).get("change") or {}
    found = change.get("build_id")
    return found if isinstance(found, int) else None


def _after_build(build: Mapping, init: Mapping | None, waiting: Mapping | None, rebuild: Mapping | None) -> list[dict]:
    """The phases entities, questions and curator once the build succeeded, in the order they run."""
    in_build = _entities_of(build.get("coverage"))
    if init is None:
        if waiting is not None:
            detail = f"waits for kg_init run #{waiting.get('id')}, in flight since before this build"
            tail = [_phase("questions", "pending", detail), _phase("curator", "pending", detail)]
        else:
            why = "no kg_init run followed this build: the graph's profile did not change enough to ask for one"
            tail = [_phase("questions", "skipped", why), _phase("curator", "skipped", why)]
        return [_entities(in_build, "the project's strategy runs no entities step"), *tail]
    asked = [_questions(init), _curator(init)]
    rebuild_id = rebuild_of(init)
    if rebuild_id is None:
        if in_build is not None:
            return [_entities(in_build, ""), *asked]
        if init.get("state") in ("queued", "running"):
            return [*asked, _phase("entities", "pending", "after the Curator decides the strategy")]
        return [*asked, _phase("entities", "skipped", "the Curator's settings needed no rebuild")]
    state, detail = _build(rebuild, f"rebuild #{rebuild_id} with the Curator's settings")
    if state != "done":
        return [*asked, _phase("entities", state, detail)]
    return [
        *asked,
        _entities(_entities_of((rebuild or {}).get("coverage")), "the Curator's strategy runs no entities step"),
    ]


def derive(
    upload: Mapping,
    build: Mapping | None = None,
    init: Mapping | None = None,
    *,
    waiting: Mapping | None = None,
    rebuild: Mapping | None = None,
) -> dict:
    """The flow of ``upload`` (its state, uploaded_at, files, error, build_id, replaced_by): ``build`` the build it
    queued, ``init`` the first kg_init run whose build is that one or later, ``waiting`` a kg_init run in flight from
    before when ``init`` is None, ``rebuild`` the build the Curator's decision queued; each a mapping of its row.
    {state, phase, phases, error}: ``phase`` the phase running or failed, None once ready."""
    phases, read = _upload(upload)
    if read and upload.get("replaced_by") is not None and upload.get("build_id") is None:
        rest = [_phase(name, "skipped", f"upload #{upload['replaced_by']} replaced it") for name in PHASES[2:]]
        return {"state": "replaced", "phase": None, "phases": [*phases, *rest], "error": None}
    if not read:
        phases += [_phase(name, "pending") for name in PHASES[2:]]
    elif upload.get("build_id") is None:
        phases.append(
            _phase("building", "failed", "no build was queued: the project has no knowledge config or is archived")
        )
        phases += [_phase(name, "pending") for name in PHASES[3:]]
    else:
        state, detail = _build(build, f"build #{upload['build_id']}")
        phases.append(_phase("building", state, detail))
        if state == "done":
            phases += _after_build(build or {}, init, waiting, rebuild)
        else:
            phases += [_phase(name, "pending") for name in PHASES[3:]]
    failed = next((p for p in phases if p["state"] == "failed"), None)
    if failed is not None:
        return {"state": "failed", "phase": failed["name"], "phases": phases, "error": failed["detail"]}
    current = next((p for p in phases if p["state"] in ("running", "pending")), None)
    if current is None:
        return {"state": "ready", "phase": None, "phases": phases, "error": None}
    return {"state": "running", "phase": current["name"], "phases": phases, "error": None}
