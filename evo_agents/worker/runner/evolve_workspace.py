"""The on-disk tree of an evolve run (``evo_agents.worker.runner.evolve``), in the style of a meta-harness search: what
its proposer agent reads and the one folder it rewrites.

Under the tree's root (``Tree``):

- ``iterations/<n>/``: one folder per iteration; ``candidate/`` holds the whole files of the parent the proposer
  rewrites (``component_files``), ``parent.json`` the parent's id, scores and components' names, ``merge/`` the
  conflicts of a merge the agent writes (``ancestor/``, ``ours/``, ``theirs/`` and the ``resolved/`` it fills);
- ``frontier.json``: the Pareto front now, each survivor with how many tasks it is best on, its scores and cost;
- ``evolution_summary.jsonl``: one line per candidate measured, appended;
- ``eval_traces/<candidate>/<task>.json``: what one task of one candidate showed, its reflection data: the task's
  prompt, the score, each verify's exit code and its whole output (stdout and stderr together), the agent's failed tool
  calls, turns, cost and error.

The tree holds no verify command and no task of the holdout split: the worker runs verify itself, and the hub never
sends a holdout task. No component is cut to a length: the proposer rewrites a file whole.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from evo_agents.hub import evolve, replay
from evo_agents.infra.files import JsonFile

ITERATIONS = "iterations"
FRONTIER = "frontier.json"
SUMMARY = "evolution_summary.jsonl"
TRACES = "eval_traces"
CANDIDATE = "candidate"  # under iterations/<n>/: the files the proposer rewrites
PARENT = "parent.json"
MERGE = "merge"  # under iterations/<n>/: the conflicts of a merge
RESOLVED = "resolved"  # under merge/: what the agent of a merge writes, one file per conflict
AXIS_SUFFIX = ".md"  # an agent_config axis is the file <axis>.md of the candidate folder
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str) -> str:
    """``text`` as one file name: each run of other characters than letters, digits, ``.``, ``_`` and ``-`` as ``_``."""
    found = _UNSAFE.sub("_", str(text)).strip("._") or "_"
    return found[:200]


def component_files(target: str, keys: Iterable[str]) -> dict[str, str]:
    """component -> its file under a candidate folder: a path of the harness itself (git), ``<axis>.md``
    (agent_config)."""
    if target == evolve.TARGETS[1]:
        return {key: f"{key}{AXIS_SUFFIX}" for key in keys}
    return {key: key for key in keys}


def _target(root: Path, relative: str) -> Path | None:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts) or parts[0] == "/":
        return None
    return root.joinpath(*parts)


def write_components(root: Path, target: str, components: Mapping[str, str | None]) -> None:
    """Write each component of ``components`` whole as its file under ``root`` (None writes nothing)."""
    files = component_files(target, components)
    for key, text in components.items():
        path = _target(root, files[key])
        if text is None or path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _files_under(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    )


def read_components(root: Path, target: str, keys: Iterable[str]) -> dict[str, str]:
    """The components a candidate folder ``root`` holds once the proposer is done: each of ``keys`` whose file is
    there (one it removed is no component), and for a git target each new file of the directory of a skill one of
    ``keys`` is a file of (a side file the proposer added). Read whole."""
    keys = list(keys)
    files = component_files(target, keys)
    found = {}
    for key in keys:
        path = _target(root, files[key])
        if path is not None and path.is_file():
            found[key] = path.read_text(encoding="utf-8", errors="replace")
    if target == evolve.TARGETS[0]:
        skills = {skill for skill in map(replay.skill_dir, keys) if skill}
        for relative in _files_under(root):
            if relative not in found and replay.skill_dir(relative) in skills:
                found[relative] = (root / relative).read_text(encoding="utf-8", errors="replace")
    return found


class Tree:
    """The tree of one evolution at ``root``."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / ITERATIONS).mkdir(exist_ok=True)
        (root / TRACES).mkdir(exist_ok=True)

    def iteration(self, number: int) -> Path:
        """The folder of iteration ``number``, made."""
        path = self.root / ITERATIONS / str(int(number))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def candidate_folder(self, number: int, target: str, parent: str, scores: Mapping, components: Mapping) -> Path:
        """iterations/<number>/candidate/ with the whole files of the parent, and parent.json beside it."""
        folder = self.iteration(number)
        about = {"parent": parent, "scores": dict(scores), "components": sorted(components), "target": target}
        _write_json(folder / PARENT, about)
        candidate = folder / CANDIDATE
        candidate.mkdir(exist_ok=True)
        write_components(candidate, target, components)
        return candidate

    def write_frontier(self, frontier: list[dict]) -> Path:
        path = self.root / FRONTIER
        _write_json(path, {"frontier": frontier})
        return path

    def append_summary(self, row: Mapping) -> None:
        with (self.root / SUMMARY).open("a", encoding="utf-8") as out:
            out.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")

    def write_trace(self, candidate: str, ref: str, trace: Mapping) -> Path:
        folder = self.root / TRACES / safe_name(candidate)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe_name(ref)}.json"
        _write_json(path, dict(trace))
        return path

    def merge_folder(self, number: int, target: str, request: Mapping) -> Path:
        """iterations/<number>/merge/ with each conflict of ``request`` (``evolve.synthesis_request``) whole, as its
        file under ancestor/, ours/ and theirs/ (a side that has none has no file), the request as request.json, and
        an empty resolved/ for the agent's answers."""
        folder = self.iteration(number) / MERGE
        folder.mkdir(exist_ok=True)
        for side in ("ancestor", "ours", "theirs"):
            texts = {item["key"]: item[side] for item in request.get("conflicts") or []}
            write_components(folder / side, target, texts)
        (folder / RESOLVED).mkdir(exist_ok=True)
        _write_json(folder / "request.json", dict(request))
        return folder


def _write_json(path: Path, body) -> None:
    JsonFile(path, mode=0o644, durable=False, ensure_ascii=False, indent=1, sort_keys=True).write(body)


def proposer_prompt(
    *, target: str, iteration: int, parent: str, files: Iterable[str], asi: Mapping[str, Mapping] | None = None
) -> str:
    """What the proposer agent of iteration ``iteration`` is asked, in the tree's root: rewrite the parent's files in
    iterations/<n>/candidate/ whole, as it sees fit, from the reflection data of the tree and the parent's ASI of each
    task it did not pass (``asi``, task -> ``evolve_eval.short_trace``), written in the prompt itself."""
    where = f"{ITERATIONS}/{iteration}/{CANDIDATE}/"
    what = "an agent's instructions and skill" if target == evolve.TARGETS[1] else "harness files of a repository"
    listed = "\n".join(f"- {where}{name}" for name in files)
    failed = ""
    if asi:
        shown = json.dumps({ref: dict(asi[ref]) for ref in sorted(asi)}, ensure_ascii=False, indent=1, sort_keys=True)
        failed = (
            f"What candidate {parent} showed on each task it did not pass (its actionable side information: each "
            f"failed check's exit code and the end of its output, the agent's failed tool calls, error and last "
            f"message):\n{shown}\n\n"
        )
    return (
        f"You improve {what} by evolution. Your working directory holds the evolution so far:\n"
        f"- {FRONTIER}: the candidates on the Pareto front, how many tasks each is best on, their scores and cost;\n"
        f"- {SUMMARY}: one line per candidate measured, its parents, scores, cost and turns;\n"
        f"- {TRACES}/<candidate>/<task>.json: what each task showed for each candidate: the task, the score, each "
        "check's exit code and whole output, the agent's failed tool calls, its turns and cost;\n"
        f"- {ITERATIONS}/<n>/: the files each earlier iteration started from.\n\n"
        f"This is iteration {iteration}. {where} holds the whole files of candidate {parent}, the parent:\n{listed}\n\n"
        f"{failed}Read the traces of the tasks it failed or did badly on, and of those other candidates did better on, find "
        f"why, and rewrite the files in {where} to do better on those tasks without losing the others. Rewrite any "
        "file whole, at any length; add a side file in the directory of a skill when it helps, or delete a file that "
        f"hurts. Change nothing outside {where}, and end your turn once the files are as you want them."
    )


def merge_prompt(*, target: str, iteration: int, keys: Iterable[str]) -> str:
    """What the agent of a merge is asked: write one file per conflict in merge/resolved/, from the ancestor's and both
    sides' whole texts."""
    folder = f"{ITERATIONS}/{iteration}/{MERGE}/"
    files = component_files(target, keys)
    listed = "\n".join(f"- {files[key]}" for key in sorted(files))
    return (
        "Two candidates of an evolution of harness files, each on the Pareto front, changed the same files apart. "
        f"For each file below, {folder}ancestor/ holds it as their nearest common ancestor has it, {folder}ours/ and "
        f"{folder}theirs/ as each side has it (a side without the file deleted it), and {folder}request.json lists "
        f"them. The traces under {TRACES}/ say what each did well.\n{listed}\n\nWrite each file in "
        f"{folder}{RESOLVED}/ whole, keeping what each side does well; leave one out to drop it. Change nothing "
        "else, and end your turn once they are written."
    )


def read_answers(folder: Path, target: str, keys: Iterable[str]) -> dict[str, str | None]:
    """The agent's answer to each conflict of a merge, read from ``folder``/resolved/: its text, None for a file it
    left out."""
    keys = list(keys)
    found = read_components(folder / RESOLVED, target, keys)
    return {key: found.get(key) for key in keys}
