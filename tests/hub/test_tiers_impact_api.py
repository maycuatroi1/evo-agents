"""A protected file a change reaches in the knowledge graph makes it tier 3 only when the diff breaks what that file
uses; an evolvable harness file that is protected merges at tier 0 once the Judge passed it and its replays won. The
model first, then the hub reading a pull request for it (``evo_agents.hub.server.change_tiers``), whose world, GitHub
and helpers are ``tests.hub.test_curator_changes``' and ``tests.hub.test_replay_merge``'."""

import pytest

from evo_agents.hub import api_surface, tiers
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    from sqlalchemy import select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server import change_tiers, changes
    from tests.hub.live import sql
    from tests.hub.test_change_tiers import MERGE_BASE, project_id
    from tests.hub.test_credentials_api import MINE, app_key  # noqa: F401 (a fixture)
    from tests.hub.test_curator import written
    from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
        GREEN,
        HEAD,
        SUCCESS,
        accepted,
        builder,
        calls,
        curator_charter,
        judged,
        moved_on,
        one_change,
        proposal_of,
        world,
    )
    from tests.hub.test_replay_merge import SKILL, SKILL_FILES, SKILL_PATHS, replayed, suite, won

PROTECTED = ["evo_agents/hub/tiers.py", "skills/curator/**"]
TARGET = ("evo-agents", "evo_agents/hub/tiers.py")
EDIT = [("evo-agents", "evo_agents/hub/replay.py")]

BEFORE = "import re\nSPLITS = ('a',)\n\ndef harness_path(path):\n    return True\n\ndef skill_dir(path):\n    pass\n"
AFTER_BODY = (
    "import re\nSPLITS = ('a',)\n\ndef harness_path(path):\n    return False\n\ndef skill_dir(path):\n    pass\n"
)
AFTER_API = "import re\nSPLITS = ('a',)\n\ndef harness_path(path, strict):\n    return True\n"
USER = "from evo_agents.hub import replay\n\ndef f(p):\n    return replay.harness_path(p)\n"


def test_the_api_a_diff_breaks_is_what_it_removes_or_resigns_and_the_protected_file_uses():
    assert api_surface.changed_api(BEFORE, AFTER_BODY) == []
    assert api_surface.changed_api(BEFORE, AFTER_API) == ["harness_path", "skill_dir"]
    assert api_surface.used_names(USER, "evo_agents.hub.replay") == {"harness_path"}
    assert api_surface.used_names("from evo_agents.hub.replay import skill_dir as s\n", "evo_agents.hub.replay") == {
        "skill_dir"
    }
    changes = {"evo_agents/hub/replay.py": (BEFORE, AFTER_API)}
    assert api_surface.reached_api(changes, USER) == ["evo_agents.hub.replay.harness_path"]
    assert api_surface.reached_api({"evo_agents/hub/replay.py": (BEFORE, AFTER_BODY)}, USER) == []
    assert api_surface.module_of("src/pkg/__init__.py") == "pkg" and api_surface.module_of("README.md") is None


def test_a_protected_file_reached_is_tier_2_before_the_diff_and_tier_3_only_when_its_api_breaks():
    before = tiers.tier_of("fix", EDIT, PROTECTED, [TARGET])
    assert before.tier == 2
    harmless = tiers.tier_of("fix", EDIT, PROTECTED, [TARGET], api={TARGET: []})
    assert harmless.tier == 1 and "breaks no module-level name" in harmless.reasons[-1]
    broken = tiers.tier_of("fix", EDIT, PROTECTED, [TARGET], api={TARGET: ["evo_agents.hub.replay.harness_path"]})
    assert broken.tier == 3 and not broken.evolvable


def test_a_protected_file_edited_stays_tier_3():
    found = tiers.tier_of("fix", [TARGET], PROTECTED, api={})
    assert found.tier == 3 and not found.evolvable
    assert tiers.replay_tier("fix", found, harness="x", won="won", judged=True).tier == 3


def test_an_evolvable_harness_file_merges_at_tier_0_when_the_judge_passed_and_the_replays_won():
    paths = [("evo-agents", "skills/curator/SKILL.md")]
    found = tiers.tier_of("skill", paths, PROTECTED, evolvable=["skills/curator/**"])
    assert found.tier == 3 and found.evolvable
    harness = "the files of skill skills/curator"
    assert tiers.replay_tier("skill", found, harness=harness, won="b passed more", judged=True).tier == 0
    assert tiers.replay_tier("skill", found, harness=harness, won="b passed more", judged=False).tier == 3
    assert tiers.replay_tier("skill", found, harness=harness, won=None, judged=True).tier == 3
    assert tiers.replay_tier("skill", found, harness=None, won="b passed more", judged=True).tier == 3
    assert tiers.replay_tier("skill", found, harness=harness, won="yes", judged=True, fix=True).tier == 3
    not_listed = tiers.tier_of("skill", paths, PROTECTED)
    assert not not_listed.evolvable
    mixed = tiers.tier_of("skill", [*paths, ("evo-agents", "ruff.toml")], PROTECTED, evolvable=["skills/**"])
    assert mixed.tier == 3 and not mixed.evolvable


# The hub reads the pull request for the rule

REPLAY_PY = "evo_agents/hub/replay.py"
CONFTEST = ("evo-agents", "tests/conftest.py")  # one of HARD_PATHS, and Python
CODE_FILES = [{"filename": REPLAY_PY, "status": "modified", "patch": "@@ -4,1 +4,1 @@\n-    return True\n+    pass\n"}]


def reaching(monkeypatch, reached) -> None:
    async def impact(engine, project_id, targets, reader, **_):
        return change_tiers.GraphView(frozenset(), frozenset(reached))

    monkeypatch.setattr(change_tiers, "impact_as_owner", impact)


@needs_pg
@pytest.mark.parametrize(
    "after, user, tier, why",
    [
        (AFTER_API, USER, 3, "and the diff removes or changes the signature of evo_agents.hub.replay.harness_path"),
        (AFTER_BODY, USER, 1, None),  # a body changed alone breaks nothing: no raise
        (AFTER_API, "from evo_agents.hub.replay import SPLITS\n", 1, None),  # a name the diff keeps
        (AFTER_API, None, 3, "and the hub could not read it or the diff's Python files"),  # gone at the head
    ],
    ids=["breaks", "body", "unused", "unread"],
)
def test_a_pull_request_reaching_a_protected_python_file_is_tier_3_only_when_its_diff_breaks_what_that_uses(
    world, monkeypatch, after, user, tier, why
):
    proposal = accepted(world, kind="fix", paths=[{"repo": "evo-agents", "path": REPLAY_PY}])
    assert proposal["tier"] == 1
    reaching(monkeypatch, {CONFTEST})
    world.github.put_file(MINE, "evo-agents", MERGE_BASE, REPLAY_PY, BEFORE)
    world.github.put_file(MINE, "evo-agents", HEAD, REPLAY_PY, after)
    world.github.put_file(MINE, "evo-agents", HEAD, CONFTEST[1], user)
    builder(world, CODE_FILES)
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == tier and proposal_of(world, proposal["id"])["tier"] == tier
    reasons = proposal_of(world, proposal["id"])["tier_reasons"]
    if why is None:
        assert len(reasons) == len(proposal["tier_reasons"]), reasons
    else:
        assert any(why in reason for reason in reasons), reasons
    read = sorted(
        (item.path.rpartition("/contents/")[2], item.query["ref"]) for item in calls(world.github, "GET", ".py")
    )
    expected = [(CONFTEST[1], HEAD)]
    if user is not None:  # the diff's files whose module the protected one imports, at both ends
        expected += [(REPLAY_PY, MERGE_BASE), (REPLAY_PY, HEAD)]
    assert read == sorted(expected)


@needs_pg
def test_a_pull_request_reaching_a_protected_file_of_no_python_or_editing_none_reads_no_file(world, monkeypatch):
    proposal = accepted(world, kind="fix", paths=[{"repo": "evo-agents", "path": REPLAY_PY}])
    reaching(monkeypatch, {("evo-agents", "curator.yaml"), ("other-repo", "evo_agents/hub/tiers.py")})
    builder(world, [{"filename": "docs/x.md", "status": "modified", "patch": "@@ -1,1 +1,1 @@\n-a\n+b\n"}])
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == 1 and proposal_of(world, proposal["id"])["tier"] == 1
    assert calls(world.github, "GET", ".py") == [] and calls(world.github, "GET", "curator.yaml") == []


def evolving(world, listed: bool) -> None:
    """The charter plans tier 3 too and protects skills/demo, which its ``evolve.evolvable_paths`` lists or not."""
    written(
        world.client,
        world.headers["owner"],
        curator_charter(planned_tiers=[0, 1, 2, 3], protected_paths=["curator.yaml", "skills/demo/**"]),
    )
    c = tables.charters
    (body,) = sql(world.db, select(c.c.body).where(c.c.project_id == project_id(world)).order_by(c.c.revision.desc()))[
        0
    ]
    evolve = {"evolvable_paths": ["skills/demo/**"] if listed else []}
    sql(world.db, update(c).where(c.c.project_id == project_id(world)).values(body={**body, "evolve": evolve}))


@needs_pg
@pytest.mark.parametrize("listed", [True, False], ids=["evolvable", "not_listed"])
def test_a_protected_harness_file_the_charter_lists_evolvable_merges_at_tier_0_once_judged_with_replays_won(
    world, monkeypatch, listed
):
    if not listed:  # the sign of a protected path is a gate of its own; an evolvable harness file raises none
        monkeypatch.setattr(changes, "_signs_of", lambda *args: [])
    tasks = suite(world)
    evolving(world, listed)
    proposal = accepted(world, kind="skill", paths=SKILL_PATHS)
    assert proposal["tier"] == 3
    builder(world, SKILL_FILES)
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == 3  # not judged yet
    replayed(world, tasks[:6], won())
    change = judged(world, paths=[SKILL])
    assert change["passed"] is True and change["tier"] == 3  # lowered at the merge, once its pass is kept
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    found = moved_on(world)
    change = one_change(world)
    if listed:
        assert found == {"checked": 1, "merged": 1} and (change["state"], change["tier"]) == ("merged", 0)
    else:
        assert change["state"] == "open" and change["tier"] == 3
        assert "tier 3 waits for its owner to merge it" in change["reason"]
    assert proposal_of(world, proposal["id"])["tier"] == 3  # the proposal keeps the tier its rules gave
