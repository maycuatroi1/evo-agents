"""Where a run's agent runs on the worker (``evo_agents.worker.environments``), as criterion a7 of the plan
hub-agent-plugins names it: the host is the default and changes no command; a plugin's environment runs the agent's
program in a container of its image, pinned by its digest, with the run's worktree, its checkouts' git directories and
a home of the run mounted at their own paths, and only the run's own variables passed in, by name; the preflight finds
the engine, asks it to answer and pulls the image, and a worker without what the run needs fails the run before its
agent starts with cause environment.

Most tests drive a fake engine (FAKE_ENGINE, a script named docker) that answers info, image inspect, pull, ps and rm,
and runs ``run`` on this machine with nothing but the variables its command names, as a container would. The last
test runs a real container when this machine has Docker or Podman that answers, and skips otherwise."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from evo_agents.plugins.run_environments import ContainerEnvironment
from evo_agents.worker import environments, orphans
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.environments import (
    CONTAINER_EXEC,
    ENGINE_VARIABLE,
    HOST_ENVIRONMENT,
    Container,
    find_engine,
    for_run,
    git_dirs,
    remove_containers,
    worker_label,
)

DIGEST = "sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"  # busybox 1.37, every platform
IMAGE = f"docker.io/library/busybox:1.37@{DIGEST}"
DECLARATION = {
    "environment": "sandbox/box",
    "plugin": "sandbox",
    "version": "0.3.0",
    "title": "Agent box",
    "provider": "container",
    "image": IMAGE,
    "network": "outbound",
    "mounts": [
        {"type": "tmpfs", "target": "/scratch", "read_only": False, "size_mb": 64},
        {"type": "volume", "target": "/cache", "name": "cache", "read_only": True},
    ],
}
OWNER_SECRET = "owner-secret-" + "7" * 16  # in the daemon's environment: never in a container
LEASED_VALUE = "leased-" + "3" * 20
# A container engine for the tests: what it was asked goes to FAKE_ENGINE_LOG, one JSON line each. The image is there
# when the file FAKE_ENGINE_IMAGES names exists, which a pull makes. `run` executes the program on this machine in its
# --workdir, with the variables its -e and --env name and nothing else of the client's, plus PATH and the FAKE_ACP_
# settings of the fake ACP agent, as an image would bake them in.
FAKE_ENGINE = r"""
import json, os, sys
args = sys.argv[1:]
def note(**extra):
    with open(os.environ["FAKE_ENGINE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"args": args, **extra}) + "\n")
command = args[0] if args else ""
if command == "info":
    note()
    code = int(os.environ.get("FAKE_ENGINE_INFO_EXIT", "0"))
    if code:
        print("Cannot connect to the Docker daemon", file=sys.stderr)
    sys.exit(code)
if command == "image":
    note()
    sys.exit(0 if os.path.exists(os.environ["FAKE_ENGINE_IMAGES"]) else 1)
if command == "pull":
    note()
    if os.environ.get("FAKE_ENGINE_PULL_EXIT", "0") != "0":
        print("pull access denied", file=sys.stderr)
        sys.exit(1)
    open(os.environ["FAKE_ENGINE_IMAGES"], "w").close()
    sys.exit(0)
if command == "ps":
    note()
    print(os.environ.get("FAKE_ENGINE_LEFT", ""))
    sys.exit(0)
if command == "rm":
    note()
    sys.exit(0)
if command == "run":
    valued = {"--user", "--network", "--publish", "--label", "--mount", "--tmpfs", "--workdir", "--env", "-e"}
    index, env, cwd = 1, {}, "/"
    while args[index].startswith("-"):
        flag = args[index]
        if flag not in valued:
            index += 1
            continue
        value = args[index + 1]
        index += 2
        if flag in ("--env", "-e"):
            key, sep, given = value.partition("=")
            if sep:
                env[key] = given
            elif key in os.environ:
                env[key] = os.environ[key]
        elif flag == "--workdir":
            cwd = value
    image, program, rest = args[index], args[index + 1], args[index + 2:]
    env.update({key: value for key, value in os.environ.items() if key.startswith("FAKE_ACP_")})
    env.setdefault("PATH", os.defpath)
    note(image=image, program=program, env=sorted(env), cwd=cwd)
    os.chdir(cwd)
    os.execve(program, [program, *rest], env)
note()
sys.exit(2)
"""


class Engine:
    """The fake engine of a test: its program (named docker), its log and its file of images."""

    def __init__(self, tmp: Path):
        bin_dir = tmp / "engine-bin"
        bin_dir.mkdir(exist_ok=True)
        self.path = bin_dir / "docker"
        self.path.write_text(f"#!{sys.executable}\n{FAKE_ENGINE}", encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self.log = tmp / "engine.jsonl"
        self.images = tmp / "engine-images"
        self.env = {
            ENGINE_VARIABLE: str(self.path),
            "FAKE_ENGINE_LOG": str(self.log),
            "FAKE_ENGINE_IMAGES": str(self.images),
        }

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def asked(self) -> list[str]:
        return [call["args"][0] for call in self.calls()]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def checkout_with_worktree(tmp: Path) -> tuple[Path, Path]:
    """A checkout with one commit, and a worktree of it on a branch: (checkout, worktree)."""
    checkout = tmp / "checkout"
    checkout.mkdir()
    git("init", "--quiet", "--initial-branch", "main", cwd=checkout)
    (checkout / "README.md").write_text("# demo\n", encoding="utf-8")
    git("add", "README.md", cwd=checkout)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "first", cwd=checkout)
    worktree = tmp / "worker" / "worktrees" / "demo-7"
    git("worktree", "add", "--quiet", "-b", "feat/x", str(worktree), cwd=checkout)
    return checkout, worktree


def container(tmp: Path, base: dict, **changes) -> Container:
    declaration = ContainerEnvironment.from_json({**DECLARATION, **changes})
    return Container(
        declaration, run_id=7, home=tmp / "worker" / "runs" / "7" / "container-home", worker="w1", base=base
    )


def context_of(environment, worktree: Path, env: dict, leased=()) -> RunContext:
    return RunContext(
        run={"id": 7}, worktree=worktree, prompt="", env=env, leased=frozenset(leased), environment=environment
    )


# The host and the choice of a provider


def test_environment_host_is_the_default_and_changes_no_command(tmp_path):
    assert for_run({"id": 7}, home_dir=tmp_path, worker="w1", base={}) is HOST_ENVIRONMENT
    assert for_run({"id": 7, "environment": None}, home_dir=tmp_path, worker="w1", base={}) is HOST_ENVIRONMENT
    context = RunContext(run={"id": 7}, worktree=tmp_path, prompt="", env={"PATH": os.defpath})
    assert context.environment is HOST_ENVIRONMENT and not HOST_ENVIRONMENT.container
    assert HOST_ENVIRONMENT.command("/bin/claude", ["--x"], context=context, directory=tmp_path) == [
        "/bin/claude",
        "--x",
    ]
    assert HOST_ENVIRONMENT.visible(context) is context.env and HOST_ENVIRONMENT.note() is None
    assert HOST_ENVIRONMENT.program("sh", {"PATH": os.defpath}) == "/bin/sh"
    with pytest.raises(RuntimeError, match="no-such-runtime is not on PATH"):
        HOST_ENVIRONMENT.program("no-such-runtime", {"PATH": os.defpath})
    boxed = for_run({"id": 7, "environment": DECLARATION}, home_dir=tmp_path / "home", worker="w1", base={})
    assert isinstance(boxed, Container) and boxed.container and boxed.name == "sandbox/box"
    assert boxed.program("claude", {"PATH": ""}) == "claude", "the image's PATH finds the runtime's program"
    with pytest.raises(ValueError, match="pinned by its digest"):
        for_run(
            {"id": 7, "environment": {**DECLARATION, "image": "busybox:latest"}},
            home_dir=tmp_path,
            worker="w1",
            base={},
        )


def test_environment_engine_is_the_one_named_else_docker_else_podman(tmp_path):
    engine = Engine(tmp_path)
    assert find_engine({ENGINE_VARIABLE: str(engine.path)}) == (str(engine.path), None)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text("#!/bin/sh\n", encoding="utf-8")
    podman.chmod(0o755)
    assert find_engine({"PATH": str(bin_dir)}) == (str(podman), None)
    assert environments.is_podman(str(podman)) and not environments.is_podman(str(engine.path))
    assert find_engine({"PATH": str(tmp_path / "nothing")})[1].startswith("neither docker nor podman is on")
    assert (
        find_engine({ENGINE_VARIABLE: "nope", "PATH": ""})[1] == f"{ENGINE_VARIABLE}=nope is not an executable on PATH"
    )
    assert worker_label(tmp_path) == worker_label(tmp_path / ".") != worker_label(tmp_path / "other")


# What goes into a container


def test_environment_container_passes_only_the_runs_own_variables(tmp_path):
    base = {"PATH": "/usr/bin", "HOME": "/Users/me", "OWNER_SECRET": OWNER_SECRET, "SHARED": "same"}
    env = {
        **base,
        "EVO_RUN_ID": "7",  # the run's own
        "FAKE_API_KEY": LEASED_VALUE,  # a lease's
        "SHARED": "same",  # a lease's too, though the daemon has the same value
        "GIT_CONFIG_COUNT": "1",  # the run's git configuration names a helper of this worker
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!evo-agents worker git-credential --run 7",
        "EVO_WORKER_HOME": "/Users/me/.evo-worker",
    }
    leased = {"FAKE_API_KEY", "SHARED", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}
    box = container(tmp_path, base)
    assert box.passed(env, leased) == ["EVO_RUN_ID", "FAKE_API_KEY", "SHARED"]
    held = set(box.withheld(env, leased))
    assert {"PATH", "HOME", "OWNER_SECRET", "EVO_WORKER_HOME"} <= held and not {"FAKE_API_KEY", "SHARED"} & held
    seen = box.visible(context_of(box, tmp_path, env, leased))
    assert seen == {"EVO_RUN_ID": "7", "FAKE_API_KEY": LEASED_VALUE, "SHARED": "same"}


def test_environment_container_command_runs_the_image_by_digest_with_its_mounts_and_labels(tmp_path):
    engine = Engine(tmp_path)
    checkout, worktree = checkout_with_worktree(tmp_path)
    base = {"PATH": os.defpath, "OWNER_SECRET": OWNER_SECRET, **engine.env}
    box = container(tmp_path, base, network="none")
    directory = tmp_path / "adapter"
    directory.mkdir()
    context = context_of(box, worktree, {**base, "EVO_RUN_ID": "7"})
    command = box.command("claude", ["--print"], context=context, directory=directory)
    spec_path = directory / "container.json"
    assert command == [sys.executable, "-I", "-c", CONTAINER_EXEC, str(spec_path), "--print"]
    assert stat.S_IMODE(spec_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(box.home.stat().st_mode) == 0o700
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["engine"] == str(engine.path) and spec["after"] == [IMAGE, "claude"]
    assert {"OWNER_SECRET", "PATH", "FAKE_ENGINE_LOG"} <= set(spec["withheld"]) and "EVO_RUN_ID" not in spec["withheld"]
    before = spec["before"]
    assert before[:7] == [
        "run",
        "--rm",
        "-i",
        "--init",
        "--pull=never",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ]
    assert before[before.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert before[before.index("--network") + 1] == "none"
    labels = [before[i + 1] for i, item in enumerate(before) if item == "--label"]
    assert labels == ["dev.evo-agents.worker=w1", "dev.evo-agents.run=7", "dev.evo-agents.environment=sandbox/box"]
    mounts = [before[i + 1] for i, item in enumerate(before) if item == "--mount"]
    common = (checkout / ".git").resolve()
    assert mounts == [
        f"type=bind,src={worktree},dst={worktree}",
        f"type=bind,src={common},dst={common}",
        f"type=bind,src={box.home},dst={box.home}",
        "type=volume,src=evo-sandbox-cache,dst=/cache,readonly",
    ]
    assert before[before.index("--tmpfs") + 1] == "/scratch:rw,size=64m"
    assert before[-4:] == ["--workdir", str(worktree), "--env", f"HOME={box.home}"]
    published = box.command("opencode", ["serve"], context=context, directory=directory, publish=4096)
    assert published[-1] == "serve"
    again = json.loads(spec_path.read_text(encoding="utf-8"))["before"]
    assert again[again.index("--publish") + 1] == "127.0.0.1:4096:4096"


def test_environment_container_hands_the_engine_names_never_values(tmp_path):
    engine = Engine(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    base = {"PATH": os.defpath, "OWNER_SECRET": OWNER_SECRET, **engine.env}
    box = container(tmp_path, base)
    env = {**base, "EVO_RUN_ID": "7", "FAKE_API_KEY": LEASED_VALUE}
    context = context_of(box, worktree, env, {"FAKE_API_KEY"})
    directory = tmp_path / "adapter"
    directory.mkdir()
    code = "import json, os; print(json.dumps({'env': dict(os.environ), 'cwd': os.getcwd()}))"
    command = box.command(sys.executable, ["-c", code], context=context, directory=directory)
    done = subprocess.run(command, env=env, cwd=worktree, capture_output=True, text=True, timeout=60, check=True)
    seen = json.loads(done.stdout)
    assert seen["cwd"] == str(worktree.resolve()) or seen["cwd"] == str(worktree)
    assert {key: seen["env"][key] for key in ("EVO_RUN_ID", "FAKE_API_KEY", "HOME")} == {
        "EVO_RUN_ID": "7",
        "FAKE_API_KEY": LEASED_VALUE,
        "HOME": str(box.home),
    }
    assert "OWNER_SECRET" not in seen["env"] and "FAKE_ENGINE_LOG" not in seen["env"]
    (run_call,) = [call for call in engine.calls() if call["args"][0] == "run"]
    assert run_call["image"] == IMAGE and run_call["program"] == sys.executable
    flat = " ".join(run_call["args"])
    assert "-e EVO_RUN_ID" in flat and "-e FAKE_API_KEY" in flat
    assert LEASED_VALUE not in flat and OWNER_SECRET not in flat, "a value is never on the engine's command line"


def test_environment_git_dirs_are_the_checkouts_a_worktree_belongs_to(tmp_path):
    checkout, worktree = checkout_with_worktree(tmp_path)
    assert git_dirs(worktree) == [(checkout / ".git").resolve()]
    plan_dir = tmp_path / "plan-run"
    plan_dir.mkdir()
    assert git_dirs(plan_dir) == []
    assert git_dirs(checkout) == [], "a checkout's own .git is inside it"


# The preflight


def test_environment_preflight_names_what_a_container_cannot_take(tmp_path):
    engine = Engine(tmp_path)
    box = container(tmp_path, {"PATH": os.defpath, **engine.env}, network="none")
    home = tmp_path / "worker"
    said = box.preflight(kind="plan", mode="interactive", runtime="opencode", core_paths=[home])
    assert len(said) == 3
    assert "runs the agent in a container, which takes step runs: the agent of a plan run talks" in said[0]
    assert "a person drives a run in a terminal of the host" in said[1]
    assert "has network none, and the daemon reaches opencode's server over a port" in said[2]
    clash = container(tmp_path, {"PATH": os.defpath, **engine.env}, mounts=[{"type": "tmpfs", "target": str(home)}])
    (problem,) = clash.preflight(kind="step", mode="headless", runtime="claude-code", core_paths=[home / "runs"])
    assert problem == f"environment sandbox/box mounts {home}, which is or holds this worker's {home / 'runs'}"
    assert engine.calls() == [], "nothing is asked of the engine for a run it would refuse anyway"


def test_environment_preflight_finds_the_engine_asks_it_and_pulls_the_image_by_its_digest(tmp_path):
    engine = Engine(tmp_path)
    where = "environment sandbox/box of plugin sandbox"

    def preflight(**env) -> list[str]:
        box = container(tmp_path, {"PATH": str(tmp_path / "empty"), **env})
        return box.preflight(kind="step", mode="headless", runtime="claude-code", core_paths=[tmp_path / "worker"])

    assert preflight() == [
        f"{where} needs a container engine: neither docker nor podman is on this worker's PATH: install one, or name "
        f"it in {ENGINE_VARIABLE}"
    ]
    assert preflight(**{ENGINE_VARIABLE: "nope"}) == [
        f"{where} needs a container engine: {ENGINE_VARIABLE}=nope is not an executable on PATH"
    ]
    assert preflight(**engine.env, FAKE_ENGINE_INFO_EXIT="1") == [
        f"{where} needs a container engine, and `docker info` failed: Cannot connect to the Docker daemon"
    ]
    assert preflight(**engine.env, FAKE_ENGINE_PULL_EXIT="1") == [
        f"{where}: `docker pull {IMAGE}` failed: pull access denied"
    ]
    assert preflight(**engine.env) == [] and engine.images.exists()
    assert engine.asked() == ["info", "info", "image", "pull", "info", "image", "pull"]
    assert preflight(**engine.env) == []
    assert engine.asked()[-2:] == ["info", "image"], "an image it has is not pulled again"
    pulled = [call["args"] for call in engine.calls() if call["args"][0] == "pull"]
    assert pulled[-1] == ["pull", IMAGE]


# Releasing


def test_environment_release_removes_the_runs_containers_left_and_the_next_daemon_does_too(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    env = {**os.environ, **engine.env, "FAKE_ENGINE_LEFT": "abc123 def456"}
    assert remove_containers(str(engine.path), "w1", 7, env) == 2
    ps, rm = engine.calls()
    assert ps["args"] == [
        "ps",
        "-aq",
        "--filter",
        "label=dev.evo-agents.worker=w1",
        "--filter",
        "label=dev.evo-agents.run=7",
    ]
    assert rm["args"] == ["rm", "-f", "abc123", "def456"]
    box = container(tmp_path, env)
    box.release()
    assert len(engine.calls()) == 2, "a container that never ran has no engine to ask"
    box.engine = str(engine.path)
    box.release()
    assert engine.asked()[-2:] == ["ps", "rm"]

    for key, value in {**engine.env, "FAKE_ENGINE_LEFT": "abc123"}.items():
        monkeypatch.setenv(key, value)
    agent = {"pid": None, "pgid": None, "containers": {"worker": "w1", "run": 7, "environment": "sandbox/box"}}
    said = asyncio.run(orphans.stop(agent))
    assert said == "nothing to stop: no process group was noted; 1 container(s) of environment sandbox/box removed"
    assert asyncio.run(orphans.stop({"pid": None, "pgid": None})) == "nothing to stop: no process group was noted"


# A real container


def _usable_engine() -> str | None:
    engine, _ = find_engine(os.environ)
    if engine is None or os.environ.get("EVO_TEST_NO_CONTAINERS"):
        return None
    try:
        answered = subprocess.run([engine, "info"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return engine if answered.returncode == 0 else None


def test_environment_container_runs_in_a_real_engine_with_its_worktree_and_only_the_runs_variables(tmp_path):
    engine = _usable_engine()
    if engine is None:
        pytest.skip("no Docker or Podman that answers on this machine")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    base = {**os.environ, "OWNER_SECRET": OWNER_SECRET}
    box = container(tmp_path, base, network="none", mounts=[{"type": "tmpfs", "target": "/scratch", "size_mb": 8}])
    problems = box.preflight(kind="step", mode="headless", runtime="claude-code", core_paths=[tmp_path / "worker"])
    if any("pull" in problem for problem in problems):
        pytest.skip(f"the image could not be pulled here: {problems}")
    assert problems == []
    env = {**base, "EVO_RUN_ID": "7", "LEASED": LEASED_VALUE}
    context = context_of(box, worktree, env, {"LEASED"})
    directory = tmp_path / "adapter"
    directory.mkdir()
    script = (
        "env > env.txt; id -u > uid.txt; pwd > pwd.txt; touch made.txt; "
        "echo ok > /scratch/x && echo tmpfs > tmpfs.txt; ls /sys/class/net > net.txt"
    )
    command = box.command("sh", ["-c", script], context=context, directory=directory)
    try:
        done = subprocess.run(command, env=env, cwd=worktree, capture_output=True, text=True, timeout=180)
    finally:
        box.release()
    assert done.returncode == 0, done.stderr
    seen = dict(line.split("=", 1) for line in (worktree / "env.txt").read_text().splitlines() if "=" in line)
    assert seen["LEASED"] == LEASED_VALUE and seen["EVO_RUN_ID"] == "7" and seen["HOME"] == str(box.home)
    assert "OWNER_SECRET" not in seen and seen.get("PATH") != os.environ.get("PATH")
    assert (worktree / "uid.txt").read_text().strip() == str(os.getuid())
    assert (worktree / "pwd.txt").read_text().strip() == str(worktree)
    assert (worktree / "made.txt").stat().st_uid == os.getuid(), "files the agent makes are the owner's"
    assert (worktree / "tmpfs.txt").read_text().strip() == "tmpfs"
    interfaces = (worktree / "net.txt").read_text().split()
    assert "lo" in interfaces and "eth0" not in interfaces, "network none: loopback, and no interface out"
    left = subprocess.run(
        [engine, "ps", "-aq", "--filter", "label=dev.evo-agents.worker=w1", "--filter", "label=dev.evo-agents.run=7"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert left.stdout.split() == [], "the run's containers are gone"


# The adapters, in a container


def _boxed(adapter, tmp_path: Path, engine: Engine):
    """``adapter`` whose run's agent runs in the container of DECLARATION, through ``engine``."""
    base = {**adapter.context.env, **engine.env, "OWNER_SECRET": OWNER_SECRET}
    box = container(tmp_path, base)
    adapter.context = replace(adapter.context, env=base, environment=box)
    return box


def test_environment_claude_code_and_codex_start_their_cli_in_the_container(tmp_path):
    from tests.worker.test_runtimes import _claude, _codex, _codex_samples, _collect, _samples

    engine = Engine(tmp_path)
    seen = {}
    for name in ("claude", "codex"):
        (tmp_path / name).mkdir()
    claude = _claude(tmp_path / "claude", _samples)
    _boxed(claude, tmp_path / "claude", engine)
    original = claude.make_client

    def make_client(options):
        launcher = Path(options.cli_path).read_text(encoding="utf-8")
        seen["claude"] = (launcher, json.loads((Path(options.cli_path).parent / "container.json").read_text()))
        return original(options)

    claude.make_client = make_client
    events, outcome = asyncio.run(_collect(claude))
    assert outcome.completed, outcome.error
    launcher, spec = seen["claude"]
    assert "container.json" in launcher and launcher.rstrip().endswith('"$@"')
    assert spec["after"] == [IMAGE, "claude"] and "OWNER_SECRET" in spec["withheld"]

    codex = _codex(tmp_path / "codex", _codex_samples)
    _boxed(codex, tmp_path / "codex", engine)
    original_codex = codex.make_codex

    def make_codex(config):
        spec = json.loads(Path(config.launch_args_override[-4]).read_text(encoding="utf-8"))
        seen["codex"] = (config.launch_args_override, spec)
        return original_codex(config)

    codex.make_codex = make_codex
    events, outcome = asyncio.run(_collect(codex))
    assert outcome.completed, outcome.error
    launch, spec = seen["codex"]
    assert launch[-3:] == ("app-server", "--listen", "stdio://") and CONTAINER_EXEC in launch
    assert spec["after"] == [IMAGE, "codex"]


def test_environment_opencode_serves_from_the_container_on_the_workers_loopback(tmp_path):
    from evo_agents.worker.runtimes import opencode as opencode_module
    from tests.worker.test_runtimes import _script, needs

    needs("aiohttp")
    engine = Engine(tmp_path)
    program = _script(
        tmp_path / "opencode",
        "import sys, time\n"
        "port = sys.argv[sys.argv.index('--port') + 1]\n"
        "host = sys.argv[sys.argv.index('--hostname') + 1]\n"
        "print(f'opencode server listening on http://{host}:{port}', flush=True)\n"
        "time.sleep(600)\n",
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    base = {"PATH": os.defpath, "OWNER_SECRET": OWNER_SECRET, **engine.env}
    box = container(tmp_path, base)
    context = context_of(box, worktree, {**base, "EVO_RUN_ID": "7"})
    directory = tmp_path / "adapter"
    directory.mkdir()

    async def go():
        server = await opencode_module.start_server(
            str(program), str(worktree), context.env, context=context, directory=directory
        )
        await server.stop()
        return server

    server = asyncio.run(go())
    (run_call,) = [call for call in engine.calls() if call["args"][0] == "run"]
    args = run_call["args"]
    port = args[args.index("--port") + 1]
    assert args[args.index("--hostname") + 1] == "0.0.0.0", "it listens on the container's every address"
    assert args[args.index("--publish") + 1] == f"127.0.0.1:{port}:{port}", "published on the worker's loopback alone"
    assert server.url == f"http://127.0.0.1:{port}"
    assert "OPENCODE_SERVER_PASSWORD" in run_call["env"] and "OWNER_SECRET" not in run_call["env"]
