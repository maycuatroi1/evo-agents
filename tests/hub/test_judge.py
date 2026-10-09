"""The Curator's Builder and Judge as the hub models them, without Postgres (``evo_agents.hub.judge``): the plan and the
branch an accepted proposal becomes, the detector of score hacking on a diff, who judges, the verdict, CI and the merge
gate.

The checks step 6 of the curator-agent plan names for the model: each sign of score hacking the plan lists (an
assertion removed, a skip or xfail added, a threshold loosened in a test, a plan's verify changed, a CI workflow or the
lint configuration changed, ``__eq__`` overloaded, ``sys.exit`` in a test, a protected path) is found, and a clean
change shows none; the Judge runs Codex only when the project's policy clears it, else Claude Code with a model other
than the Builder's; the merge goes only for a tier 0 change on GitHub that the Judge passed at the pull request's head,
with CI green, its repo's ruleset checked and tier 0 in the charter's auto_merge, and every other case stays open."""

from __future__ import annotations

import pytest

from evo_agents.hub import judge
from evo_agents.hub.access import ProjectRules

REPO = "evo-agents"
PROTECTED = ["curator.yaml", ".github/workflows/**", "evo-agents:evo_agents/hub/credentials.py"]
HEAD = "a" * 40


def diff_of(*files: str) -> list[judge.FileDiff]:
    return judge.parse_diff("\n".join(files) + "\n")


def file_diff(path: str, *lines: str, start: int = 1, old_path: str | None = None, mode: str = "") -> str:
    """The ``git diff`` of one file whose hunk is ``lines`` (each starting with +, - or a space)."""
    removed = sum(1 for line in lines if line.startswith("-") or line.startswith(" "))
    added = sum(1 for line in lines if line.startswith("+") or line.startswith(" "))
    head = [f"diff --git a/{old_path or path} b/{path}"]
    if mode:
        head.append(mode)
    head += [f"--- a/{old_path or path}", f"+++ b/{path}", f"@@ -{start},{removed} +{start},{added} @@"]
    return "\n".join(head + list(lines))


def kinds(signs: list[dict]) -> list[str]:
    return [sign["kind"] for sign in signs]


def signs_of(*files: str, verify=()) -> list[dict]:
    return judge.hack_signs(diff_of(*files), repo=REPO, protected=PROTECTED, verify_commands=list(verify))


# The plan and the branch


def test_an_accepted_proposal_becomes_a_curator_plan_on_a_branch_of_its_own():
    draft = {
        "id": "Wait helper!",
        "status": "active",
        "context": "why it matters",
        "repos": [{"repo": "evo-agents", "branch": "main"}],
        "steps": [
            {"id": 1, "what": "a helper", "verify": "pytest -q", "acceptance": ["no sleep"], "status": "done"},
            {"id": 2, "what": "docs", "evidence": "the Builder says so", "note": "x", "done_at": "2026-10-08"},
        ],
        "hub": {"project": "evo-agents", "revision": 3, "digest": "sha256:" + "0" * 64},
    }
    plan_id = judge.curator_plan_id(7, draft["id"])
    branch = judge.curator_branch(7, draft["id"])
    assert (plan_id, branch) == ("curator-7-wait-helper", "curator/7-wait-helper")
    assert judge.is_curator_branch(branch) and not judge.is_curator_branch("main")
    assert not judge.is_curator_branch("curator/") and not judge.is_curator_branch("curator/../main")
    assert not judge.is_curator_branch("feat/curator/x") and not judge.is_curator_branch(None)
    body = judge.curator_plan(draft, plan_id=plan_id, repo=REPO, branch=branch, proposal_id=7, project="evo-agents")
    assert body["id"] == plan_id and "hub" not in body and "status" not in body
    assert body["repos"] == [{"repo": REPO, "branch": branch}]  # never the draft's main
    assert body["context"].startswith("Made by the Curator of project evo-agents from proposal #7")
    assert body["context"].endswith("why it matters")
    assert [step["status"] for step in body["steps"]] == ["pending", "pending"]
    assert all(step["repo"] == REPO for step in body["steps"])
    assert "evidence" not in body["steps"][1] and "done_at" not in body["steps"][1] and "note" not in body["steps"][1]
    assert draft["steps"][0]["status"] == "done"  # the draft is not changed
    assert judge.verify_commands(body) == ["pytest -q"]
    assert judge.plan_checks(body) == {"1": ("pytest -q", '["no sleep"]'), "2": (None, "null")}
    long = judge.curator_plan_id(12345, "x" * 300)
    assert len(long) <= judge.MAX_PLAN_ID_CHARS and long.startswith("curator-12345-x")


def test_the_forge_of_an_origin():
    assert judge.forge_of("git@github.com:maycuatroi1/evo-agents.git") == "github"
    assert judge.forge_of("https://github.com/maycuatroi1/evo-agents") == "github"
    assert judge.forge_of("git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git") == "gitlab"
    assert judge.forge_of(None) is None and judge.forge_of("") is None and judge.forge_of("not a url") is None
    assert judge.gitlab_push_options("main", "A wait helper") == [
        "merge_request.create",
        "merge_request.target=main",
        "merge_request.remove_source_branch",
        "merge_request.title=A wait helper",
    ]


# Signs of score hacking


CLEAN = file_diff(
    "evo_agents/worker/wait.py",
    "+def wait_for(path):",
    "+    return path.exists()",
)
TEST_ADDED = file_diff(
    "tests/test_wait.py",
    "+def test_wait_for(tmp_path):",
    "+    assert wait_for(tmp_path)",
    mode="new file mode 100644",
)


def test_hack_a_clean_change_with_its_new_test_shows_no_sign():
    assert signs_of(CLEAN, TEST_ADDED, verify=["python -m pytest -q tests/test_wait.py"]) == []


def test_hack_an_assertion_removed_from_a_test_is_a_sign():
    found = signs_of(
        file_diff("tests/test_queue.py", " def test_claim():", "-    assert claim() == 1", "+    claim()", start=10)
    )
    assert kinds(found) == ["assert_removed"]
    assert found[0]["path"] == "tests/test_queue.py" and found[0]["line"] == 11
    assert found[0]["text"] == "assert claim() == 1"
    moved = file_diff("tests/test_queue.py", "-    assert claim() == 1", "+    assert  claim() == 1")
    assert signs_of(moved) == []  # the same assertion, written again


def test_hack_a_test_removed_is_a_sign():
    whole = file_diff(
        "tests/test_queue.py", "-def test_claim():", "-    assert claim()", mode="deleted file mode 100644"
    )
    assert kinds(signs_of(whole))[0] == "test_removed"
    one = file_diff("tests/test_queue.py", "-def test_flaky():", "-    pass")
    assert kinds(signs_of(one)) == ["test_removed"]


@pytest.mark.parametrize(
    "line",
    [
        "+@pytest.mark.skip(reason='flaky')",
        "+@pytest.mark.xfail",
        "+    pytest.skip('later')",
        "+@unittest.skip('later')",
        "+it.skip('claims', () => {})",
        "+test.only('the one', () => {})",
        "+xit('claims', () => {})",
    ],
)
def test_hack_a_skip_or_an_xfail_added_is_a_sign(line):
    assert kinds(signs_of(file_diff("tests/test_queue.py", line))) == ["skip_added"]


def test_hack_a_threshold_loosened_in_a_test_is_a_sign():
    found = signs_of(
        file_diff(
            "tests/test_speed.py",
            "-    assert elapsed < 0.1",
            "+    assert elapsed < 5.0",
            "-    assert value == pytest.approx(1.0, rel=1e-6)",
            "+    assert value == pytest.approx(1.0, rel=1e-1)",
        )
    )
    assert kinds(found) == ["threshold_changed", "threshold_changed"]
    assert found[0]["text"] == "assert elapsed < 0.1"


def test_hack_a_plans_verify_changed_is_a_sign():
    copy = file_diff("plans/active/curator-7-wait-helper.yaml", "-verify: pytest -q", "+verify: 'true'")
    assert "verify_changed" in kinds(signs_of(copy))
    script = file_diff("scripts/check.sh", "-pytest -q", "+exit 0")
    assert kinds(signs_of(script, verify=["bash scripts/check.sh"])) == ["verify_changed"]
    new_script = file_diff("scripts/check.sh", "+pytest -q", mode="new file mode 100755")
    assert signs_of(new_script, verify=["bash scripts/check.sh"]) == []  # a verify's file the change makes is fine


def test_hack_a_ci_workflow_or_the_lint_configuration_changed_is_a_sign():
    workflow = file_diff(".github/workflows/ci.yml", "-      - run: pytest", "+      - run: true")
    assert {"ci_changed", "protected_path"} <= set(kinds(signs_of(workflow)))
    assert kinds(signs_of(file_diff("ruff.toml", '-select = ["E", "F"]', '+select = ["E"]'))) == ["lint_config_changed"]
    assert kinds(signs_of(file_diff("tests/conftest.py", "+collect_ignore = ['test_queue.py']"))) == [
        "lint_config_changed"
    ]
    pyproject = file_diff("pyproject.toml", '-ignore = ["E501"]', '+ignore = ["E501", "F401"]')
    assert kinds(signs_of(pyproject)) == ["lint_config_changed"]
    deps = file_diff("pyproject.toml", '-    "httpx>=0.27",', '+    "httpx>=0.28",')
    assert signs_of(deps) == []  # a dependency is not lint configuration
    assert kinds(signs_of(file_diff("evo_agents/x.py", "+import os  # noqa: F401"))) == ["lint_suppressed"]


def test_hack_eq_overloaded_and_an_exit_in_a_test_are_signs():
    assert kinds(signs_of(file_diff("evo_agents/x.py", "+    def __eq__(self, other):", "+        return True"))) == [
        "eq_overridden"
    ]
    assert kinds(signs_of(file_diff("tests/test_x.py", "+    sys.exit(0)"))) == ["exit_in_test"]
    assert kinds(signs_of(file_diff("tests/test_x.py", "+    os._exit(0)"))) == ["exit_in_test"]
    assert signs_of(file_diff("evo_agents/cli.py", "+    sys.exit(main())")) == []  # outside a test it is the code's


def test_hack_a_protected_path_of_the_charter_is_a_sign_even_renamed_away():
    found = signs_of(
        file_diff("evo_agents/hub/credentials.py", "-GITHUB_PERMISSIONS = {}", "+GITHUB_PERMISSIONS = {1}")
    )
    assert kinds(found) == ["protected_path"] and "evo-agents:evo_agents/hub/credentials.py" in found[0]["text"]
    renamed = "\n".join(
        [
            "diff --git a/curator.yaml b/docs/curator.yaml",
            "similarity index 100%",
            "rename from curator.yaml",
            "rename to docs/curator.yaml",
        ]
    )
    assert kinds(signs_of(renamed)) == ["protected_path"]
    other_repo = judge.hack_signs(
        diff_of(file_diff("evo_agents/hub/credentials.py", "+x = 1")), repo="other", protected=PROTECTED
    )
    assert other_repo == []  # repo:glob protects that repo's path alone


def test_hack_a_diff_that_cannot_be_read_is_a_sign_but_an_image_is_not():
    binary = (
        "diff --git a/evo_agents/x.so b/evo_agents/x.so\nBinary files a/evo_agents/x.so and b/evo_agents/x.so differ"
    )
    assert kinds(signs_of(binary)) == ["diff_unreadable"]
    image = "diff --git a/docs/flow.png b/docs/flow.png\nBinary files a/docs/flow.png and b/docs/flow.png differ"
    assert signs_of(image) == []


def test_hack_the_files_of_a_github_pull_request_read_as_a_diff():
    files = judge.from_github_files(
        [
            {"filename": "tests/test_queue.py", "status": "modified", "patch": "@@ -4,2 +4,1 @@\n-    assert x\n y\n"},
            {"filename": "evo_agents/big.py", "status": "modified", "changes": 9000},
            {"filename": "docs/new.md", "status": "renamed", "previous_filename": "curator.yaml", "changes": 0},
        ]
    )
    found = judge.hack_signs(files, repo=REPO, protected=PROTECTED)
    assert kinds(found) == ["assert_removed", "diff_unreadable", "protected_path"]
    assert found[0]["line"] == 4
    reason = judge.raised_reason(found)
    assert reason.startswith("the diff of the Builder shows signs of score hacking") and reason.endswith("tier 3")
    assert (
        len(
            judge.hack_signs(
                diff_of(*[file_diff(f"tests/test_{n}.py", "-assert x") for n in range(80)]), repo=REPO, protected=[]
            )
        )
        == judge.MAX_SIGNS
    )


# Who judges


def rules(sinks: list[dict], levels=("public", "internal", "customer", "secret")) -> ProjectRules:
    return ProjectRules("evo-agents", list(levels), ["any", "domestic-only"], sinks)


HUB = {"id": "evo-hub", "kind": "hub", "clearance": {"level": "internal"}}
CLAUDE = {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "customer"}}
CODEX = {"id": "codex@openai", "kind": "agent-session", "clearance": {"level": "internal"}}
INTERNAL = {"level": "internal", "projects": ["evo-agents"]}
CUSTOMER = {"level": "customer", "location": "domestic-only", "projects": ["evo-agents"]}


def test_judge_runs_codex_only_when_the_policy_clears_codex_for_the_projects_label():
    assert judge.codex_cleared(rules([HUB, CLAUDE, CODEX]), INTERNAL)
    assert not judge.codex_cleared(rules([HUB, CLAUDE]), INTERNAL)  # meridai: no sink for Codex
    assert not judge.codex_cleared(rules([HUB, CLAUDE, CODEX]), CUSTOMER)  # a label above the sink's clearance
    unknown = {**CODEX, "clearance": {"level": "galactic"}}
    assert not judge.codex_cleared(rules([HUB, unknown]), INTERNAL)  # a clearance the ladder lacks admits nothing
    assert not judge.codex_cleared(rules([HUB, CODEX]), {"level": "internal", "projects": ["other"]})

    builder = {"runtime": "claude-code", "model": None}
    both = {"claude-code", "codex"}
    assert judge.judge_runtime({"runtime": "claude-code"}, builder, codex_allowed=True, worker_runtimes=both)[:2] == (
        "codex",
        None,
    )
    asked = {"runtime": "codex", "model": "gpt-5-codex"}
    assert judge.judge_runtime(asked, builder, codex_allowed=True, worker_runtimes=both)[:2] == ("codex", "gpt-5-codex")
    runtime, model, why = judge.judge_runtime(asked, builder, codex_allowed=False, worker_runtimes=both)
    assert (runtime, model) == ("claude-code", "sonnet") and "declares no sink for Codex" in why
    no_codex = judge.judge_runtime(asked, builder, codex_allowed=True, worker_runtimes={"claude-code"})
    assert no_codex[:2] == ("claude-code", "sonnet") and "has no Codex" in no_codex[2]


def test_judge_on_claude_code_never_takes_the_builders_model():
    sonnet_builder = {"runtime": "claude-code", "model": "sonnet"}
    found = judge.judge_runtime(
        {"runtime": "claude-code", "model": "sonnet"},
        sonnet_builder,
        codex_allowed=False,
        worker_runtimes={"claude-code"},
    )
    assert found[:2] == ("claude-code", "opus")
    opus_builder = {"runtime": "claude-code", "model": "claude-opus-5-5"}
    assert judge.judge_runtime(
        {"runtime": "claude-code"}, opus_builder, codex_allowed=False, worker_runtimes={"claude-code"}
    )[:2] == ("claude-code", "sonnet")
    chosen = judge.judge_runtime(
        {"runtime": "claude-code", "model": "haiku"}, opus_builder, codex_allowed=False, worker_runtimes={"claude-code"}
    )
    assert chosen[:2] == ("claude-code", "haiku")
    codex_builder = {"runtime": "codex", "model": None}
    assert judge.judge_runtime(
        {"runtime": "codex"}, codex_builder, codex_allowed=False, worker_runtimes={"claude-code"}
    )[:2] == ("claude-code", None)


# The verdict


VERIFY = [{"command": "pytest -q", "exit_code": 0}]
HIDDEN = [{"index": 1, "exit_code": 0}]


def verdict(**changes):
    given = {
        "agent": "pass",
        "verify": VERIFY,
        "expected_verify": ["pytest -q"],
        "hidden": HIDDEN,
        "hidden_count": 1,
        "signs": [],
        "head_matches": True,
        **changes,
    }
    return judge.final_verdict(**given)


def test_judge_passes_a_change_only_when_everything_passed():
    assert verdict() == (True, [])
    for changes, why in (
        ({"agent": "fail"}, "the Judge did not pass it"),
        ({"agent": None}, "the Judge gave no verdict"),
        ({"verify": []}, "1 verify command(s) of the plan did not run"),
        ({"verify": [{"command": "pytest -q", "exit_code": 1}]}, "exited other than 0"),
        ({"hidden": []}, "1 hidden check(s) of the project did not run"),
        ({"hidden": [{"index": 1, "exit_code": 2}]}, "1 hidden check(s) of the project failed"),
        ({"signs": [{"kind": "skip_added"}]}, "signs of score hacking: skip_added"),
        ({"head_matches": False}, "another commit than the head"),
    ):
        passed, reasons = verdict(**changes)
        assert not passed and any(why in reason for reason in reasons), (changes, reasons)


def test_judge_reads_its_agents_verdict_file():
    assert judge.read_verdict('{"verdict": "pass", "reasons": " looked fine "}') == ("pass", "looked fine")
    assert judge.read_verdict('{"verdict": "maybe"}')[0] is None
    assert judge.read_verdict("not json")[0] is None
    conclusion, title, summary = judge.check_run_output(
        False,
        ["the Judge did not pass it"],
        VERIFY,
        [{"index": 1, "exit_code": 3}],
        [{"kind": "skip_added", "path": "tests/x.py", "line": 2}],
    )
    assert conclusion == "failure" and title.startswith("The Judge failed it")
    assert "`pytest -q` exited 0" in summary and "0 of 1 passed" in summary and "tests/x.py:2" in summary
    prompt = judge.build_judge_prompt(
        "evo-agents",
        {"id": 7, "title": "A wait helper", "kind": "fix", "tier": 0, "summary": "Why"},
        {
            "goal": "No sleep.",
            "steps": [
                {
                    "id": 1,
                    "title": "Helper",
                    "what": "w",
                    "verify": "pytest -q",
                    "acceptance": ["a"],
                    "evidence": "the Builder's own words",
                }
            ],
        },
        REPO,
        branch="curator/7-wait-helper",
        base="origin/main",
        head=HEAD,
        pr_url="https://github.com/o/r/pull/3",
    )
    assert "the Builder's own words" not in prompt  # never what the Builder wrote
    assert "git diff origin/main...aaaaaaaaaaaa" in prompt and judge.VERDICT_RULE in prompt
    results = judge.results_text(VERIFY, [{"index": 1, "exit_code": 0}], [])
    assert "hidden check 1 exited 0" in results


# CI and the merge


GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
NO_STATUS = {"state": "pending", "total_count": 0, "statuses": []}


def test_merge_reads_ci_from_check_runs_and_statuses():
    assert judge.ci_state(GREEN, NO_STATUS) == ("green", [])
    assert judge.ci_state([], NO_STATUS)[0] == "none"
    assert (
        judge.ci_state([{"name": judge.JUDGE_CHECK_NAME, "status": "completed", "conclusion": "success"}], NO_STATUS)[0]
        == "none"
    )  # the Judge's own check run is not CI
    assert judge.ci_state([{"name": "test", "status": "in_progress"}], NO_STATUS)[0] == "pending"
    assert judge.ci_state([{"name": "test", "status": "completed", "conclusion": "failure"}], NO_STATUS)[0] == "red"
    assert judge.ci_state(GREEN, {"state": "failure", "total_count": 1})[0] == "red"
    assert judge.ci_state(GREEN, {"state": "pending", "total_count": 1})[0] == "pending"
    assert judge.ci_state(GREEN, None)[0] == "unreadable"


def facts(**changes) -> judge.MergeFacts:
    given = {
        "forge": "github",
        "tier": 0,
        "auto_merge": (0,),
        "repo_checked": True,
        "passed": True,
        "judged_sha": HEAD,
        "pr_state": "open",
        "pr_merged": False,
        "pr_head": HEAD,
        "pr_base": "main",
        "default_branch": "main",
        "signs": (),
        "ci": "green",
        "mergeable": True,
        **changes,
    }
    return judge.MergeFacts(**given)


def test_merge_goes_only_for_a_tier_0_change_passed_at_its_head_with_ci_green():
    assert judge.merge_decision(facts()) == ("merge", [])


@pytest.mark.parametrize(
    "changes, why",
    [
        ({"forge": "gitlab"}, "a merge request on GitLab stays open"),
        ({"tier": 1}, "tier 1 waits for its owner"),
        ({"tier": 3}, "tier 3 waits for its owner"),
        ({"auto_merge": ()}, "auto_merge does not name tier 0"),
        ({"repo_checked": False}, "has not checked that the repo's ruleset"),
        ({"passed": False}, "the Judge did not pass it"),
        ({"pr_head": "b" * 40}, "not the commit the Judge passed"),
        ({"judged_sha": None}, "not the commit the Judge passed"),
        ({"pr_base": "release"}, "not into the default branch main"),
        ({"signs": ({"kind": "protected_path"},)}, "protected paths: protected_path"),
        ({"ci": "red", "ci_reasons": ("test ended failure",)}, "test ended failure"),
        ({"ci": "none", "ci_reasons": ("no CI ran on the commit",)}, "no CI ran"),
        ({"ci": "unreadable"}, "CI is unreadable"),
        ({"pr_state": "closed"}, "the pull request is closed"),
        ({"pr_merged": True}, "merged already"),
        ({"mergeable": False}, "conflicts"),
    ],
)
def test_merge_refusals_leave_the_pull_request_open(changes, why):
    decided, reasons = judge.merge_decision(facts(**changes))
    assert decided == "open" and any(why in reason for reason in reasons), reasons


def test_merge_waits_while_ci_runs():
    assert judge.merge_decision(facts(ci="pending", ci_reasons=("test is queued",))) == ("wait", ["test is queued"])


# The gaps the security review of step 6 found in the detector (H2), each a sign now, file by file


def test_hack_an_assertion_moved_to_another_file_is_removed_from_its_own():
    moved = signs_of(
        file_diff("tests/test_queue.py", " def test_claim():", "-    assert claim() == 1", "+    claim()"),
        file_diff("scripts/never_run.py", "+    assert claim() == 1", mode="new file mode 100644"),
    )
    assert kinds(moved) == ["assert_removed"]  # never pooled across files
    unguarded = file_diff(
        "tests/test_queue.py", "+    if False:", "-    assert claim() == 1", "+        assert claim() == 1"
    )
    found = signs_of(unguarded)
    assert kinds(found) == ["assert_removed"] and "a guard that never runs" in found[0]["text"]
    assert kinds(signs_of(file_diff("web/e2e/run.spec.ts", "+  if (false) {"))) == ["assert_removed"]
    assert signs_of(file_diff("tests/test_queue.py", "+if TYPE_CHECKING:", "+    from queue import Queue")) == []


def test_hack_an_expected_value_changed_off_the_assertions_line_is_a_sign():
    found = signs_of(
        file_diff("tests/test_queue.py", "-    expected = 3", "+    expected = 4", "     assert claim() == expected")
    )
    assert kinds(found) == ["expected_changed"]
    words = signs_of(file_diff("tests/test_brief.py", '-    want = "two runs"', '+    want = "no run"'))
    assert kinds(words) == ["expected_changed"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/__snapshots__/test_brief.ambr",
        "web/src/__snapshots__/card.test.tsx.snap",
        "tests/hub/golden/cli-contract.json",
        "tests/data/expected.json",
        "pkg/testdata/out.txt",
    ],
)
def test_hack_a_golden_or_snapshot_file_changed_is_a_sign(path):
    assert "golden_changed" in kinds(signs_of(file_diff(path, '-  "a": 1', '+  "a": 2')))


def test_hack_a_new_snapshot_of_a_new_test_is_no_sign():
    assert signs_of(file_diff("tests/__snapshots__/test_new.ambr", "+x", mode="new file mode 100644")) == []


@pytest.mark.parametrize(
    "line",
    [
        "+        self.skipTest('later')",
        "+    raise unittest.SkipTest('later')",
        "+from pytest import mark",
        "+@mark.skip",
        "+@pt.mark.xfail(strict=False)",
        "+from pytest import skip as later",
        "+    pt.skip('not today')",
        "+__test__ = False",
    ],
)
def test_hack_a_skip_aliased_or_imported_on_its_own_is_a_sign(line):
    assert "skip_added" in kinds(signs_of(file_diff("tests/test_queue.py", line)))


@pytest.mark.parametrize(
    "line",
    [
        '+    getattr(os, "_exit")(0)',
        "+    __import__('os')._exit(0)",
        "+    os.kill(os.getpid(), 9)",
        "+    pytest.exit('done', returncode=0)",
    ],
)
def test_hack_an_exit_through_another_door_is_a_sign(line):
    assert kinds(signs_of(file_diff("tests/test_queue.py", line))) == ["exit_in_test"]


@pytest.mark.parametrize(
    "line",
    [
        "+    except AssertionError:",
        "+    except (AssertionError, ValueError):",
        "+    except:",
        "+    except BaseException:",
        "+    with contextlib.suppress(AssertionError):",
        "+  } catch (e) {}",
    ],
)
def test_hack_an_assertions_failure_caught_is_a_sign(line):
    assert kinds(signs_of(file_diff("tests/test_queue.py", line))) == ["assert_caught"]


@pytest.mark.parametrize(
    "path, line",
    [
        ("pyproject.toml", '+norecursedirs = ["tests/slow"]'),
        ("pyproject.toml", "+[[tool.mypy.overrides]]"),
        ("pyproject.toml", "+ignore_errors = true"),
        ("setup.cfg", "+[mypy-evo_agents.*]"),
        ("web/tsconfig.json", '-    "strict": true,'),
        ("web/tsconfig.app.json", '+    "noImplicitAny": false,'),
        ("web/package.json", '+  "jest": {"testPathIgnorePatterns": ["e2e"]},'),
    ],
)
def test_hack_configuration_of_tests_and_types_is_a_sign(path, line):
    assert "lint_config_changed" in kinds(signs_of(file_diff(path, line)))


@pytest.mark.parametrize(
    "path, verify",
    [
        ("Makefile", "make check"),
        ("justfile", "cd web && just test"),
        ("noxfile.py", "uv run nox -s tests"),
        ("noxfile.py", "python -m nox"),
        ("tasks.py", "invoke test"),
    ],
)
def test_hack_the_file_of_a_runner_the_verify_calls_is_a_sign(path, verify):
    changed = file_diff(path, "-\tpytest -q", "+\ttrue")
    assert kinds(signs_of(changed, verify=[verify])) == ["verify_changed"]
    assert signs_of(changed, verify=["python -m pytest -q"]) == []  # a runner the verify does not call


def test_hack_a_package_script_the_verify_runs_is_a_sign():
    script = file_diff("web/package.json", '-    "check:all": "vitest run",', '+    "check:all": "true",')
    assert "verify_changed" in kinds(signs_of(script, verify=["cd web && pnpm check:all"]))
    other = file_diff("web/package.json", '-    "dev": "vite",', '+    "dev": "vite --host",')
    assert signs_of(other, verify=["cd web && pnpm check:all"]) == []


def test_hack_an_unknown_or_unreadable_file_where_tests_or_ci_live_fails_closed():
    image = "diff --git a/tests/golden/plot.png b/tests/golden/plot.png\nBinary files a/x and b/y differ"
    assert {"golden_changed", "diff_unreadable"} <= set(kinds(signs_of(image)))
    blob = "diff --git a/tests/data/cases.bin b/tests/data/cases.bin\nBinary files a/x and b/y differ"
    assert "diff_unreadable" in kinds(signs_of(blob))
    workflow = "diff --git a/.github/workflows/logo.png b/.github/workflows/logo.png\nBinary files a/x and b/y differ"
    assert "diff_unreadable" in kinds(signs_of(workflow))
    docs = "diff --git a/docs/flow.png b/docs/flow.png\nBinary files a/docs/flow.png and b/docs/flow.png differ"
    assert signs_of(docs) == []  # an image of the docs is still fine


def test_hack_the_runners_a_verify_command_calls():
    globs, scripts = judge.verify_runners(
        ["cd web && pnpm test", "bash -c 'make lint && just check'", "FOO=1 uv run nox -s unit", "npm run e2e:ci"]
    )
    assert {"Makefile", "justfile", "noxfile.py"} <= globs and scripts == {"test", "e2e:ci"}


# The verdict in the agent's last message, and the plan the hub's


def test_judge_reads_the_verdict_that_ends_its_agents_last_message():
    message = 'I ran the tests and read the diff.\n\n```json\n{"verdict": "fail", "reasons": "a {brace} too far"}\n```'
    assert judge.verdict_from_message(message) == ("fail", "a {brace} too far")
    assert judge.verdict_from_message('{"verdict": "pass"}') == ("pass", "")
    assert judge.verdict_from_message('{"verdict": "pass"} and then more words')[0] is None
    assert judge.verdict_from_message("I think it passes.")[0] is None
    assert judge.verdict_from_message(None)[0] is None
    assert judge.verdict_from_message('{"reasons": "no verdict"}')[0] is None


def test_curator_plan_progress_is_all_a_write_may_change():
    body = {
        "id": "curator-7-x",
        "status": "active",
        "goal": "g",
        "steps": [{"id": 1, "what": "w", "verify": "v", "status": "pending"}],
    }
    done = {**body, "status": "done", "steps": [{**body["steps"][0], "status": "done", "evidence": "e", "note": "n"}]}
    assert judge.progress_free(done) == judge.progress_free(body)
    for changed in ({"goal": "other"}, {"context": "c"}, {"steps": [{**body["steps"][0], "what": "x"}]}):
        assert judge.progress_free({**body, **changed}) != judge.progress_free(body)
    assert judge.is_curator_plan_id("curator-7-x") and not judge.is_curator_plan_id("my-curator")


# CI the ruleset requires


REQUIRED = [{"context": "test", "integration_id": None}]


def test_merge_ci_is_green_only_once_the_required_checks_passed():
    assert judge.ci_state(GREEN, NO_STATUS, required=REQUIRED) == ("green", [])
    lint = [{"name": "lint", "status": "completed", "conclusion": "success"}]
    assert judge.ci_state(lint, NO_STATUS, required=REQUIRED) == (
        "pending",
        ["the required check test has not reported"],
    )
    skipped = [{"name": "test", "status": "completed", "conclusion": "skipped"}]
    assert judge.ci_state(skipped, NO_STATUS, required=REQUIRED)[0] == "red"
    assert judge.ci_state(GREEN, NO_STATUS, required=[])[0] == "red"  # a ruleset that requires nothing
    from_app = [{"context": "test", "integration_id": 15368}]
    assert judge.ci_state([{**GREEN[0], "app": {"id": 1}}], NO_STATUS, required=from_app)[0] == "red"
    assert judge.ci_state([{**GREEN[0], "app": {"id": 15368}}], NO_STATUS, required=from_app)[0] == "green"
    status = {"state": "success", "total_count": 1, "statuses": [{"context": "test", "state": "success"}]}
    assert judge.ci_state([], status, required=REQUIRED)[0] == "green"
    assert judge.ci_state(GREEN, NO_STATUS, required=REQUIRED, head_message="wip [skip ci]")[0] == "red"
    assert judge.skip_ci_marker("Fix\n\nskip-checks: true") == "skip-checks: true"
    assert judge.skip_ci_marker("Fix the skip ci docs") is None
    rules = [
        {"type": "update", "ruleset_id": 1},
        {"type": "required_status_checks", "ruleset_id": 1, "parameters": {"required_status_checks": from_app}},
    ]
    assert judge.required_checks(rules) == from_app
