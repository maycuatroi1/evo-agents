"""Where a run's agent runs on this worker: the seam of the environment providers (``docs/workers.md``, Environments).

A run's claim names the environment its agent profile picked (``RunSpec.environment``, ``evo_agents.hub.profiles``):
none for the worker's own machine, or the declaration of a plugin's environment (``evo_agents.plugins.run_environments``),
a container image pinned by its digest. ``for_run`` gives the provider of each:

- ``Host``, the default: the agent's program on the agent's PATH, with the agent's environment whole, as every run ran
  before environments. Nothing changes for a run whose claim names no environment.
- ``Container``: the agent's program runs in a container of the image, through Docker or Podman (``find_engine``:
  ``EVO_WORKER_CONTAINER_ENGINE``, else docker, else podman on PATH), as this machine's user (``--user``, and
  ``--userns=keep-id`` on rootless Podman), with ``--init``, ``--rm``, ``-i``, every capability dropped, no new
  privileges, and ``--pull=never`` so it runs the image the preflight pulled by its digest. It mounts, each at its own
  path, the run's worktree (the agent's directory), the git directory of each checkout a worktree in it belongs to,
  so git works there, and a home of the run (``runs/<run>/container-home``, the container's HOME, which keeps the
  runtime's sessions for the run's next agent), the directory of each agent pack built for the run, read-only
  (``evo_agents.worker.packs``), then the declared mounts: a tmpfs, or a volume named
  ``evo-<plugin>-<name>``. The network is the declaration's: ``none``, or ``outbound``, the engine's default network,
  with nothing published but the port an agent serves the daemon on (opencode), bound to 127.0.0.1.

  Only the run's own variables go in (``passed``): those its leases set (env leases; the run's git configuration
  names a helper of this worker and stays out) and those the run, its runtime's adapter or the runtime's SDK set over
  the daemon's environment. The daemon's own environment (its PATH and HOME, the owner's tokens and keys) never does:
  the engine's client reads each value passed from its own environment (``-e NAME``), so no value is ever on a
  command line. A program of the runtime (claude, opencode, codex, an ACP agent) is the image's, found on its PATH.

The adapters build their command through ``Environment.program`` and ``Environment.command``, and a container's
helper (CONTAINER_EXEC) picks the variables when the runtime starts, from the environment its SDK gives it. The
containers of a run carry the labels of this worker and the run, so ``release`` removes what is left of them once an
agent ends, and the next daemon does after one that died (``remove_containers``, ``evo_agents.worker.orphans``).

``preflight`` checks, before the agent starts, what the run's preflight needs of the environment: a kind of run whose
agent does not talk to this daemon through ``evo-agents worker`` (CONTAINER_KINDS), headless, an engine that answers,
the image pulled by its digest, declared mounts clear of this worker's paths, and a network an opencode server can be
reached on. Each problem fails the run with cause environment before its agent starts. Standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from evo_agents.plugins.run_environments import HOST, ContainerEnvironment, Mount

ENGINE_VARIABLE = "EVO_WORKER_CONTAINER_ENGINE"  # docker, podman, or the path of either
ENGINES = ("docker", "podman")
CONTAINER_KINDS = ("step",)  # the kinds of run whose agent needs no `evo-agents worker` command of this daemon
HOME_DIR = "container-home"  # under runs/<run>: the container's HOME
PROBE_TIMEOUT = 30.0  # seconds for `<engine> info` and `<engine> image inspect`
PULL_TIMEOUT = 900.0  # seconds to pull an image
RELEASE_TIMEOUT = 30.0  # seconds to list and remove what is left of a run's containers
WORKER_LABEL = "dev.evo-agents.worker"  # a digest of this worker's home: two daemons of a machine keep apart
RUN_LABEL = "dev.evo-agents.run"
ENVIRONMENT_LABEL = "dev.evo-agents.environment"
LISTEN_HOST = "0.0.0.0"  # where a server in a container listens; the engine publishes the port on 127.0.0.1 alone
# Never passed into a container, whoever set them: the machine's own paths and sessions, the engine's client settings,
# the daemon's home, and the run's git configuration (WITHHELD_PREFIXES), which names a credential helper of this
# worker that the container does not have.
WITHHELD = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SHELL",
        "USER",
        "LOGNAME",
        "SSH_AUTH_SOCK",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "CONTAINER_HOST",
        "XDG_RUNTIME_DIR",
        "EVO_WORKER_HOME",
    }
)
WITHHELD_PREFIXES = ("GIT_CONFIG_",)
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The helper a container's command starts with: it reads the spec file the adapter's directory holds (the engine, the
# options of `run`, what comes after them and the variables withheld), passes on every variable of the environment it
# was given that is not withheld, by name, and executes the engine. Its own arguments after the spec go to the runtime.
CONTAINER_EXEC = (
    "import json, os, re, sys\n"
    "spec = json.load(open(sys.argv[1], encoding='utf-8'))\n"
    "name = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')\n"
    "held, prefixes = set(spec['withheld']), tuple(spec['prefixes'])\n"
    "passed = sorted(k for k in os.environ if name.fullmatch(k) and k not in held and not k.startswith(prefixes))\n"
    "names = [item for k in passed for item in ('-e', k)]\n"
    "argv = [spec['engine'], *spec['before'], *names, *spec['after'], *sys.argv[2:]]\n"
    "os.execv(argv[0], argv)\n"
)


class Environment:
    """The host: the worker's own machine, as every run ran before environments."""

    name = HOST
    container = False
    listen_host = "127.0.0.1"

    def program(self, binary: str, env: Mapping[str, str]) -> str:
        """The path of ``binary`` on the agent's PATH."""
        found = shutil.which(binary, path=env.get("PATH"))
        if found is None:
            raise RuntimeError(f"{binary} is not on PATH")
        return found

    def command(
        self,
        program: str,
        args: Sequence[str] = (),
        *,
        context=None,
        directory: Path | None = None,
        env: Mapping[str, str] | None = None,
        publish: int | None = None,
    ) -> list[str]:
        """The command that runs ``program args`` for the agent of ``context`` (an adapter's RunContext): as it is."""
        return [program, *args]

    def visible(self, context) -> Mapping[str, str]:
        """The variables the agent of ``context`` gets: all of its environment."""
        return context.env

    def note(self) -> str | None:
        """What the run's log says once of where the agent runs; nothing for the host."""
        return None

    def release(self) -> None:
        """Remove what is left of the run's containers once an agent ended; nothing on the host."""


HOST_ENVIRONMENT = Environment()


def find_engine(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(the path of the container engine, None), or (None, why there is none): ``EVO_WORKER_CONTAINER_ENGINE`` when set
    (docker, podman or a path), else docker, else podman, on ``env``'s PATH."""
    named = (env.get(ENGINE_VARIABLE) or "").strip()
    path = env.get("PATH")
    if named:
        found = shutil.which(named, path=path)
        return (found, None) if found else (None, f"{ENGINE_VARIABLE}={named} is not an executable on PATH")
    for engine in ENGINES:
        found = shutil.which(engine, path=path)
        if found:
            return found, None
    return None, "neither docker nor podman is on this worker's PATH: install one, or name it in " + ENGINE_VARIABLE


def is_podman(engine: str) -> bool:
    return "podman" in Path(engine).name


def worker_label(root: Path) -> str:
    """The value of WORKER_LABEL for the worker whose home is ``root``."""
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def remove_containers(engine: str, worker: str, run_id: int, env: Mapping[str, str] | None = None) -> int:
    """Remove every container of run ``run_id`` of the worker labelled ``worker`` that is left, running or not; how
    many. A failure of the engine is no failure of the caller: it removes none."""
    filters = ["--filter", f"label={WORKER_LABEL}={worker}", "--filter", f"label={RUN_LABEL}={int(run_id)}"]
    listed = _engine(engine, ["ps", "-aq", *filters], env, RELEASE_TIMEOUT)
    ids = listed.stdout.split() if listed is not None and listed.returncode == 0 else []
    if ids:
        _engine(engine, ["rm", "-f", *ids], env, RELEASE_TIMEOUT)
    return len(ids)


def _engine(engine: str, args: list[str], env: Mapping[str, str] | None, timeout: float):
    """``engine args`` with stdin /dev/null, or None when it did not run or answer within ``timeout``."""
    try:
        return subprocess.run(
            [engine, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=None if env is None else dict(env),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _tail(text: str, limit: int = 300) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else "..." + text[-limit:]


def _within(path: str, other: str) -> bool:
    """Whether ``path`` and ``other`` are the same directory or one holds the other."""
    a, b = path.rstrip("/") + "/", other.rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


def git_dirs(worktree: Path) -> list[Path]:
    """The git directory of each checkout a worktree in ``worktree`` (it, or a directory right under it) belongs to:
    the common directory its ``.git`` file leads to, outside ``worktree``; each once, in order."""
    found: list[Path] = []
    candidates = [worktree]
    with contextlib.suppress(OSError):
        candidates += sorted(path for path in worktree.iterdir() if path.is_dir())
    for directory in candidates:
        common = _common_dir(directory / ".git")
        if common is not None and not _within(str(common), str(worktree)) and common not in found:
            found.append(common)
    return found


def _common_dir(dot_git: Path) -> Path | None:
    """The common git directory a worktree's ``.git`` file names (gitdir, then its commondir); None for none."""
    try:
        if not dot_git.is_file():
            return None
        text = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = (dot_git.parent / text.partition(":")[2].strip()).resolve()
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return gitdir
    return (gitdir / common).resolve()


class Container(Environment):
    """A plugin's environment: the agent's program runs in a container of its image, through the engine."""

    container = True
    listen_host = LISTEN_HOST

    def __init__(
        self,
        declaration: ContainerEnvironment,
        *,
        run_id: int,
        home: Path,
        worker: str,
        base: Mapping[str, str],
    ):
        self.declaration = declaration
        self.name = declaration.environment
        self.run_id = int(run_id)
        self.home = home  # the container's HOME, kept for the run
        self.worker = worker  # WORKER_LABEL's value
        self.base = base  # the daemon's environment, whose variables the container never gets
        self.engine: str | None = None  # found by the preflight, or by the first command

    # What the agent gets

    def program(self, binary: str, env: Mapping[str, str]) -> str:
        """``binary`` as the image's PATH finds it: its name, as the declaration of the runtime names it."""
        return binary

    def withheld(self, env: Mapping[str, str], leased: Iterable[str]) -> list[str]:
        """The variables of the daemon's environment (and of this process's) that the run did not set itself in
        ``env`` and that no lease set (``leased``), with WITHHELD: none of them goes into the container."""
        leased = set(leased)
        inherited = set(self.base) | set(os.environ)
        own = {key for key, value in env.items() if self.base.get(key, os.environ.get(key)) != value} | leased
        return sorted((inherited - own) | WITHHELD)

    def passed(self, env: Mapping[str, str], leased: Iterable[str]) -> list[str]:
        """The names of ``env`` that go into the container (``withheld``, WITHHELD_PREFIXES)."""
        held = set(self.withheld(env, leased))
        return sorted(
            key for key in env if _VARIABLE.fullmatch(key) and key not in held and not key.startswith(WITHHELD_PREFIXES)
        )

    def visible(self, context) -> Mapping[str, str]:
        return {key: context.env[key] for key in self.passed(context.env, context.leased)}

    def note(self) -> str:
        d = self.declaration
        mounts = "".join(f", {mount.type} at {mount.target}" for mount in d.mounts)
        return (
            f"The agent runs in environment {d.environment} of plugin {d.plugin} {d.version}: image {d.image}, network "
            f"{d.network}, through {Path(self.engine or 'the engine').name}. It gets the run's worktree, the git "
            f"directories of its checkouts and a home of the run{mounts}; of variables, only those the run's leases "
            "and the run itself set, none of the worker's own environment."
        )

    # The command

    def mounts(self, worktree: Path, read_only: Sequence[Path] = ()) -> list[str]:
        """The engine's options of the mounts: the run's worktree, its checkouts' git directories and the run's home,
        each at its own path, then ``read_only`` (the directories of the run's agent packs) at theirs, read-only, then
        the declared ones."""
        options: list[str] = []
        for path in (worktree, *git_dirs(worktree), self.home):
            options += ["--mount", f"type=bind,src={path},dst={path}"]
        for path in read_only:
            options += ["--mount", f"type=bind,src={path},dst={path},readonly"]
        for mount in self.declaration.mounts:
            options += self._declared(mount)
        return options

    def _declared(self, mount: Mount) -> list[str]:
        if mount.type == "tmpfs":
            settings = ["ro" if mount.read_only else "rw", *([f"size={mount.size_mb}m"] if mount.size_mb else [])]
            return ["--tmpfs", f"{mount.target}:{','.join(settings)}"]
        name = self.declaration.volume_name(mount)
        return ["--mount", f"type=volume,src={name},dst={mount.target}{',readonly' if mount.read_only else ''}"]

    def run_options(self, worktree: Path, cwd: Path, publish: int | None, read_only: Sequence[Path] = ()) -> list[str]:
        """The options of ``<engine> run`` before the variables, for an agent working in ``cwd``; ``read_only`` as
        ``mounts`` takes it."""
        engine = self.engine or ""
        user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
        if is_podman(engine) and hasattr(os, "getuid") and os.getuid() != 0:
            user = ["--userns=keep-id", *user]
        network = ["--network", "none"] if self.declaration.network == "none" else []
        ports = ["--publish", f"127.0.0.1:{publish}:{publish}"] if publish else []
        labels = [f"{WORKER_LABEL}={self.worker}", f"{RUN_LABEL}={self.run_id}", f"{ENVIRONMENT_LABEL}={self.name}"]
        return [
            "run",
            "--rm",
            "-i",
            "--init",
            "--pull=never",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            *user,
            *network,
            *ports,
            *(item for label in labels for item in ("--label", label)),
            *self.mounts(worktree, read_only),
            "--workdir",
            str(cwd),
            "--env",
            f"HOME={self.home}",
        ]

    def command(
        self,
        program: str,
        args: Sequence[str] = (),
        *,
        context=None,
        directory: Path | None = None,
        env: Mapping[str, str] | None = None,
        publish: int | None = None,
    ) -> list[str]:
        """The command that runs ``program args`` in a container for the agent of ``context``: CONTAINER_EXEC, with
        its spec written to ``directory``, the adapter's own; ``env`` is what the adapter gives the runtime (the
        context's environment when None), ``publish`` the port a server of the agent listens on."""
        if self.engine is None:
            self.engine, why = find_engine(self.base)
            if self.engine is None:
                raise RuntimeError(why)
        if context is None or directory is None:
            raise ValueError("a container's command needs the run's context and the adapter's directory")
        env = context.env if env is None else env
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        worktree = Path(context.worktree)
        spec = {
            "engine": self.engine,
            "before": self.run_options(
                worktree, worktree, publish, [pack.root for pack in getattr(context, "packs", ())]
            ),
            "after": [self.declaration.image, program],
            "withheld": self.withheld(env, context.leased),
            "prefixes": list(WITHHELD_PREFIXES),
        }
        path = Path(directory) / "container.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(spec, handle)
        return [sys.executable or "python3", "-I", "-c", CONTAINER_EXEC, str(path), *args]

    def release(self) -> None:
        if self.engine is not None:
            remove_containers(self.engine, self.worker, self.run_id, self.base)

    # The preflight

    def preflight(self, *, kind: str, mode: str, runtime: str, core_paths: Iterable[Path]) -> list[str]:
        """Why the run's agent cannot run in this environment here, each a sentence; empty when it can. It finds the
        engine, asks it to answer, and pulls the image by its digest when the engine does not have it."""
        d = self.declaration
        where = f"environment {d.environment} of plugin {d.plugin}"
        problems = []
        if kind not in CONTAINER_KINDS:
            problems.append(
                f"{where} runs the agent in a container, which takes {', '.join(CONTAINER_KINDS)} runs: the agent of a "
                f"{kind} run talks to this worker's daemon through `evo-agents worker`, which a container does not "
                "have; run it under an agent whose environment axis allows host"
            )
        if mode != "headless":
            problems.append(
                f"{where} runs the agent in a container, and a person drives a run in a terminal of the host"
            )
        if d.network == "none" and runtime == "opencode":
            problems.append(f"{where} has network none, and the daemon reaches opencode's server over a port")
        problems += self._clear_of(core_paths)
        if problems:
            return problems
        return self._engine_problems(where)

    def _clear_of(self, core_paths: Iterable[Path]) -> list[str]:
        found = []
        paths = [str(path) for path in core_paths]
        for path in paths:
            if "," in path:
                found.append(f"this worker's path {path} holds a comma, which a container's mount cannot name")
        for mount in self.declaration.mounts:
            clash = next((path for path in paths if _within(mount.target, path)), None)
            if clash:
                found.append(f"environment {self.name} mounts {mount.target}, which is or holds this worker's {clash}")
        return found

    def _engine_problems(self, where: str) -> list[str]:
        engine, why = find_engine(self.base)
        if engine is None:
            return [f"{where} needs a container engine: {why}"]
        self.engine = engine
        name = Path(engine).name
        answered = _engine(engine, ["info"], self.base, PROBE_TIMEOUT)
        if answered is None or answered.returncode != 0:
            said = "no answer in time" if answered is None else _tail(answered.stderr or answered.stdout)
            return [f"{where} needs a container engine, and `{name} info` failed: {said}"]
        image = self.declaration.image
        present = _engine(engine, ["image", "inspect", image], self.base, PROBE_TIMEOUT)
        if present is not None and present.returncode == 0:
            return []
        pulled = _engine(engine, ["pull", image], self.base, PULL_TIMEOUT)
        if pulled is None or pulled.returncode != 0:
            said = f"no answer within {PULL_TIMEOUT:g}s" if pulled is None else _tail(pulled.stderr or pulled.stdout)
            return [f"{where}: `{name} pull {image}` failed: {said}"]
        return []


def for_run(spec: Mapping, *, home_dir: Path, worker: str, base: Mapping[str, str]) -> Environment:
    """The environment of the run whose claim is ``spec``: the host when it names none, else the container of its
    declaration, whose home is ``home_dir``; ValueError when the claim names one this worker does not read."""
    data = spec.get("environment") if isinstance(spec, Mapping) else None
    if data is None:
        return HOST_ENVIRONMENT
    declaration = ContainerEnvironment.from_json(data)
    return Container(declaration, run_id=int(spec["id"]), home=home_dir, worker=worker, base=base)
