"""``evo-agents hub contract``: the command line as data (seam hub-cli-v1) and the check of hub commands in markdown.

The golden copy is tests/hub/golden/cli-contract.json. After a deliberate change of a command, an option, a --json
key or the API, regenerate it with ``python -m evo_agents hub contract print > tests/hub/golden/cli-contract.json``
and tell the consumers of hub-cli-v1 (evo-cli, agent-skills).
"""

import argparse
import json
from pathlib import Path

import pytest

from evo_agents import __version__
from evo_agents.hub import contract, kg_cli, memory, skill_sync
from evo_agents.hub.contract import check_command, commands, find_commands, hub_parsers
from evo_agents.hub.mirror import ExportResult
from evo_agents.hub.plan_cli import ImportResult
from evo_agents.hub.registry import PullResult
from tests.hub import pg
from tests.hub.contract_keys import assert_json_keys

ROOT = Path(__file__).parents[2]
GOLDEN = Path(__file__).parent / "golden" / "cli-contract.json"
REGENERATE = "python -m evo_agents hub contract print > tests/hub/golden/cli-contract.json"
HUB_URL = "https://hub.example.org"

# What the commands whose --json output is built on this side print, from the code that builds it.
BUILT_HERE = {
    "hub registry pull": lambda: PullResult(Path("registry.json")).as_json(),
    "hub plan import": lambda: ImportResult(Path("harness"), "demo").as_json(),
    "hub plan export": lambda: ExportResult(Path("harness"), "demo").as_json(),
    "hub memory push": lambda: memory.Report("push", HUB_URL).as_json(),
    "hub memory pull": lambda: memory.Report("pull", HUB_URL).as_json(),
    "hub skills sync": lambda: skill_sync.Report(HUB_URL, False).as_json(),
    "hub kg push": lambda: kg_cli.push_json([]),
}


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def without_version(document: dict) -> dict:
    """The OpenAPI document with the package version left out, so a release does not change the golden copy."""
    return {**document, "info": {**document["info"], "version": "<version>"}}


def run(*args: str):
    return pg.cli(["hub", "contract", *args], pg.clean_env())


# The contract


def test_commands_match_the_golden_copy():
    assert commands() == golden()["commands"], f"the hub command line changed; if on purpose: {REGENERATE}"


def test_print_writes_version_commands_and_the_openapi_document():
    pytest.importorskip("fastapi")
    from evo_agents.hub.openapi import document

    result = run("print")
    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert list(printed) == ["version", "commands", "openapi"]
    assert printed["version"] == contract.CONTRACT_VERSION == 1
    assert printed["commands"] == commands()
    assert printed["openapi"] == document()
    assert printed["openapi"]["info"]["version"] == __version__
    assert without_version(printed["openapi"]) == without_version(golden()["openapi"]), (
        f"the API changed; if on purpose: {REGENERATE}"
    )


def test_print_without_the_server_extra_names_the_pip_command(monkeypatch, capsys):
    def missing():
        raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")

    monkeypatch.setattr(contract, "build", missing)
    assert contract.cmd_print(argparse.Namespace(hub_command="contract")) == 2
    assert capsys.readouterr().out == ""


def test_every_hub_command_is_in_the_contract_with_its_options():
    printed = commands()
    for path, parser in hub_parsers().items():
        flags = {flag for action in parser._actions for flag in action.option_strings} - {"-h", "--help"}
        assert {flag for option in printed[path]["options"] for flag in option["flags"]} == flags
    assert printed["hub plan step"]["positionals"][2] == {
        "name": "status",
        "metavar": "STATUS",
        "value": "string",
        "required": True,
        "repeatable": False,
        "choices": ["done", "in_progress", "pending", "blocked"],
    }
    limit = next(o for o in printed["hub memory search"]["options"] if o["flags"] == ["--limit"])
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 50, 10)
    patch_set = next(o for o in printed["hub plan patch"]["options"] if o["flags"] == ["--set"])
    assert patch_set["repeatable"] and patch_set["required"]


def test_a_command_prints_json_exactly_when_it_declares_the_keys():
    for path, parser in hub_parsers().items():
        has_flag = any("--json" in action.option_strings for action in parser._actions)
        declared = parser.get_default("json_output")
        assert has_flag == (declared is not None), path
        if declared is not None and declared.kind != "map":
            assert declared.keys and len(set(declared.keys)) == len(declared.keys), path


def test_keys_passed_on_from_the_hub_are_the_properties_of_its_answer():
    pytest.importorskip("fastapi")
    from evo_agents.hub.openapi import document

    schemas = document()["components"]["schemas"]
    checked = 0
    for path, parser in hub_parsers().items():
        output = parser.get_default("json_output")
        for declared in [output] + [variant for _, variant in output.variants] if output else []:
            if declared.schema is None:
                continue
            properties = set(schemas[declared.schema]["properties"])
            assert set(declared.added) <= set(declared.keys), path
            assert not set(declared.added) & properties, path
            assert set(declared.keys) == properties | set(declared.added), path
            checked += 1
    assert checked >= 16


def test_admin_stats_is_a_map_of_counts():
    pytest.importorskip("fastapi")
    from evo_agents.hub.openapi import document

    answer = document()["paths"]["/v1/admin/stats"]["get"]["responses"]["200"]["content"]["application/json"]
    assert answer["schema"]["additionalProperties"] == {"type": "integer"}
    assert commands()["hub admin stats"]["json"] == {"kind": "map", "keys": []}


def test_keys_built_here_are_what_the_code_builds():
    for path, parser in hub_parsers().items():
        output = parser.get_default("json_output")
        if output is None or output.schema is not None or output.kind == "map":
            continue
        assert path in BUILT_HERE, f"{path} builds its --json output here: add it to BUILT_HERE"
        assert list(BUILT_HERE[path]()) == list(output.keys), path


def test_assert_json_keys_follows_kind_and_variants():
    assert_json_keys("hub admin stats", {"users": 2})
    assert_json_keys("hub plan list", [])
    with pytest.raises(AssertionError):
        assert_json_keys("hub kg push", {"ok": True})
    body = {"revision": 1, "area": "active", "digest": "d", "summary": "s", "actor": "a", "created_at": "t"}
    assert_json_keys("hub plan show", {**body, "label": "l", "body": {}}, "--revision")


# Commands in markdown

MARKDOWN = """# Title

Run `evo-agents hub login --url https://hub.example.org` once, then ``evo-agents hub plan step
my-plan 3 done --evidence "a b"``. The group `evo-agents hub plan` lists them.

```sh
# evo-agents hub this-is-a-comment
evo-agents hub plan patch my-plan --step 3 \\
  --set status=done --json | jq .revision
uvx --from evo-ak==0.2.0 evo-agents hub hook stop || true
```

~~~
evo-agents hub kg build --project <name> --wait 2>/dev/null
~~~

    evo-agents hub indented --is-not-read
"""


def test_find_commands_reads_code_spans_and_fenced_blocks_with_their_lines():
    found = [(f.line, f.text) for f in find_commands(MARKDOWN, "doc.md")]
    assert found == [
        (3, "evo-agents hub login --url https://hub.example.org"),
        (3, 'evo-agents hub plan step my-plan 3 done --evidence "a b"'),
        (4, "evo-agents hub plan"),
        (8, "evo-agents hub plan patch my-plan --step 3    --set status=done --json | jq .revision"),
        (10, "evo-agents hub hook stop || true"),
        (14, "evo-agents hub kg build --project <name> --wait 2>/dev/null"),
    ]
    assert all(not check_command(commands(), text) for _, text in found)


@pytest.mark.parametrize(
    "text",
    [
        "evo-agents hub",
        "evo-agents hub --help",
        "evo-agents hub plan",
        "evo-agents hub plan step <slug> <id> done",
        "evo-agents hub plan step PLAN STEP STATUS --evidence '...'",
        "evo-agents hub plan patch ... --json",
        "evo-agents hub plan put plans/active/<slug>.yaml --if-revision 4",
        "evo-agents hub plan list [--area completed] [--json]",
        "evo-agents hub memory search 'a phrase' --limit=50",
        'evo-agents hub contract print > "$TMPDIR/hub-contract.json" && cd elsewhere',
        "evo-agents hub kg build --project p --timeout 90.5",
        "evo-agents hub serve --help",
        "evo-agents hub token revoke $ID",
        "evo-agents hub contract check a.md b.md c.md",
    ],
)
def test_commands_the_command_line_takes_pass(text):
    assert check_command(commands(), text) == []


@pytest.mark.parametrize(
    "text, problem",
    [
        ("evo-agents hub plans list", "`evo-agents hub` has no subcommand 'plans'"),
        ("evo-agents hub plan stpe x 1 done", "`evo-agents hub plan` has no subcommand 'stpe'"),
        ("evo-agents hub plan --json", "takes a subcommand"),
        ("evo-agents hub plan list --all", "`evo-agents hub plan list` has no option --all"),
        ("evo-agents hub plan list --area archived", "--area 'archived' is not one of active, completed"),
        ("evo-agents hub plan step x 1 finished", "STATUS 'finished' is not one of done"),
        ("evo-agents hub plan put", None),
        ("evo-agents hub plan show x --revision", "--revision needs a value"),
        ("evo-agents hub plan show x --revision latest", "--revision 'latest' is not a whole number"),
        ("evo-agents hub whoami --json=yes", "--json takes no value"),
        ("evo-agents hub token list all", "takes at most 0 argument(s); 'all' is too many"),
        ("evo-agents hub memory search q --limit 99", "--limit '99' is not a whole number from 1 to 50"),
        ("evo-agents hub plan step x 1 done --evidence 'unclosed", "cannot be read as a shell command"),
    ],
)
def test_commands_the_command_line_does_not_take_are_reported(text, problem):
    problems = check_command(commands(), text)
    if problem is None:  # a missing positional is not checked: prose names commands without their arguments
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems


def test_check_prints_each_problem_with_file_and_line(tmp_path):
    good = tmp_path / "good.md"
    good.write_text(MARKDOWN, encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("text\n\n```sh\nevo-agents hub plan list --all\n```\n\nand `evo-agents hub nope`\n", "utf-8")

    passed = run("check", str(good))
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert passed.stdout == "6 hub command(s) in 1 file(s), all in the contract\n"

    failed = run("check", str(good), str(bad))
    assert failed.returncode == 1
    assert failed.stdout.splitlines() == [
        f"{bad}:4: evo-agents hub plan list --all: `evo-agents hub plan list` has no option --all",
        f"{bad}:7: evo-agents hub nope: `evo-agents hub` has no subcommand 'nope'; it has "
        + ", ".join(sorted({key.split()[1] for key in commands()})),
        "2 problem(s) in the 8 hub command(s) of 2 file(s)",
    ]


def test_check_against_a_saved_contract(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("`evo-agents hub plan step x 1 done --evidence e`\n", encoding="utf-8")
    saved = {"version": 1, "commands": {k: v for k, v in commands().items() if k != "hub plan step"}}
    older = tmp_path / "contract.json"
    older.write_text(json.dumps(saved), encoding="utf-8")
    result = run("check", "--contract", str(older), str(doc))
    assert result.returncode == 1
    assert "`evo-agents hub plan` has no subcommand 'step'" in result.stdout

    older.write_text(json.dumps({"version": 2, "commands": {}}), encoding="utf-8")
    refused = run("check", "--contract", str(older), str(doc))
    assert refused.returncode == 2 and "is not a version 1 hub contract" in refused.stderr


def test_check_of_a_missing_file_is_a_usage_error(tmp_path):
    result = run("check", str(tmp_path / "missing.md"))
    assert result.returncode == 2
    assert result.stdout == "" and result.stderr.startswith("error: ")


def test_the_docs_of_this_repository_pass():
    files = [str(ROOT / "README.md"), str(ROOT / "docs" / "hub.md"), str(ROOT / "web" / "README.md")]
    count, problems = contract.check_files(commands(), files)
    assert problems == []
    assert count >= 30
