"""The worker's hook of autonomy (plan autonomy-levels, criteria a8 and a9): the table of patterns that reads a Bash
command into its classes (``evo_agents.worker.command_classes``), one case for each pattern; the gate that acts on the
mode of each class for the agent (deny refuses naming class, mode and source; ask refuses and names ``evo-agents worker
ask --category``; notify lets through, records an event and asks the hub to tell the principal; auto lets through and
records); the floor S2, a command of K9 only after a backup made within 24 hours, for every principal, A5 included;
and the PreToolUse hook every claude-code run with a snapshot gets, beside the profile's.

The hook is a second layer: a command wrapped in base64, kept in a script or run through ``eval`` gets past it, as the
last tests show and ``docs/autonomy-levels.md`` says.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_agents.hub.autonomy_levels import CLASS_NAMES, CLASSES
from evo_agents.worker import autonomy_hook
from evo_agents.worker.autonomy_hook import Gate, add_backup, fresh_backup
from evo_agents.worker.command_classes import classify, simple_commands

WORKTREE = Path("/work/evo-run/7")
OWN = ("acme/app",)


def classes(command: str) -> list[str]:
    return [hit.klass for hit in classify(command, WORKTREE, own_repos=OWN, default_branches=("trunk",))]


# The table of patterns: one case each


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # K9: a forced push, the deletion of a remote branch
        ("git push --force origin feat/x", ["K9"]),
        ("git push -f origin feat/x", ["K9"]),
        ("git push --force-with-lease origin feat/x", ["K9"]),
        ("git push origin +feat/x", ["K9"]),
        ("git push origin --delete feat/old", ["K9"]),
        ("git push origin :feat/old", ["K9"]),
        ("git -C app push -fu origin feat/x", ["K9"]),
        # K9: DROP, TRUNCATE, DELETE without WHERE through an SQL client
        ('psql "$DATABASE_URL" -c "DROP TABLE users"', ["K9"]),
        ("mysql -e 'truncate table orders' shop", ["K9"]),
        ('psql -c "delete from sessions"', ["K9"]),
        ("sqlite3 app.db 'DELETE FROM jobs;'", ["K9"]),
        ("psql <<'SQL'\ndrop schema audit cascade;\nSQL", ["K9"]),
        # K9: rm -rf outside the worktree
        ("rm -rf /var/lib/postgres", ["K9"]),
        ("rm -fr ../other-run", ["K9"]),
        ("sudo rm -r -f /srv/data", ["K9"]),
        ("rm --recursive --force ~/backups", ["K9"]),
        ('rm -rf "$DATA_DIR"', ["K9"]),
        # K9: terraform destroy, kubectl delete, the recursive deletion of a bucket
        ("terraform destroy -auto-approve", ["K9"]),
        ("terraform apply -destroy", ["K9"]),
        ("kubectl delete namespace prod", ["K9"]),
        ("aws s3 rm s3://acme-data --recursive", ["K9"]),
        ("aws s3 rb s3://acme-data --force", ["K9"]),
        ("gsutil -m rm -r gs://acme-data", ["K9"]),
        ("gcloud storage rm --recursive gs://acme-data", ["K9"]),
        ("rclone purge remote:acme", ["K9"]),
        # K5: gh pr merge, glab mr merge, a push to a default branch
        ("gh pr merge 12 --squash", ["K5"]),
        ("glab mr merge 4", ["K5"]),
        ("git push origin main", ["K5"]),
        ("git push origin HEAD:refs/heads/master", ["K5"]),
        ("git push origin feat/x:trunk", ["K5"]),
        # K6: a push of a tag v*, gh release create
        ("git push origin v1.2.0", ["K6"]),
        ("git push origin refs/tags/v2.0.0", ["K6"]),
        ("git push --tags origin", ["K6"]),
        ("gh release create v1.2.0 --notes x", ["K6"]),
        # K8: writes through evo-agents hub secret|account
        ("evo-agents hub secret set GITHUB_TOKEN --value-stdin", ["K8"]),
        ("evo-agents hub account add claude --name work", ["K8"]),
        # K10: an issue or a pull request outside the run's repos
        ("gh issue create --repo other/lib --title Bug --body x", ["K10"]),
        ("gh pr create -R https://github.com/other/lib --fill", ["K10"]),
        # K12: writes through evo-agents hub admin|policy|agent|org|autonomy
        ("evo-agents hub admin grant lena --role admin", ["K12"]),
        ("evo-agents hub policy set demo --mode auto", ["K12"]),
        ("evo-agents hub agent edit reviewer --allow tool=all", ["K12"]),
        ("python -m evo_agents hub org seat add ivan", ["K12"]),
        ("evo-agents hub autonomy pref --level A5", ["K12"]),
        # several classes, each once, in the order the command reaches them
        ("git push --force origin main && gh release create v3", ["K9", "K5", "K6"]),
        # bash -c: the script is read too
        ("bash -c 'git push --force origin feat/x'", ["K9"]),
        ('FOO=1 env BAR=2 sh -lc "kubectl delete pod web-1"', ["K9"]),
    ],
)
def test_autonomy_level_classifies_each_pattern(command, expected):
    assert classes(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feat/x",
        "git push -u origin feat/x",
        "git push origin HEAD:feat/x",
        "rm -rf build node_modules",
        f"rm -rf {WORKTREE}/app/dist",
        "rm -rf /tmp/evo-scratch",
        "rm -r docs/old",
        'psql -c "delete from sessions where expires_at < now()"',
        'psql -c "select * from users"',
        "echo 'drop table users' > notes.md",
        "evo-agents hub secret list",
        "evo-agents hub autonomy show",
        "evo-agents worker ask --category delete_data --question 'Drop?' --option a=Yes --option b=No",
        "gh pr create --fill",
        "gh issue create --repo acme/app --title Bug --body x",
        "gh pr view 12",
        "kubectl get pods",
        "terraform plan",
        "aws s3 ls s3://acme-data",
    ],
)
def test_autonomy_level_leaves_commands_of_no_class_alone(command):
    assert classes(command) == []


def test_autonomy_level_hook_is_a_second_layer_that_a_wrapped_command_gets_past():
    """Base64 piped to a shell, a script written first and ``eval`` of a variable are not read: the hub's leases and
    gates are the boundary, the hook only a second layer."""
    for command in (
        "echo Z2l0IHB1c2ggLS1mb3JjZSBvcmlnaW4gbWFpbgo= | base64 -d | sh",
        "printf 'git push --force origin main' > p.sh && sh p.sh",
        'CMD="git push --force origin main"; eval "$CMD"',
    ):
        assert classes(command) == [], command
    assert simple_commands("a && b; c | d\ne") == [["a"], ["b"], ["c"], ["d"], ["e"]]


# The gate: what each mode does


def modes(**given: str) -> dict:
    found = dict.fromkeys(CLASSES, "auto")
    found.update(given)
    return found


class Recorder:
    def __init__(self, answer: dict | None = None):
        self.events: list[tuple[str, dict]] = []
        self.acts: list[dict] = []
        self.answer = answer

    def record(self, text: str, body: dict) -> None:
        self.events.append((text, body))

    async def act(self, body: dict) -> dict | None:
        self.acts.append(body)
        return self.answer


def gate(tmp_path, recorder: Recorder, **given: str) -> Gate:
    autonomy = {
        "card": "# Your autonomy on this run",
        "modes": modes(**given),
        "categories": ["architecture", "scope"],
        "sources": dict.fromkeys(CLASSES, "ceiling:unit/interns"),
    }
    made = Gate.of(autonomy, WORKTREE, tmp_path / "backups.jsonl", record=recorder.record, act=recorder.act)
    assert made is not None
    return made


def check(made: Gate, command: str, tool: str = "Bash") -> str | None:
    return asyncio.run(made.check(tool, {"command": command}))


def test_autonomy_level_deny_refuses_naming_class_mode_and_source(tmp_path):
    recorder = Recorder()
    why = check(gate(tmp_path, recorder, K5="deny"), "gh pr merge 12")
    assert (
        why == f"gh pr merge 12: K5 ({CLASS_NAMES['K5']}) is deny for the run's principal, set by ceiling:unit/interns"
    )
    assert recorder.acts == [], "a deny needs no word of the hub"
    ((text, body),) = recorder.events
    assert body == {"class": "K5", "mode": "deny", "what": "gh pr merge 12", "verdict": "refused"}
    assert text.startswith("Autonomy hook refused a command of K5 (deny)")


def test_autonomy_level_ask_refuses_and_names_the_category_to_ask(tmp_path):
    recorder = Recorder({"mode": "ask", "told": False, "answered": True})
    made = gate(tmp_path, recorder, K6="ask")
    why = check(made, "gh release create v1.0.0")
    assert "K6" in why and "is ask" in why and "ceiling:unit/interns" in why
    assert "evo-agents worker ask --category deploy" in why and "EVO_DECISION=<the decision's id>" in why
    assert recorder.acts == []
    assert check(made, "EVO_DECISION=31 gh release create v1.0.0") is None, "the hub says #31 is answered"
    assert recorder.acts == [{"klass": "K6", "what": "gh release create v1.0.0", "decision_id": 31}]
    recorder.answer = {"mode": "ask", "told": False, "answered": False}
    refused = check(made, "EVO_DECISION=32 gh release create v1.0.0")
    assert "decision #32 is not an answered decision of this run in category deploy" in refused
    recorder.answer = None
    assert "the hub did not say whether decision #33 is answered" in check(
        made, "EVO_DECISION=33 gh release create v1.0.0"
    )


def test_autonomy_level_ask_of_k9_names_both_categories(tmp_path):
    why = check(gate(tmp_path, Recorder(), K9="ask"), "kubectl delete pod web-1")
    assert "--category delete_data" in why and "--category live_migration" in why


def test_autonomy_level_notify_lets_through_records_and_asks_the_hub_to_tell(tmp_path):
    recorder = Recorder({"mode": "notify", "told": True, "answered": False})
    made = gate(tmp_path, recorder, K10="notify")
    assert check(made, "gh issue create --repo other/lib --title Bug --body x") is None
    assert recorder.acts == [{"klass": "K10", "what": "gh issue create --repo other/lib --title Bug --body x"}]
    ((text, body),) = recorder.events
    assert body["verdict"] == "allowed" and body["mode"] == "notify" and "the hub tells the principal" in text
    recorder.answer = None
    assert check(made, "gh pr create -R other/lib --fill") is None, "a hub that does not answer leaves it through"
    recorder.answer = {"mode": "deny", "told": False, "answered": False}
    why = check(made, "gh pr create -R other/lib --fill")
    assert "is deny" in why and "stricter than the run's snapshot" in why


def test_autonomy_level_auto_lets_through_and_records(tmp_path):
    recorder = Recorder()
    assert check(gate(tmp_path, recorder), "gh pr merge 12") is None
    assert recorder.acts == [] and [body["verdict"] for _, body in recorder.events] == ["allowed"]
    assert check(gate(tmp_path, recorder), "ls -la") is None and len(recorder.events) == 1, "no class, no event"
    assert check(gate(tmp_path, recorder, K5="deny"), "gh pr merge 1", tool="Read") is None, "Bash alone"


# The floor S2: a backup made within 24 hours before K9, for every principal


def test_autonomy_level_k9_needs_a_fresh_backup_even_at_a5(tmp_path):
    recorder = Recorder()
    made = gate(tmp_path, recorder)  # every class auto: a principal at A5
    why = check(made, "psql -c 'DROP TABLE users'")
    assert "floor S2" in why and "make a backup first" in why and "evo-agents worker backup --target" in why
    old = datetime.now(UTC) - timedelta(hours=25)
    add_backup(tmp_path / "backups.jsonl", {"target": "db", "location": "/b/1", "made_at": old.isoformat()})
    assert "make a backup first" in check(made, "psql -c 'DROP TABLE users'"), "a backup older than 24 hours"
    add_backup(
        tmp_path / "backups.jsonl", {"target": "db", "location": "/b/2", "made_at": datetime.now(UTC).isoformat()}
    )
    assert check(made, "psql -c 'DROP TABLE users'") is None
    assert fresh_backup(tmp_path / "backups.jsonl")["location"] == "/b/2"
    assert (tmp_path / "backups.jsonl").stat().st_mode & 0o777 == 0o600


def test_autonomy_level_deny_comes_before_the_backup_and_a_run_without_a_snapshot_has_no_gate(tmp_path):
    why = check(gate(tmp_path, Recorder(), K9="deny"), "terraform destroy")
    assert "is deny" in why and "backup" not in why
    assert Gate.of(None, WORKTREE, tmp_path / "b.jsonl") is None
    assert Gate.of({"card": "x"}, WORKTREE, tmp_path / "b.jsonl") is None
    assert autonomy_hook.claude_hook(None) is None


# The PreToolUse hook of a claude-code run


def test_autonomy_level_claude_code_gets_the_hook_with_or_without_a_profile(tmp_path):
    from tests.worker.test_agent_profiles import _launch, needs, profile
    from tests.worker.test_runtimes import _claude, _samples

    needs("claude_agent_sdk")
    from evo_agents.worker.runtimes import profiles

    made = gate(tmp_path, Recorder(), K5="deny")
    hooks = profiles.claude_hooks(None, made)["PreToolUse"][0].hooks
    assert len(hooks) == 1
    denied = asyncio.run(hooks[0]({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 3"}}, "toolu_1", {}))
    out = denied["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "K5" in out["permissionDecisionReason"]
    assert asyncio.run(hooks[0]({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "toolu_2", {})) == {}
    limited = profiles.Profile.of({"profile": profile(tool=["read", "bash"])})
    both = profiles.claude_hooks(limited, made)["PreToolUse"][0].hooks
    assert len(both) == 2, "the profile's hook and the hook of autonomy, side by side"
    assert profiles.claude_hooks(None, None) is None

    adapter = _claude(tmp_path, _samples)
    adapter.context = dataclasses.replace(adapter.context, autonomy=made)
    adapter._session = "11111111-2222-3333-4444-555555555555"
    options = adapter.build_options("/opt/bin/claude")
    assert options.hooks is not None and len(options.hooks["PreToolUse"][0].hooks) == 1
    _flags, _env, without = _launch(tmp_path)
    assert without.hooks is None, "a run without a snapshot launches as before"
