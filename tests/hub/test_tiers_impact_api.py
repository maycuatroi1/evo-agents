"""A protected file a change reaches in the knowledge graph makes it tier 3 only when the diff breaks what that file
uses; an evolvable harness file that is protected merges at tier 0 once the Judge passed it and its replays won."""

from evo_agents.hub import api_surface, tiers

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
