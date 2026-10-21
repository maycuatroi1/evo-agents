"""A stand-in for ``uv tool install`` in the tests of the daemon's self-update, run as the ``uv`` the daemon finds.

It writes what uv writes of a tool install of evo-ak (``uv-receipt.toml``, the dist-info with its
``direct_url.json``, ``bin/python`` and the ``evo-agents`` command) into ``$UV_TOOL_DIR/evo-ak`` and
``$UV_TOOL_BIN_DIR``, where ``bin/python`` and the command are shell scripts that succeed, or fail for a version that
"does not start". What each call does comes from the JSON file ``$FAKE_UV_SCENARIO``:

```
{"nightly": "<sha>",    the commit @nightly resolves to
 "version": "0.23.0",   the version a git install gets
 "recreates": false,    true: every call deletes the environment and the command first, as uv 0.12.24 does
 "steps": ["fail", "broken", "wipe", "ok"]}   one per call, in order; "ok" once they run out
```

``fail`` exits 1 and touches nothing (a build that fails, with uv 0.6.13 over an environment of the same interpreter);
``broken`` installs a version whose python and command exit 1; ``wipe`` deletes the environment and exits 1 (uv 0.6.13
asked for another interpreter, then a failed build). With ``recreates`` each call deletes the environment and the
command before its step, so ``fail`` leaves nothing either, and ``ok`` builds a new environment, without whatever else
lay in the old one. Each call is appended to ``$FAKE_UV_LOG`` as one JSON line: its arguments, UV_TOOL_DIR and
UV_TOOL_BIN_DIR.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

REPO = "https://github.com/maycuatroi1/evo-agents"
GIT_REQUIREMENT = re.compile(r"^evo-ak(?:\[(?P<extras>[^\]]*)\])? @ git\+" + re.escape(REPO) + r"@(?P<rev>\S+)$")
PYPI_REQUIREMENT = re.compile(r"^evo-ak(?:\[(?P<extras>[^\]]*)\])?==(?P<version>\S+)$")


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_install(
    tool_dir: Path,
    bin_dir: Path,
    *,
    version: str,
    commit: str | None,
    extras: tuple[str, ...] = ("worker",),
    revision: str | None = "nightly",
    starts: bool = True,
    link: bool = True,
) -> Path:
    """The environment of a tool install of evo-ak as uv leaves it: from git at ``revision`` when ``commit`` is set,
    else from PyPI. The command on the PATH is a symlink to the environment's, as uv makes it on Unix, or with
    ``link=False`` a copy of it. Returns the environment's path."""
    prefix = Path(tool_dir) / "evo-ak"
    prefix.mkdir(parents=True, exist_ok=True)
    site = prefix / "lib" / "python3.12" / "site-packages"
    for old in site.glob("evo_ak-*.dist-info") if site.is_dir() else []:
        shutil.rmtree(old)
    dist = site / f"evo_ak-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: evo-ak\nVersion: {version}\n", encoding="utf-8")
    listed = ", ".join(f'"{name}"' for name in extras)
    if commit:
        direct = {"url": REPO, "vcs_info": {"vcs": "git", "commit_id": commit, "requested_revision": revision}}
        (dist / "direct_url.json").write_text(json.dumps(direct), encoding="utf-8")
        source = f', git = "{REPO}?rev={revision}"'
    else:
        source = ""
    command = Path(bin_dir) / "evo-agents"
    (prefix / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "evo-ak", extras = [{listed}]{source} }}]\n'
        f'entrypoints = [\n    {{ name = "evo-agents", install-path = "{command}" }},\n]\n',
        encoding="utf-8",
    )
    if starts:
        _script(prefix / "bin" / "python", "exit 0")
        _script(prefix / "bin" / "evo-agents", f'echo "evo-agents {version}"')
    else:
        _script(prefix / "bin" / "python", "echo \"ModuleNotFoundError: No module named 'aiohttp'\" >&2; exit 1")
        _script(prefix / "bin" / "evo-agents", 'echo "Traceback: ImportError" >&2; exit 1')
    command.parent.mkdir(parents=True, exist_ok=True)
    command.unlink(missing_ok=True)
    if link:
        command.symlink_to(prefix / "bin" / "evo-agents")
    else:
        shutil.copy2(prefix / "bin" / "evo-agents", command)
    return prefix


def install_script(path: Path) -> Path:
    """An executable ``uv`` at ``path`` that runs this module with the Python running the tests."""
    _script(path, f'exec "{sys.executable}" "{Path(__file__).resolve()}" "$@"')
    return path


def _next_step(scenario_path: Path) -> tuple[dict, str]:
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    steps = scenario.get("steps") or []
    step = steps.pop(0) if steps else "ok"
    scenario["steps"] = steps
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    return scenario, step


def main(argv: list[str]) -> int:
    tool_dir, bin_dir = Path(os.environ["UV_TOOL_DIR"]), Path(os.environ["UV_TOOL_BIN_DIR"])
    with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "tool_dir": str(tool_dir), "bin_dir": str(bin_dir)}) + "\n")
    if argv[:2] != ["tool", "install"] or "--python" not in argv:
        print(f"fake uv: unexpected arguments {argv}", file=sys.stderr)
        return 2
    scenario, step = _next_step(Path(os.environ["FAKE_UV_SCENARIO"]))
    requirement = argv[-1]
    git, pypi = GIT_REQUIREMENT.match(requirement), PYPI_REQUIREMENT.match(requirement)
    if not git and not pypi:
        print(f"fake uv: cannot read the requirement {requirement}", file=sys.stderr)
        return 2
    found = git or pypi
    extras = tuple(item for item in (found.group("extras") or "").split(",") if item)
    if scenario.get("recreates"):
        shutil.rmtree(tool_dir / "evo-ak", ignore_errors=True)
        (bin_dir / "evo-agents").unlink(missing_ok=True)
    if step == "fail":
        print("error: Failed to build `evo-ak`: the build backend returned an error", file=sys.stderr)
        return 1
    if step == "wipe":
        shutil.rmtree(tool_dir / "evo-ak", ignore_errors=True)
        print("error: Failed to build `evo-ak` in a new environment", file=sys.stderr)
        return 1
    if git:
        revision = git.group("rev")
        commit = scenario["nightly"] if revision == "nightly" else revision
        version = scenario.get("version", "0.23.0") if revision == "nightly" else scenario.get("old_version", "0.22.0")
        write_install(
            tool_dir, bin_dir, version=version, commit=commit, extras=extras, revision=revision, starts=step != "broken"
        )
    else:
        write_install(
            tool_dir, bin_dir, version=pypi.group("version"), commit=None, extras=extras, starts=step != "broken"
        )
    print(f"Installed 1 executable: evo-agents ({requirement})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
