"""``deploy/worker/bootstrap.sh`` and ``evo-agents worker pool user-data``, step 5 of the worker-pools plan (criterion
a6): the script parses (and passes shellcheck when the machine has it), the package ships the same file, the user-data
is the script with the hub and the join token written in, and a run as a plain user installs in its home, joins with
the token from the environment and installs the service, then a second run on the same machine joins nothing again.
The installers are stubs on PATH here, so nothing is downloaded and nothing outside a temporary home changes."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from evo_agents.worker.browser.install import LINUX_PACKAGES
from evo_agents.worker.pool_cli import JOIN_TOKEN_ENV, bootstrap_script, render_user_data

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "worker" / "bootstrap.sh"
URL = "https://hub.example.org"
TOKEN = "evj_test'odd$token"  # a quote and a dollar, which the user-data must keep as they are
STUBS = ("uv", "node", "npm", "corepack", "pnpm", "claude", "codex", "opencode", "loginctl", "systemctl")
EVO_AGENTS_STUB = """#!/bin/bash
echo "evo-agents $*" >> "$HOME/calls.log"
if [ "$1 $2" = "worker join" ]; then
    printf '%s' "$EVO_JOIN_TOKEN" > "$HOME/seen-token"
    mkdir -p "$HOME/.evo/worker"
    echo '{}' > "$HOME/.evo/worker/config.json"
    echo evw_x > "$HOME/.evo/worker/token"
fi
if [ "$1 $2 $3" = "worker service install" ]; then
    mkdir -p "$HOME/.config/systemd/user"
    : > "$HOME/.config/systemd/user/evo-agents-worker.service"
fi
"""


def test_the_script_parses_and_the_package_ships_the_same_file():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert bootstrap_script() == SCRIPT.read_text(encoding="utf-8")
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed on this machine")
def test_shellcheck_finds_nothing():
    result = subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_script_installs_every_library_chromium_needs():
    words = re.findall(r"[\w.+-]+", SCRIPT.read_text(encoding="utf-8"))
    assert [package for package in LINUX_PACKAGES if package not in words] == []
    assert {"fonts-liberation", "fonts-noto-core", "fontconfig"} <= set(words)


def test_user_data_is_the_script_with_the_hub_and_the_token_written_in():
    data = render_user_data(URL, TOKEN)
    assert data.startswith("#!/bin/bash\n")  # cloud-init runs user-data that starts with #! as a script
    assert data.count(TOKEN) == 0  # quoted, never bare
    printed = subprocess.run(
        [
            "bash",
            "-c",
            data.split("\nEVO_HUB_URL=${EVO_HUB_URL", 1)[0] + '\nprintf "%s\\n%s" "$EVO_HUB_URL" "$EVO_JOIN_TOKEN"',
        ],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=True,
    )
    assert printed.stdout == f"{URL}\n{TOKEN}"
    with pytest.raises(Exception, match="settings line"):
        render_user_data(URL, TOKEN, "#!/bin/bash\necho no settings here\n")


@pytest.fixture
def machine(tmp_path):
    """A home whose ~/.local/bin has stubs of every installer and of evo-agents, which log their calls."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in STUBS:
        stub = bin_dir / name
        version = 'if [ "$1" = --version ]; then echo v22.11.0; fi\n' if name == "node" else ""
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "$HOME/calls.log"\n{version}', encoding="utf-8")
        stub.chmod(0o755)
    (bin_dir / "evo-agents").write_text(EVO_AGENTS_STUB, encoding="utf-8")
    (bin_dir / "evo-agents").chmod(0o755)
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    return home, env


def _run(script: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=env["HOME"], timeout=60)


def _calls(home: Path) -> list[str]:
    log = home / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the script installs with apt and creates a user")
def test_user_data_as_a_plain_user_joins_once_and_a_second_run_changes_nothing(machine, tmp_path):
    home, env = machine
    data = tmp_path / "user-data"
    data.write_text(render_user_data(URL, TOKEN), encoding="utf-8")

    first = _run(data, env)
    assert first.returncode == 0, first.stderr
    calls = _calls(home)
    assert "uv python install 3.12" in calls
    assert f"evo-agents worker join --url {URL}" in calls
    assert "evo-agents worker service install" in calls
    assert (home / "seen-token").read_text(encoding="utf-8") == TOKEN  # from the environment, not the command line
    assert not any("evj_" in call for call in calls)
    assert TOKEN not in first.stdout + first.stderr
    assert not any(call.startswith(("npm install", "uv tool install")) for call in calls)  # all there already

    second = _run(data, env)
    assert second.returncode == 0, second.stderr
    again = _calls(home)[len(calls) :]
    assert not any(call.startswith(("evo-agents worker join", "evo-agents worker service")) for call in again)
    assert "not joining again" in second.stderr
    assert json.loads((home / ".evo" / "worker" / "config.json").read_text(encoding="utf-8")) == {}


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the script installs with apt and creates a user")
def test_a_machine_that_is_not_a_worker_needs_the_hub_and_a_token(machine):
    home, env = machine
    result = _run(SCRIPT, env)
    assert result.returncode == 1
    assert "EVO_JOIN_TOKEN" in result.stderr
    assert _calls(home) == []
    # a worker already: a run without the token installs what is missing and keeps its worker
    (home / ".evo" / "worker").mkdir(parents=True)
    (home / ".evo" / "worker" / "config.json").write_text("{}", encoding="utf-8")
    (home / ".evo" / "worker" / "token").write_text("evw_x\n", encoding="utf-8")
    kept = _run(SCRIPT, {**env, JOIN_TOKEN_ENV: ""})
    assert kept.returncode == 0, kept.stderr
    assert not any(call.startswith("evo-agents worker join") for call in _calls(home))


def test_pool_user_data_makes_a_token_for_the_pool_and_prints_the_script(monkeypatch, capsys):
    from evo_agents.cli import build_parser
    from evo_agents.worker import pool_cli

    calls = []

    class FakeHub:
        url = URL

        def call(self, method, path, body=None):
            calls.append((method, path, body))
            return {"id": 7, "pool": "vps", "token": TOKEN, "max_uses": 3, "expires_at": "2026-10-26T00:00:00Z"}

    monkeypatch.setattr(pool_cli, "_hub", FakeHub)
    args = build_parser().parse_args(["worker", "pool", "user-data", "vps", "--ttl", "48", "--max-nodes", "3"])
    assert args.func(args) == 0
    out, err = capsys.readouterr()
    assert calls == [("POST", "/v1/worker-pools/vps/tokens", {"hours": 48, "max_uses": 3})]
    assert out == render_user_data(URL, TOKEN)
    assert "token revoke vps 7" in err and TOKEN not in err
    with pytest.raises(SystemExit):
        build_parser().parse_args(["worker", "pool", "user-data", "vps", "--max-nodes", "101"])
