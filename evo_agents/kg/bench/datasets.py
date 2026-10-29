"""The code benchmarks of ``kg bench``: where each lives, at which revision, and how its rows become tasks.

``fetch`` downloads the files of a set at the revision pinned here (``SOURCES``) into the bench cache, never into a
repo (``cache_dir`` refuses a directory inside a git work tree), then reads them into ``tasks.jsonl``: one task a
line, ``{"bench", "id", "key", "repo", "commit", "language", "question", "files", "nodes", "lines"}``. ``question``
is the issue as the set gives it; ``files`` are paths at ``commit``, ``nodes`` are ``path::qualname`` of the functions
the set names, ``lines`` are closed ranges ``[path, first, last]`` from line 1. ``key`` names the SWE-bench instance
a task comes from, lowercased and without Pro's ``instance_`` prefix, so that a task two sets share is kept once
(``tasks.dedupe``). ``fetched.json`` next to it names each file with its revision, size and SHA-256, and counts the
rows left out by why.

What each set gives as the answer:

- ContextBench: ``gold_context``, the files and line ranges an agent needs.
- SWE-Explore: ``read_core_files`` and ``read_core_regions``; the issue, repo and commit come from the SWE-bench set
  the row names (Verified, Pro or Multilingual).
- LocBench: the files of the patch, ``edit_functions`` as nodes, and the lines the patch changes on the old side (an
  insertion: the line before it).
- MuLocBench: the files of ``file_loc`` that exist at the base commit, its functions as nodes, its ``mod`` and
  ``add`` lines.

A file the patch adds is not an answer: it is not in the graph of the base commit. A row without a repo, a commit,
an issue or an answer is left out and counted.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from evo_agents.kg.bench.locbench_repos import LOCBENCH_REPOS

HF_URL = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{file}"
DOWNLOAD_TIMEOUT = 600.0
BENCH_EXTRA = "pip install 'evo-ak[bench]'"
CACHE_ENV = "EVO_KG_BENCH_CACHE"
DEFAULT_CACHE = "~/.cache/evo-agents/kg-bench"


@dataclass(frozen=True)
class Source:
    dataset: str  # a Hugging Face dataset id
    revision: str  # the commit of the dataset repo: what the tasks are read from, fixed
    file: str


CONTEXTBENCH = "contextbench"
SWE_EXPLORE = "swe-explore"
LOCBENCH = "locbench"
MULOCBENCH = "mulocbench"
CODE_BENCHES = (CONTEXTBENCH, SWE_EXPLORE, LOCBENCH, MULOCBENCH)

SWE_VERIFIED = Source(
    "princeton-nlp/SWE-bench_Verified", "c104f840cc67f8b6eec6f759ebc8b2693d585d4a", "data/test-00000-of-00001.parquet"
)
SWE_PRO = Source(
    "ScaleAI/SWE-bench_Pro", "2d52cb3df914a3fcf80c7f66738b3a88ae37fc50", "data/default/test-00000-of-00001.parquet"
)
SWE_MULTILINGUAL = Source(
    "SWE-bench/SWE-bench_Multilingual", "846e647b9f33c0b51b739d005d13d85493c9af09", "data/test-00000-of-00001.parquet"
)
SOURCES: dict[str, tuple[Source, ...]] = {
    CONTEXTBENCH: (
        Source("Contextbench/ContextBench", "c2855792b006af41c67202d33883fb9d46362853", "data/full.parquet"),
    ),
    SWE_EXPLORE: (
        Source(
            "SWE-Explore-Bench/SWE-Explore-Bench",
            "bdb0ae45d7c337d9e1dc3ebfe2a0af6bc7c1fbd9",
            "bench.final.public.jsonl",
        ),
        SWE_VERIFIED,
        SWE_PRO,
        SWE_MULTILINGUAL,
    ),
    LOCBENCH: (
        Source("czlll/Loc-Bench_V1", "c44cf3b74e07ca642cec841b471a9939907c12a7", "data/test-00000-of-00001.parquet"),
    ),
    MULOCBENCH: (Source("somethingone/MULocBench", "a456004b54a4f537c22bde84993457585ebf7387", "all_data_card.jsonl"),),
}
EXPLORE_SOURCES = {"verified": SWE_VERIFIED, "pro": SWE_PRO, "multilingual": SWE_MULTILINGUAL}

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".swift": "swift",
    ".scala": "scala",
}
DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NUMBER = re.compile(r"/(\d+)/?$")


class BenchError(RuntimeError):
    """A bench command that cannot go on; the message says why and what to do."""


# -- the cache -----------------------------------------------------------------------------------------------------


def _work_tree_of(path: Path) -> Path | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def cache_dir(given: str | Path | None = None) -> Path:
    """The bench cache: ``given``, else $EVO_KG_BENCH_CACHE, else DEFAULT_CACHE. Never inside a git work tree, so no
    repo ever holds a set's data (ContextBench names no license)."""
    path = Path(given or os.environ.get(CACHE_ENV) or DEFAULT_CACHE).expanduser().resolve()
    tree = _work_tree_of(path)
    if tree is not None:
        raise BenchError(
            f"the bench cache {path} is inside the git work tree {tree}: give --cache a directory outside any repo"
        )
    return path


def data_dir(cache: Path, bench: str) -> Path:
    return cache / "data" / bench


def raw_path(cache: Path, bench: str, source: Source) -> Path:
    return data_dir(cache, bench) / "raw" / source.dataset.replace("/", "__") / source.revision / source.file


# -- fetching ------------------------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(source: Source, target: Path, mirror: Path | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".part-")
    os.close(fd)
    try:
        if mirror is not None:
            found = mirror / source.dataset / source.file
            if not found.is_file():
                raise BenchError(f"{found} is missing: --from holds DATASET/FILE for each file of the set")
            shutil.copyfile(found, tmp)
        else:
            url = HF_URL.format(dataset=source.dataset, revision=source.revision, file=source.file)
            try:
                with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, open(tmp, "wb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            except OSError as exc:
                raise BenchError(f"cannot download {url}: {exc}") from None
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fetch(bench: str, cache: Path, *, mirror: Path | None = None, force: bool = False) -> dict:
    """Download the files of ``bench`` at their pinned revisions (a file already there is kept unless ``force``),
    read them into tasks.jsonl and write fetched.json, which it returns."""
    files = []
    for source in SOURCES[bench]:
        target = raw_path(cache, bench, source)
        if force or not target.is_file():
            _download(source, target, mirror)
        files.append(
            {
                "dataset": source.dataset,
                "revision": source.revision,
                "file": source.file,
                "bytes": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    tasks, skipped = READERS[bench](cache)
    if bench == LOCBENCH:
        unknown = sorted({t["repo"] for t in tasks if t["repo"].lower() not in LOCBENCH_REPOS})
        if unknown:
            raise BenchError(
                f"LocBench names repos evo_agents/kg/bench/locbench_repos.py lacks: {', '.join(unknown[:10])}; "
                "add them there, since no repo of LocBench may be in tune"
            )
    out = data_dir(cache, bench)
    with (out / "tasks.jsonl.tmp").open("w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(out / "tasks.jsonl.tmp", out / "tasks.jsonl")
    fetched = {"bench": bench, "files": files, "tasks": len(tasks), "skipped": skipped}
    (out / "fetched.json").write_text(json.dumps(fetched, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return fetched


def fetched_info(cache: Path, bench: str) -> dict | None:
    path = data_dir(cache, bench) / "fetched.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def revision(bench: str) -> dict[str, str]:
    """The revision of each dataset ``bench`` is read from, as the summary names them."""
    return {s.dataset: s.revision for s in SOURCES[bench]}


# -- reading rows --------------------------------------------------------------------------------------------------


def read_rows(path: Path) -> list[dict]:
    """The rows of a parquet (with the bench extra), JSON Lines or JSON list file."""
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise BenchError(f"reading {path.name} needs pyarrow: {BENCH_EXTRA}") from None
        return pq.read_table(path).to_pylist()
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
        return data if isinstance(data, list) else list(data.values())
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _source_rows(cache: Path, bench: str, source: Source) -> list[dict]:
    return read_rows(raw_path(cache, bench, source))


def instance_key(instance_id: str) -> str:
    """The SWE-bench instance a task comes from, as every set can name it."""
    key = instance_id.strip().lower()
    return key.removeprefix("instance_")


def language_of(paths) -> str:
    """The language most of ``paths`` are written in, by suffix; ``other`` when none is known."""
    counts: dict[str, int] = {}
    for path in paths:
        name = LANGUAGES.get(PurePosixPath(path).suffix.lower())
        if name:
            counts[name] = counts.get(name, 0) + 1
    return max(sorted(counts), key=counts.get) if counts else "other"


def merge_ranges(lines: list[tuple[str, int, int]]) -> list[list]:
    """Closed ranges per path, sorted, the ones that touch or overlap made one."""
    merged: list[list] = []
    for path, first, last in sorted(lines):
        if merged and merged[-1][0] == path and first <= merged[-1][2] + 1:
            merged[-1][2] = max(merged[-1][2], last)
        else:
            merged.append([path, first, last])
    return merged


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _count(skipped: dict, why: str) -> None:
    skipped[why] = skipped.get(why, 0) + 1


def _task(bench: str, row_id: str, key: str, repo, commit, question, files, nodes, lines, language=None) -> dict:
    files = list(dict.fromkeys(f for f in files if f))
    return {
        "bench": bench,
        "id": row_id,
        "key": key,
        "repo": repo,
        "commit": commit,
        "language": (language or language_of(files)).lower(),
        "question": question,
        "files": files,
        "nodes": list(dict.fromkeys(nodes)),
        "lines": merge_ranges(lines),
    }


def _usable(task: dict, skipped: dict) -> bool:
    for key, why in (("repo", "no repo"), ("commit", "no base commit"), ("question", "no issue")):
        if not task[key] or not str(task[key]).strip():
            _count(skipped, why)
            return False
    if not task["files"] and not task["nodes"] and not task["lines"]:
        _count(skipped, "no answer at the base commit")
        return False
    return True


# -- the sets ------------------------------------------------------------------------------------------------------


def read_contextbench(cache: Path) -> tuple[list[dict], dict]:
    tasks, skipped = [], {}
    for row in _source_rows(cache, CONTEXTBENCH, SOURCES[CONTEXTBENCH][0]):
        gold = _json_value(row.get("gold_context")) or []
        lines = [
            (g["file"], int(g["start_line"]), int(g["end_line"]))
            for g in gold
            if isinstance(g, dict)
            and g.get("file")
            and int(g.get("start_line") or 0) >= 1
            and int(g.get("end_line") or 0) >= int(g.get("start_line") or 0)
        ]
        task = _task(
            CONTEXTBENCH,
            str(row.get("instance_id")),
            instance_key(str(row.get("original_inst_id") or row.get("instance_id"))),
            row.get("repo"),
            row.get("base_commit"),
            row.get("problem_statement"),
            [g["file"] for g in gold if isinstance(g, dict) and g.get("file")],
            [],
            lines,
            row.get("language"),
        )
        if _usable(task, skipped):
            tasks.append(task)
    return tasks, skipped


def read_swe_explore(cache: Path) -> tuple[list[dict], dict]:
    issues: dict[str, dict] = {}
    for name, source in EXPLORE_SOURCES.items():
        for row in _source_rows(cache, SWE_EXPLORE, source):
            issues[(name, instance_key(str(row.get("instance_id"))))] = row
    tasks, skipped = [], {}
    for row in _source_rows(cache, SWE_EXPLORE, SOURCES[SWE_EXPLORE][0]):
        key = instance_key(str(row.get("instance_id")))
        issue = issues.get((row.get("dataset"), key))
        if issue is None:
            _count(skipped, f"not in SWE-bench {row.get('dataset')}")
            continue
        truth = row.get("ground_truth") or {}
        regions = [r for r in truth.get("read_core_regions") or [] if isinstance(r, dict) and r.get("path")]
        lines = [(r["path"], int(r["start"]), int(r["end"])) for r in regions if 1 <= int(r["start"]) <= int(r["end"])]
        task = _task(
            SWE_EXPLORE,
            str(row.get("instance_id")),
            key,
            issue.get("repo"),
            issue.get("base_commit"),
            issue.get("problem_statement"),
            list(truth.get("read_core_files") or []) + [r["path"] for r in regions],
            [],
            lines,
            issue.get("repo_language") if row.get("dataset") == "pro" else None,
        )
        if _usable(task, skipped):
            tasks.append(task)
    return tasks, skipped


def patch_answers(patch: str) -> tuple[list[str], list[tuple[str, int, int]]]:
    """The files a unified diff changes that exist before it, and the lines it changes there: a removed line, or for
    an insertion the line before it (line 1 at the top of a file)."""
    files: list[str] = []
    lines: list[tuple[str, int, int]] = []
    path: str | None = None
    old = 0
    for text in patch.splitlines():
        if m := DIFF_FILE.match(text):
            path = m[1]
            continue
        if text.startswith("--- "):
            path = None if text[4:].strip() == "/dev/null" else (text[6:].strip() if text[4:6] == "a/" else path)
            if path and path not in files:
                files.append(path)
            continue
        if text.startswith("+++ "):
            continue
        if m := HUNK.match(text):
            old = int(m[1])
            continue
        if path is None or not text:
            continue
        if text[0] == " ":
            old += 1
        elif text[0] == "-":
            lines.append((path, old, old))
            old += 1
        elif text[0] == "+":
            at = max(old - 1, 1)
            lines.append((path, at, at))
    return files, lines


def read_locbench(cache: Path) -> tuple[list[dict], dict]:
    tasks, skipped = [], {}
    for row in _source_rows(cache, LOCBENCH, SOURCES[LOCBENCH][0]):
        files, lines = patch_answers(row.get("patch") or "")
        nodes = []
        for entry in row.get("edit_functions") or []:
            path, sep, qual = str(entry).rpartition(":")
            if sep and path in files and qual:
                nodes.append(f"{path}::{qual}")
        task = _task(
            LOCBENCH,
            str(row.get("instance_id")),
            instance_key(str(row.get("instance_id"))),
            row.get("repo"),
            row.get("base_commit"),
            row.get("problem_statement"),
            files,
            nodes,
            lines,
        )
        if _usable(task, skipped):
            tasks.append(task)
    return tasks, skipped


def _loc_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        found = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return {}
    return found if isinstance(found, dict) else {}


def _loc_qualname(key: str) -> str | None:
    """``('Class', 'func', 12)`` of MuLocBench's ``Loc`` as ``Class.func``; None for ``(None, None, None)``."""
    try:
        parts = ast.literal_eval(key)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parts, tuple) or len(parts) < 2:
        return None
    names = [p for p in parts[:2] if isinstance(p, str) and p]
    return ".".join(names) or None


def read_mulocbench(cache: Path) -> tuple[list[dict], dict]:
    tasks, skipped = [], {}
    for row in _source_rows(cache, MULOCBENCH, SOURCES[MULOCBENCH][0]):
        repo = f"{row.get('organization')}/{row.get('repo_name')}" if row.get("organization") else None
        loc = _loc_dict(row.get("file_loc"))
        files, nodes, lines = [], [], []
        for entry in loc.get("files") or []:
            path = entry.get("path")
            if not path or entry.get("status") == "added":
                continue
            files.append(path)
            located = entry.get("Loc") or {}
            if isinstance(located, list):  # a few rows give the lines alone
                located = {"(None, None, None)": located}
            for key, changes in located.items() if isinstance(located, dict) else ():
                qual = _loc_qualname(key)
                if qual:
                    nodes.append(f"{path}::{qual}")
                if isinstance(changes, dict):
                    changes = [n for kind in ("mod", "add") for n in changes.get(kind) or []]
                lines += [(path, n, n) for n in changes or [] if type(n) is int and n >= 1]
        number = NUMBER.search(row.get("pr_html_url") or "") or NUMBER.search(row.get("iss_html_url") or "")
        name = f"{row.get('organization')}__{row.get('repo_name')}-{number[1] if number else '?'}"
        issue = NUMBER.search(row.get("iss_html_url") or "")
        task = _task(
            MULOCBENCH,
            f"{row.get('organization')}__{row.get('repo_name')}-issue-{issue[1] if issue else '?'}",
            instance_key(name),
            repo,
            row.get("base_commit") or loc.get("base_commit"),
            "\n\n".join(p for p in (row.get("title"), row.get("body")) if p),
            files,
            nodes,
            lines,
        )
        if _usable(task, skipped):
            tasks.append(task)
    return tasks, skipped


READERS = {
    CONTEXTBENCH: read_contextbench,
    SWE_EXPLORE: read_swe_explore,
    LOCBENCH: read_locbench,
    MULOCBENCH: read_mulocbench,
}
