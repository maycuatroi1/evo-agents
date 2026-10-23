"""``evo-agents worker browser install``: the Chromium the daemon starts for a run dispatched with browser (plan
agent-browser, criterion a3), downloaded into this user's cache, no sudo.

It is Playwright's build of Chromium, pinned with Playwright (``PLAYWRIGHT``): ``npx -y playwright@<pin> install
--no-shell --no-remove chromium`` puts it in Playwright's cache ($PLAYWRIGHT_BROWSERS_PATH, else
~/Library/Caches/ms-playwright on macOS and ~/.cache/ms-playwright on Linux): the full binary, not the headless shell
(spike decision 11), and the browsers of other versions of Playwright stay. On Linux, when ``ldd`` finds libraries
Chromium lacks, the packages that hold them are fetched with ``apt-get download`` and unpacked with ``dpkg -x`` under
the worker's browser directory, which Chromium then finds on LD_LIBRARY_PATH (decision 10). The install starts Chromium
once as a run does (``chromium.start``): with its own sandbox, else with --no-sandbox where this machine does not let
it make one (decision 1). It writes what it found to ``browser/state.json`` in the worker's home: the binary, its
version, the sandbox, the libraries, and how many browsers the worker holds at once, ``--max`` or else one per core
but one and per 1.5 GiB of memory available (``default_max``: 3 on a machine of 4 cores and 7 GiB, 4 on a larger one
with 6 GiB free, as the spike measured; decision 5).

Running it again changes nothing once state.json says what it would write: the binary is where it says, and no other
max is asked. The daemon reads state.json at each look at its runtimes (``report``): while the binary is there, its
heartbeat lists the feature browser and says the version, the display (headless: --headless=new, no Xvfb, decision 4),
the max and the sandbox; else it says neither.

``problems`` is the preflight of a run dispatched with browser: no Chromium on this worker, or no node on the run's
PATH (which the agent's browser tools run on), is a problem with cause missing_tool, naming the program and the
command that installs it."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from evo_agents.hub.client import write_atomic
from evo_agents.hub.runs import MAX_BROWSERS
from evo_agents.worker import preflight
from evo_agents.worker.browser import chromium
from evo_agents.worker.home import DIR_MODE, FILE_MODE, WorkerHome, WorkerStateError

PLAYWRIGHT = "playwright@1.63.0"
BUILD = "chromium-1243"  # the directory of the Chromium that PLAYWRIGHT installs, in its cache
COMMAND = "evo-agents worker browser install"
DIRECTORY = "browser"  # in the worker's home
STATE = "state.json"
LIBRARIES = "libroot"
DISPLAY = "headless"  # --headless=new: the spike found every sign-in page it tried works without a screen
CACHE_VARIABLE = "PLAYWRIGHT_BROWSERS_PATH"
# the binary under BUILD, as Playwright lays it out on each system and processor
EXECUTABLES = (
    "chrome-linux64/chrome",
    "chrome-linux/chrome",
    "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
)
# what Chromium needs on an Ubuntu without a desktop, fetched where ldd finds a library missing (spike setup-libs)
LINUX_PACKAGES = (
    "libasound2t64",
    "libatk-bridge2.0-0t64",
    "libatk1.0-0t64",
    "libatspi2.0-0t64",
    "libgbm1",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxi6",
    "libxrandr2",
    "libxrender1",
    "libcups2t64",
    "libcairo2",
    "libpango-1.0-0",
    "libavahi-common3",
    "libavahi-client3",
    "libxcb-render0",
    "libxcb-shm0",
    "libpixman-1-0",
    "libthai0",
    "libharfbuzz0b",
    "libdatrie1",
    "libgraphite2-3",
    "libavahi-common-data",
)
GIB = 1024**3
GIB_PER_BROWSER = 1.5
DOWNLOAD_TIMEOUT = 1800.0
PROBE_TIMEOUT = 30.0  # seconds Chromium has to write its DevTools port at the install's start
COMMAND_TIMEOUT = 120.0
VERSION = re.compile(r"(\d{1,4}(?:\.\d{1,6}){1,3})\s*$")
NOT_FOUND = re.compile(r"^\s*(\S+) => not found", re.MULTILINE)


class InstallError(WorkerStateError):
    """Chromium could not be installed; ``str`` says why and what to do."""


@dataclass(frozen=True)
class Installed:
    """What state.json says of the Chromium the daemon starts."""

    executable: str
    version: str
    sandbox: bool
    max: int
    libraries: str | None = (
        None  # the directory of the libraries the install unpacked (Linux), first on LD_LIBRARY_PATH
    )

    def report(self) -> dict:
        """What a heartbeat says of it (``runs.models.browser.BrowserReport`` on the hub)."""
        return {"chromium": self.version, "display": DISPLAY, "max": self.max, "sandbox": self.sandbox}


def directory(home: WorkerHome) -> Path:
    return home.root / DIRECTORY


def read(home: WorkerHome) -> Installed | None:
    """What state.json says, while the binary it names is there; None when nothing is installed."""
    try:
        data = json.loads((directory(home) / STATE).read_text(encoding="utf-8"))
        installed = Installed(**data)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(installed.max, int) or not 1 <= installed.max <= MAX_BROWSERS:
        return None
    if not VERSION.fullmatch(installed.version) or not os.access(installed.executable, os.X_OK):
        return None
    return installed


def report(home: WorkerHome) -> dict | None:
    """What the daemon's heartbeat says of its Chromium, with the feature browser; None for no Chromium."""
    installed = read(home)
    return None if installed is None else installed.report()


def problems(home: WorkerHome, path: str | None) -> list[preflight.Problem]:
    """The preflight of a run dispatched with browser: Chromium installed on this worker, and node on ``path`` (the
    run's PATH) for the agent's browser tools."""
    found = []
    if read(home) is None:
        found.append(
            preflight.Problem(
                "missing_tool",
                f"the run asks for a browser and Chromium is not installed on this worker: run `{COMMAND}`",
            )
        )
    if shutil.which("node", path=path if path is not None else os.defpath) is None:
        found.append(
            preflight.Problem(
                "missing_tool",
                "the run asks for a browser and node, which its browser tools run on, is not on this worker's PATH: "
                f"install Node.js 18 or later, then run `{COMMAND}`",
            )
        )
    return found


# Where Playwright keeps Chromium


def cache_root(env: Mapping[str, str]) -> Path:
    """Playwright's cache of browsers: $PLAYWRIGHT_BROWSERS_PATH, else this user's cache directory."""
    named = env.get(CACHE_VARIABLE)
    if named and named != "0":
        return Path(named).expanduser()
    if sys.platform == "darwin":
        return Path("~/Library/Caches/ms-playwright").expanduser()
    return Path(env.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "ms-playwright"


def locate(env: Mapping[str, str]) -> Path | None:
    """The Chromium binary of ``PLAYWRIGHT`` in its cache, once its download completed; None before."""
    build = cache_root(env) / BUILD
    if not (build / "INSTALLATION_COMPLETE").exists():
        return None
    for relative in EXECUTABLES:
        candidate = build / relative
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def download(env: Mapping[str, str], say: Callable[[str], None]) -> Path:
    """Have Playwright download Chromium into its cache; the binary."""
    npx = shutil.which("npx", path=env.get("PATH"))
    if npx is None:
        raise InstallError(
            "npx is not on PATH: install Node.js 18 or later (its npx downloads Chromium, and the agent's browser tools "
            f"run on node), then run `{COMMAND}` again"
        )
    say(f"Downloading Chromium with {PLAYWRIGHT} into {cache_root(env)} (no sudo)...")
    command = [npx, "-y", PLAYWRIGHT, "install", "--no-shell", "--no-remove", "chromium"]
    try:
        done = subprocess.run(command, env=dict(env), stdin=subprocess.DEVNULL, timeout=DOWNLOAD_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise InstallError(
            f"the download of Chromium took more than {DOWNLOAD_TIMEOUT:g}s: run `{COMMAND}` again"
        ) from None
    found = locate(env)
    if done.returncode != 0 or found is None:
        raise InstallError(
            f"`npx {PLAYWRIGHT} install chromium` exited {done.returncode} and left no Chromium in {cache_root(env)}"
        )
    return found


# What the binary needs and does here


def _run(command: list[str], env: Mapping[str, str], **options) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, env=dict(env), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, **options
    )


def missing_libraries(executable: Path, env: Mapping[str, str], libraries: str | None) -> list[str]:
    """The shared libraries ``ldd`` finds missing for ``executable`` with ``libraries`` on LD_LIBRARY_PATH."""
    if shutil.which("ldd") is None:
        return []
    done = _run(["ldd", str(executable)], chromium.environment(env, libraries), timeout=COMMAND_TIMEOUT)
    return sorted(set(NOT_FOUND.findall(done.stdout)))


def linux_libraries(
    executable: Path, home: WorkerHome, env: Mapping[str, str], say: Callable[[str], None]
) -> str | None:
    """The directory of the libraries unpacked for Chromium on Linux, fetching them when ``ldd`` finds some missing;
    None when it needs none (and on macOS)."""
    if not sys.platform.startswith("linux"):
        return None
    root = directory(home) / LIBRARIES
    unpacked = root / "usr" / "lib" / f"{platform.machine()}-linux-gnu"
    held = str(unpacked) if unpacked.is_dir() else None
    missing = missing_libraries(executable, env, held)
    if not missing:
        return held
    if shutil.which("apt-get") is None or shutil.which("dpkg") is None:
        raise InstallError(f"Chromium lacks {', '.join(missing)}, and this machine has no apt-get: install them")
    say(f"Chromium lacks {', '.join(missing)}: fetching them with apt-get download into {root} (no sudo)...")
    root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evo-browser-debs-") as debs:
        for package in LINUX_PACKAGES:
            names = [package, package.removesuffix("t64")] if package.endswith("t64") else [package]
            for name in names:  # an Ubuntu before 24.04 names them without t64
                if _run(["apt-get", "download", name], env, cwd=debs, timeout=COMMAND_TIMEOUT).returncode == 0:
                    break
        for deb in sorted(Path(debs).glob("*.deb")):
            _run(["dpkg", "-x", str(deb), str(root)], env, timeout=COMMAND_TIMEOUT)
    still = missing_libraries(executable, env, str(unpacked))
    if still:
        raise InstallError(f"Chromium still lacks {', '.join(still)}: install them with the system's package manager")
    return str(unpacked)


def chromium_version(executable: Path, env: Mapping[str, str], libraries: str | None) -> str:
    """The version ``--version`` prints, such as 153.0.8010.12."""
    done = _run([str(executable), "--version"], chromium.environment(env, libraries), timeout=COMMAND_TIMEOUT)
    found = VERSION.search(done.stdout.strip())
    if done.returncode != 0 or found is None:
        raise InstallError(
            f"{executable} --version printed no version: {chromium.tail_text(done.stdout + done.stderr)}"
        )
    return found.group(1)


def starts(
    executable: Path, version: str, env: Mapping[str, str], libraries: str | None, *, sandbox: bool
) -> tuple[bool, str]:
    """Whether Chromium starts here as a run starts it (``sandbox`` or --no-sandbox) and writes its DevTools port in
    time, and the end of its log when it does not."""
    with tempfile.TemporaryDirectory(prefix="evo-browser-probe-") as scratch:
        profile, log = Path(scratch) / "profile", Path(scratch) / "chromium.log"
        process = chromium.start(
            executable, profile, version, sandbox=sandbox, libraries=libraries, base_env=env, log_path=log
        )
        try:
            deadline = time.monotonic() + PROBE_TIMEOUT
            while time.monotonic() < deadline:
                if chromium.port(profile) is not None:
                    return True, ""
                if process.poll() is not None:
                    break
                time.sleep(0.1)
        finally:
            chromium.kill(process)
        return False, chromium.tail(log) or f"it wrote no DevTools port within {PROBE_TIMEOUT:g}s"


def available_memory() -> int | None:
    """The bytes of memory available for new processes: MemAvailable on Linux, free, inactive and speculative pages on
    macOS; None when unknown."""
    with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=False).stdout
            size = int(re.search(r"page size of (\d+) bytes", out).group(1))
            pages = {name: int(value) for name, value in re.findall(r"^Pages (\w+):\s+(\d+)\.", out, re.MULTILINE)}
            return size * sum(pages.get(name, 0) for name in ("free", "inactive", "speculative"))
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


def default_max(cores: int | None = None, memory: int | None = None) -> int:
    """How many browsers at once a worker of ``cores`` and ``memory`` bytes available holds: one per core but one and
    per 1.5 GiB, at least one."""
    cores = cores if cores is not None else (os.cpu_count() or 1)
    memory = memory if memory is not None else available_memory()
    by_memory = int(memory / GIB // GIB_PER_BROWSER) if memory is not None else cores
    return max(1, min(cores - 1, by_memory, MAX_BROWSERS))


# The install


def write(home: WorkerHome, installed: Installed) -> None:
    folder = directory(home)
    folder.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    data = json.dumps(asdict(installed), indent=2).encode() + b"\n"
    write_atomic(folder / STATE, data, FILE_MODE)


def install(
    home: WorkerHome, env: Mapping[str, str], *, max_browsers: int | None = None, say: Callable[[str], None] = print
) -> tuple[Installed, bool]:
    """Chromium installed for the daemon of ``home``, and whether this changed anything: nothing when state.json
    already says what it would write."""
    if max_browsers is not None and not 1 <= max_browsers <= MAX_BROWSERS:
        raise InstallError(f"--max takes 1 to {MAX_BROWSERS} browsers at once, not {max_browsers}")
    held, found = read(home), locate(env)
    if held is not None and found is not None and held.executable == str(found):
        if max_browsers is None or max_browsers == held.max:
            return held, False
        installed = Installed(**{**asdict(held), "max": max_browsers})
        write(home, installed)
        return installed, True
    found = found or download(env, say)
    libraries = linux_libraries(found, home, env, say)
    version = chromium_version(found, env, libraries)
    sandbox, said = starts(found, version, env, libraries, sandbox=True)
    if not sandbox:
        say(f"Chromium does not start with its own sandbox here ({said}): it runs with --no-sandbox.")
        works, said = starts(found, version, env, libraries, sandbox=False)
        if not works:
            raise InstallError(f"Chromium {version} does not start on this machine: {said}")
    chosen = max_browsers or (held.max if held is not None else default_max())
    installed = Installed(str(found), version, sandbox, chosen, libraries)
    write(home, installed)
    return installed, True
