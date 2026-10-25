"""The fit of a worker for a run under an agent profile that is not its project's default
(``runs.service.worker_fit.lacks_agent_home``): the pick of a chat run's worker and the candidates of a run refuse a
daemon without the feature agent_home exactly when the claim would (``profiles.claim_clause``), and say so."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="the fit of a worker needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import runs  # noqa: E402
from evo_agents.hub.server.runs.service.worker_fit import lacks_agent_home, unfit_worker, worker_problems  # noqa: E402

AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime")
OLD = tuple(feature for feature in runs.WORKER_FEATURES if feature != runs.AGENT_HOME)
UP = {"available": True, "version": "1"}


def profile(*, default: bool = False, runtimes: list[str] | None = None) -> SimpleNamespace:
    permissions = {axis: {"mode": "all"} for axis in AXES}
    if runtimes is not None:
        permissions["runtime"] = {"mode": "allowlist", "allow": runtimes}
    return SimpleNamespace(
        is_default=default, permissions=permissions, handle="scout", instructions="", packs=(), project_id=1
    )


def placement(features, runtimes) -> dict:
    """The place_* columns of ``worker_placement.columns``, of a worker in no pool with no labels and no taints."""
    names = ("pool", "labels", "taints", "accepted", "os", "arch", "doctor_at", "doctor_fresh", "findings")
    return {f"place_{name}": None for name in names} | {"place_runtimes": runtimes, "place_features": features}


def worker(features=OLD, runtimes=("claude-code", "codex")) -> SimpleNamespace:
    return SimpleNamespace(
        _mapping=placement(tuple(features), {name: UP for name in runtimes}),
        id=3,
        name="studio",
        labels=(),
        online=True,
        drained_at=None,
        agent_version="0.9.0",
        run_kinds=("chat", "author", "question"),
        dispatch_from="any",
        holding=0,
        slots=2,
        others=0,
        checkouts={"aud/app": {"path": "/src/app"}},
        features=tuple(features),
        plugins=None,
        runtimes={name: UP for name in runtimes},
    )


def test_a_daemon_without_agent_home_is_unfit_for_another_profiles_claude_code_run_and_says_why():
    old, scout = worker(), profile()
    assert lacks_agent_home(old, scout)  # any: Claude Code comes first
    why = unfit_worker(old, scout, "machine", "chat")
    assert why == (
        "runs evo-agents 0.9.0, which gives no Claude Code agent of @scout a config dir of its own (feature agent_home): "
        "update its daemon"
    )
    assert lacks_agent_home(old, scout, "claude-code") and not lacks_agent_home(old, scout, "codex")
    assert worker_problems(old, scout, "machine", "chat", "codex") == []


def test_the_default_profile_a_daemon_with_agent_home_or_another_first_runtime_is_fit():
    assert unfit_worker(worker(), profile(default=True), "machine", "chat") is None
    assert unfit_worker(worker(features=runs.WORKER_FEATURES), profile(), "machine", "chat") is None
    assert unfit_worker(worker(), profile(runtimes=["codex"]), "machine", "chat") is None
    assert unfit_worker(worker(runtimes=("codex",)), profile(), "machine", "question") is None
