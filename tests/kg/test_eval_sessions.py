"""``evo-agents kg eval-questions --from-sessions``: questions from the code searches agents made in a harness's Claude
Code transcripts, answered by the files they opened next. Synthetic transcripts and repos only."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub import kg_eval_sessions as ks
from evo_agents.hub.memory import slug
from evo_agents.kg import evaluate as ev
from evo_agents.kg.project import load_project_at
from tests.kg.projects import commit, git, write

NOW = datetime.now(UTC)
RECENT = (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")
OLD = (NOW - timedelta(days=400)).isoformat().replace("+00:00", "Z")
SINCE = (NOW - timedelta(days=30)).timestamp()
IDS = itertools.count(1)


# -- the shell ------------------------------------------------------------------------------------------------------


def test_simple_commands_split_on_operators_and_drop_redirects_and_heredocs():
    line = "cd /x 2>/dev/null && git grep -n \"a\\|b\" $R -- '*.py' | grep -v /tests/; echo ---"
    assert ks.simple_commands(line) == [
        (["cd", "/x"], False),
        (["git", "grep", "-n", "a\\|b", "$R", "--", "*.py"], False),
        (["grep", "-v", "/tests/"], True),
        (["echo", "---"], False),
    ]
    heredoc = "python - <<'EOF' 2>&1 | tail -3\ngrep -rn ignored_name .\nEOF\nR=main; cat app/a.py"
    assert ks.simple_commands(heredoc) == [
        (["python", "-"], False),
        (["tail", "-3"], True),
        (["cat", "app/a.py"], False),
    ]
    assert ks.simple_commands("grep -rn 'open quote") == []
    steps = list(ks.steps("cd /a && cd sub; cat x; cd; cat y", "/start"))
    assert [(s.argv, s.cwd) for s in steps] == [(("cat", "x"), "/a/sub"), (("cat", "y"), None)]
    assert ks.resolve("../b/c.py", "/a/sub") == "/a/b/c.py" and ks.resolve("rel", None) is None
    assert ks.resolve("$HOME/x", None) == str(Path.home() / "x") and ks.resolve("$D/x", "/a") is None


def test_terms_keep_the_identifiers_of_a_pattern_and_drop_regex_syntax():
    patterns = ["def get_tenant_id\\b", "^class RefreshTokenBlacklist", "navigator\\.locks|BroadcastChannel", "1234"]
    patterns += ["cls:", "peer\\|peer_review", "Peer_Review", "\\s+export async function", "app-editor"]
    assert ks.terms_of(patterns) == [
        "get_tenant_id",
        "RefreshTokenBlacklist",
        "navigator.locks",
        "BroadcastChannel",
        "peer",
        "peer_review",
        "app-editor",
    ]
    shaped = {term: ks.code_shaped(term) for term in ["get_tenant_id", "BroadcastChannel", "navigator.locks", "HTML"]}
    shaped |= {term: ks.code_shaped(term) for term in ["app-editor", "uc102", "peer", "Permission", "refresh"]}
    assert [term for term, yes in shaped.items() if yes] == [
        "get_tenant_id",
        "BroadcastChannel",
        "navigator.locks",
        "HTML",
        "app-editor",
        "uc102",
    ]


# -- a harness, its sibling repos and their sessions ------------------------------------------------------------------


@pytest.fixture
def cluster(tmp_path, kg_env):
    """northwind-harness with the sibling repos northwind-api (a source, on branch main, with a branch release the
    source reads) and lumen-web (a source), tools (in harness.yaml, no source), and transcripts of its sessions."""
    api, web, tools = tmp_path / "northwind-api", tmp_path / "lumen-web", tmp_path / "tools"
    for path in ("app/billing.py", "app/invoice.py", "app/__init__.py", "uv.lock", "config.json"):
        write(api / path, f"# {path}\nclass BillingRun:\n    invoice_total = 0\n")
    git(api, "init", "-q", "-b", "main")
    commit(api, "init")
    git(api, "checkout", "-q", "-b", "release")
    write(api / "app/release_only.py", "RELEASE_FLAG = True\n")
    commit(api, "release")
    git(api, "checkout", "-q", "main")
    write(api / "app/scratch.py", "untracked = 1\n")
    write(web / "src/cart.ts", "export function cartTotal() {}\n")
    git(web, "init", "-q", "-b", "main")
    commit(web, "init")
    write(tools / "x.py", "x = 1\n")

    root = tmp_path / "northwind-harness"
    write(root / "app/billing.py", "# the harness's own file of the same name\n")
    repos = "".join(
        f"  - name: {name}\n    path: {name}\n    origin: git@github.com:northwind/{name}.git\n"
        for name in ("northwind-api", "lumen-web", "tools", "northwind-harness")
    )
    write(root / "harness.yaml", f"name: northwind\nworkspace: ..\nknowledge_file: knowledge.yaml\nrepos:\n{repos}")
    write(
        root / "knowledge.yaml",
        """version: 1
project: northwind
policy:
  levels: [public, internal]
  sinks:
    - id: agent
      kind: agent-session
      clearance: {level: internal}
sources:
  - id: harness
    connector: harness
    label: {level: internal, integrity: U}
  - id: api
    connector: git
    repo: northwind-api
    ref: release
    label: {level: internal, integrity: U}
  - id: web
    connector: git
    repo: lumen-web
    ref: origin/main
    label: {level: internal, integrity: U}
  - id: cli
    connector: git
    repo: northwind-harness
    label: {level: internal, integrity: U}
""",
    )
    git(root, "init", "-q", "-b", "main")
    commit(root, "init")
    return load_project_at(root, kg_env), tmp_path / "claude" / "projects"


def tool(name: str, cwd: Path | str, when: str = RECENT, **given) -> dict:
    block = {"type": "tool_use", "id": f"toolu_{next(IDS)}", "name": name, "input": given}
    return {
        "type": "assistant",
        "cwd": str(cwd),
        "timestamp": when,
        "message": {"role": "assistant", "content": [block]},
    }


def bash(command: str, cwd, when: str = RECENT) -> dict:
    return tool("Bash", cwd, when, command=command)


def read(path, cwd, when: str = RECENT) -> dict:
    return tool("Read", cwd, when, file_path=str(path))


def transcript(projects: Path, started_in: Path, lines: list[dict], name: str | None = None) -> Path:
    session = name or f"0000000{next(IDS)}-1111-2222-3333-444444444444"
    path = projects / slug(started_in) / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {"type": "user", "cwd": str(started_in), "timestamp": RECENT, "message": {"role": "user", "content": "go"}}
    path.write_text("".join(json.dumps(line) + "\n" for line in [head, *lines]), encoding="utf-8")
    return path


def test_repo_map_finds_a_repo_through_its_worktrees_but_not_the_harness(cluster, tmp_path):
    project, _ = cluster
    api = tmp_path / "northwind-api"
    tree = tmp_path / "elsewhere" / "api-tree"
    git(api, "worktree", "add", "-q", str(tree), "release")
    sources = ks.source_repos(project)
    assert sorted(sources) == ["lumen-web", "northwind-api"]  # tools reads no source, the harness is no sibling
    assert sources["northwind-api"][1] == ("release",)
    repos = ks.repo_map({name: checkout for name, (checkout, _) in sources.items()})
    assert repos.locate(f"{api}/app/billing.py") == ("northwind-api", "app/billing.py")
    assert repos.locate(f"{api}") == ("northwind-api", "")
    for nested in (f"{api}/.claude/worktrees/bold-x", f"{api}-wt/fix", f"{api}-wt-fix", str(tree)):
        assert repos.locate(f"{nested}/app/invoice.py") == ("northwind-api", "app/invoice.py")
    assert repos.locate(f"{tmp_path}/northwind-api2/app/x.py") is None
    assert repos.locate(f"{tmp_path}/northwind-harness/app/billing.py") is None


def test_a_search_names_one_repo_and_its_patterns(cluster, tmp_path):
    project, _ = cluster
    api, web, root = tmp_path / "northwind-api", tmp_path / "lumen-web", project.harness.root
    sources = ks.source_repos(project)
    repos = ks.repo_map({name: checkout for name, (checkout, _) in sources.items()})

    def search(command, cwd=root):
        found = [ks.search_of(step, repos) for step in ks.steps(command, str(cwd))]
        return [hit for hit in found if hit is not None]

    assert search(f"cd {api} && git grep -n -A3 'BillingRun' origin/main -- app/ | grep -v tests") == [
        ("northwind-api", ["BillingRun"])
    ]
    assert search(f"git -C {api} grep -e invoice_total -e BillingRun") == [
        ("northwind-api", ["invoice_total", "BillingRun"])
    ]
    assert search(f"grep -rn --include '*.py' invoice_total {api}/app") == [("northwind-api", ["invoice_total"])]
    assert search(f"cd {api}-wt/fix && rg -t py BillingRun") == [("northwind-api", ["BillingRun"])]
    assert search("rg cartTotal src", cwd=web) == [("lumen-web", ["cartTotal"])]
    assert search(f"grep -n invoice_total {api}/app/billing.py") == []  # not recursive: no search of the code
    assert search(f"cd {api} && git log | rg BillingRun") == []  # rg filtering a pipe
    assert search(f"rg BillingRun {api} {web}") == []  # two repos
    assert search("grep -rn BillingRun .") == []  # the harness
    assert search(f"cd {api} && git grep -f patterns.txt") == []


def test_the_files_a_call_opened(cluster, tmp_path):
    project, _ = cluster
    api, root = tmp_path / "northwind-api", project.harness.root
    sources = ks.source_repos(project)
    repos = ks.repo_map({name: checkout for name, (checkout, _) in sources.items()})

    def opened(line):
        return ks.opened(
            ks.Call(line["message"]["content"][0]["name"], line["message"]["content"][0]["input"], str(root)), repos
        )

    assert opened(read(f"{api}-wt/fix/app/billing.py", root)) == [("northwind-api", "app/billing.py")]
    assert opened(read(root / "app/billing.py", root)) == []
    assert opened(bash(f"cd {api} && R=release; git show $R:app/invoice.py | cat -n | sed -n 1,5p", root)) == [
        ("northwind-api", "app/invoice.py")
    ]
    assert opened(bash(f"git -C {api}/app show main:./billing.py", root)) == [("northwind-api", "app/billing.py")]
    assert opened(bash("sed -n 1,20p app/billing.py; cat app/invoice.py", root)) == []  # the harness's files
    assert opened(bash(f'cd {api}/app && head -n 20 invoice.py && tail -5 ../app/billing.py && cat "$F"', root)) == [
        ("northwind-api", "app/invoice.py"),
        ("northwind-api", "app/billing.py"),
    ]
    assert (
        opened(bash(f"cd {api} && sed -i s/a/b/ app/billing.py && sed 1p app/invoice.py && cat app/*.py", root)) == []
    )
    assert opened(bash(f'cd {api} && git show "${{R}}:app/${{f}}" && git show --stat HEAD', root)) == []


def test_questions_from_sessions_name_the_files_opened_after_each_search(cluster, tmp_path):
    project, projects = cluster
    api, web, root = tmp_path / "northwind-api", tmp_path / "lumen-web", project.harness.root
    worktree = root / ".claude" / "worktrees" / "bold-x"
    transcript(
        projects,
        root,
        [
            bash(f"cd {api} && git grep -n 'class BillingRun\\|invoice_total' release -- app/", root),
            bash("sed -n 1,40p app/billing.py", root),  # the harness's file: not an answer
            bash(f"cd {api} && git show release:app/release_only.py && cat uv.lock config.json app/scratch.py", root),
            read(f"{api}/app/invoice.py", root),
            bash(f"cd {web} && rg -n 'refresh' src", root),  # no identifier
            bash(f"cd {web} && rg -n 'cartTotal' src", root),  # nothing of lumen-web opened after it
            bash("echo one", root),
            bash("echo two", root),
            bash("echo three", root),
            bash("echo four", root),
            bash("echo five", root),
            bash("echo six", root),
            read(f"{web}/src/cart.ts", root),  # the seventh call after: too late
            bash(f"cd {api} && grep -rn BillingRun app", root, when=OLD),  # before the days asked
            read(f"{api}/app/billing.py", root, when=OLD),
        ],
    )
    # the same terms again, from a Claude Code worktree of the harness, with a subagent's transcript
    again = transcript(
        projects,
        worktree,
        [
            bash(f"git -C {api} grep -e BillingRun -e invoice_total", worktree),
            read(f"{api}/app/billing.py", worktree),
            read(f"{api}/app/invoice.py", worktree),
        ],
    )
    subagent = again.parent / again.stem / "subagents" / "agent-a1.jsonl"
    write(subagent, json.dumps(bash(f"cd {api}-wt/fix && rg 'InvoiceLine'", worktree)) + "\n")
    with subagent.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(read(f"{api}-wt/fix/app/invoice.py", worktree)) + "\n")
    # another harness whose name only starts the same: left out by the directory its session ran in
    transcript(projects, tmp_path / "northwind-harness2", [bash(f"cd {api} && git grep FOREIGN_NAME", root)])

    found = ks.session_questions(project, [projects], since=SINCE)
    assert (found.transcripts, found.searches, found.no_identifier, found.unanswered) == (3, 5, 1, 1)
    by_text = {q["question"]: q for q in found.questions}
    assert sorted(by_text) == ["BillingRun invoice_total", "InvoiceLine"]
    billing = by_text["BillingRun invoice_total"]
    assert billing["files"] == [
        "northwind-api:app/invoice.py",  # opened after both searches
        "northwind-api:app/release_only.py",  # in the ref the source reads, not in the working tree
        "northwind-api:app/billing.py",
    ]
    assert billing["searches"] == 2 and billing["type"] == ev.SESSION_TYPE == "session"
    assert (billing["project"], billing["repo"]) == ("northwind", "northwind-api")
    assert by_text["InvoiceLine"]["files"] == ["northwind-api:app/invoice.py"]
    # the id is the hash of the repo and the terms, whatever their case: the question keeps its part for good
    assert billing["id"] == ks.question_id("northwind-api", "billingrun INVOICE_TOTAL")
    assert billing["id"].startswith("session-") and len({q["id"] for q in found.questions}) == 2
    assert ks.session_questions(project, [projects], since=SINCE).questions == found.questions
    assert ev.parse_questions(json.dumps(q) for q in found.questions)[0].files[0] == ("northwind-api", "app/invoice.py")


def test_eval_questions_command_prints_a_session_set_kg_eval_reads(cluster, tmp_path, capsys):
    project, projects = cluster
    api, root = tmp_path / "northwind-api", project.harness.root
    transcript(projects, root, [bash(f"cd {api} && git grep -n BillingRun", root), read(f"{api}/app/billing.py", root)])
    command = ["kg", "eval-questions", "--from-sessions", "--project", str(root), "--transcripts", str(projects)]
    assert main([*command, "--days", "7"]) == 0
    out = capsys.readouterr()
    assert "1 question(s) from 1 transcript(s) of northwind in the last 7 day(s)" in out.err
    assert "  northwind-api: 1" in out.err
    [question] = ev.parse_questions(out.out.splitlines())
    assert (question.type, question.text, question.files) == (
        "session",
        "BillingRun",
        (("northwind-api", "app/billing.py"),),
    )

    questions = tmp_path / "sessions.jsonl"
    questions.write_text(out.out, encoding="utf-8")
    evaluate = ["kg", "eval", str(questions), "--project", str(root), "--type", "session", "--method", "grep"]
    assert main([*evaluate, "--repo", f"northwind-api={api}", "--json"]) == 0
    [result] = json.loads(capsys.readouterr().out)["results"]
    assert result["method"] == "grep" and result["ranks"][0] is not None

    assert main([*command, "--plan", "demo"]) == 2
    assert "--plan picks plans" in capsys.readouterr().err


def test_a_term_that_reads_as_a_secret_never_makes_a_question():
    from evo_agents.hub.kg_eval_sessions import terms_of

    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert terms_of([f"{token}\\|lumen_cards"]) == ["lumen_cards"]
