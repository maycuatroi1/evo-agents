"""``evo-agents kg eval``: how well kg_search and kg_context find what a question asks for, next to grep.

A question set is a JSONL file, one question a line:

    {"id": "q1", "question": "where are results ranked", "type": "words", "project": "demo",
     "files": ["app:app/search.py"], "nodes": ["symbol:app:app/search.py::rank_results"]}

``question`` is what the tools are asked, ``type`` groups the questions in the report, and the answers are ``files``
(``repo:path``, or a path in any repo) and ``nodes`` (node ids or aliases); a question needs at least one, and each
entry is one answer. ``project`` picks the graph (default: the command's ``--project``, else the project of the
current directory), ``repo`` keeps grep to one repo of the project, and ``id`` defaults to ``line N``. Other keys
are kept for whoever wrote the set and not read.

A set may open with a header line, ``{"set": "evo-agents", "version": 3}``: the set's name and version, which the report
names. The hub keeps a project's versioned set (``evo-agents hub kg eval-set push|pull``), and ``pull`` writes that
header. Each question falls in one part of the set by the hash of its id (``split_of``): HOLDOUT_SHARE of them in
``holdout``, the rest in ``tune``, the same on every machine and on the hub. Ranking is tuned on ``tune``
(``--split tune``); ``holdout`` is what the hub's nightly evaluation asks (``evo_agents.hub.kg_health_eval``), and the
ranking code may not import this module (lint-imports, contract kg-ranking-not-eval).

Each question goes through each method with the same fixed budget:

- ``kg_search``: the question as the query, ``limit`` results, read in order.
- ``kg_context``: the question as the query, ``budget_tokens``, its nodes in order (the seeds first).
- ``grep``: ``git grep`` of the question's terms (the whole question when it has no space, then its words of three
  letters or more that are not stop words, at most GREP_MAX_TERMS) in the checkouts of the project's repos: files
  ranked by how many terms they hold, then by how many lines match, then by path, printed ``path:line:text`` with at
  most GREP_LINES_PER_FILE lines a file, cut at ``budget_tokens``. Only the files shown count.

A node of the graph answers a file when its path (``props.path``, or the file a section belongs to) is the answer's
and its repo, when the node names one, is the answer's too; it answers a node when its id or one of its aliases is
that node. A file grep shows answers a file the same way, and a node whose id names a file (``symbol:R:P::Q``,
``R:file:P``) when that is the file: grep finds a symbol by the file that defines it.

The report has, per question type and method: the questions run, Recall@1 and Recall@5 (the share of a question's
answers in its first k results, averaged over the questions), hit@5 (the share of questions with an answer in the
first 5), capped Recall@5 (the answers in the first 5 over min(5, the question's answers), averaged), MRR (one over the
rank of the first answer found, 0 when none is), the share of questions with no result (an error counts as none), the
answers found anywhere in the output per 1,000 tokens of output (summed over the questions), the mean tokens of an
output, the tokens per hit (the mean tokens over hit@5) and per answer found (the mean tokens over Recall@5), None when
nothing was found, and the 95th percentile of the time a call took (nearest rank). Tokens are estimated as in the
hub's handoff: UTF-8 bytes / 3, rounded up.

The command only reads: the graph through the tools of a local store or the hub, the checkouts through git.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.harness import repo_paths
from evo_agents.kg.project import ProjectError, resolve_project

METHODS = ("kg_search", "kg_context", "grep")
KS = (1, 5)
BYTES_PER_TOKEN = 3  # as the hub's handoff counts tokens (evo_agents.hub.handoff)
DEFAULT_LIMIT = 10  # kg_search's own default
DEFAULT_BUDGET_TOKENS = 3000  # kg_context's own default
MIN_BUDGET_TOKENS, MAX_BUDGET_TOKENS = 500, 8000  # what kg_context takes
MAX_LIMIT = 25  # what kg_search takes
GREP_MAX_TERMS = 8
GREP_MIN_TERM = 3
GREP_LINES_PER_FILE = 3
GREP_LINE_CHARS = 200
GREP_TIMEOUT = 60.0
ALL_TYPES = "all"
HIT_K = 5  # hit@5, capped Recall@5 and the tokens per hit and per answer found read the first HIT_K results
TUNE, HOLDOUT = "tune", "holdout"
SPLITS = (TUNE, HOLDOUT)
HOLDOUT_SHARE = 0.3  # of the questions of a set, by the hash of their id: changing it moves questions between parts
# `kg eval-questions` (evo_agents.hub.kg_eval): the type of its questions, what of a step asks, the most files a step
# may change and still make one question. Here so that building the command line imports nothing of the hub.
PLAN_STEP_TYPE = "plan-step"
PLAN_TEXTS = ("title", "what")
DEFAULT_MAX_FILES = 20
WORD = re.compile(r"\w+")
STOP_WORDS = frozenset(
    "the and for with from that this these those what where which when how does did into are was were has have had "
    "not but can its our out use used using all any get set new who why will would should could about there their "
    "của cho các những một trong được không với khi này thì để từ theo nào bằng lên vào".split()
)
FILE_ID = re.compile(r"^(?P<repo>[^:#]+):file:(?P<path>[^#]+)")
SYMBOL_ID = re.compile(r"^symbol:(?P<repo>[^:]+):(?P<path>.+?)::")


class QuestionError(ValueError):
    """A question set that cannot be read; the message names each bad line."""


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    type: str
    project: str | None = None
    repo: str | None = None
    files: tuple[tuple[str | None, str], ...] = ()  # (repo or None, path)
    nodes: tuple[str, ...] = ()

    @property
    def answers(self) -> int:
        return len(self.files) + len(self.nodes)

    @property
    def split(self) -> str:
        return split_of(self.id)


def split_of(question_id: str) -> str:
    """The part of a set a question falls in, ``tune`` or ``holdout``, by the SHA-256 of its id: fixed for good."""
    digest = hashlib.sha256(question_id.encode()).digest()
    return HOLDOUT if int.from_bytes(digest[:8], "big") < HOLDOUT_SHARE * 2**64 else TUNE


@dataclass(frozen=True)
class QuestionSet:
    """A question set as read: its questions, and its name and version when a header line gives them."""

    questions: list[Question]
    name: str | None = None
    version: int | None = None

    def part(self, split: str | None) -> list[Question]:
        """The questions of ``split`` (``tune`` or ``holdout``); all of them for None or ``all``."""
        return [q for q in self.questions if split in (None, ALL_TYPES) or q.split == split]


@dataclass
class Outcome:
    """One method on one question: the rank of each answer (files, then nodes; None when not shown)."""

    question: Question
    method: str
    ranks: list[int | None] = field(default_factory=list)
    count: int = 0
    tokens: int = 0
    ms: float = 0.0
    error: str | None = None
    skipped: str | None = None  # why the method could not run on this question: it is left out of the numbers

    @property
    def found(self) -> int:
        return sum(1 for rank in self.ranks if rank is not None)

    def within(self, k: int) -> int:
        return sum(1 for rank in self.ranks if rank is not None and rank <= k)

    def recall(self, k: int) -> float:
        return self.within(k) / max(len(self.ranks), 1)

    def hit(self, k: int) -> bool:
        return self.within(k) > 0

    def capped_recall(self, k: int) -> float:
        """The answers in the first ``k`` over min(k, the answers): 1.0 when the first k are all answers."""
        return self.within(k) / max(min(k, len(self.ranks)), 1)

    def reciprocal_rank(self) -> float:
        best = min((rank for rank in self.ranks if rank is not None), default=None)
        return 0.0 if best is None else 1.0 / best

    def to_json(self) -> dict:
        return {
            "id": self.question.id,
            "type": self.question.type,
            "split": self.question.split,
            "project": self.question.project,
            "method": self.method,
            "ranks": self.ranks,
            "count": self.count,
            "found": self.found,
            "tokens": self.tokens,
            "ms": round(self.ms, 1),
            "error": self.error,
            "skipped": self.skipped,
        }


@dataclass
class Report:
    settings: dict
    rows: list[dict]
    outcomes: list[Outcome]

    def to_json(self) -> dict:
        return {**self.settings, "rows": self.rows, "results": [o.to_json() for o in self.outcomes]}


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text.encode()) / BYTES_PER_TOKEN)


# -- the question set --------------------------------------------------------------------------------------------


def split_file(text: str) -> tuple[str | None, str]:
    """``repo:path`` into (repo, path); a path without a repo into (None, path). A leading ``./`` goes."""
    repo, sep, path = text.partition(":")
    if not sep:
        repo, path = None, text
    while path.startswith("./"):
        path = path[2:]
    return repo or None, path


def _strings(value, key: str, problems: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        problems.append(f"{key} is a list of non-empty strings")
        return []
    return [v.strip() for v in value]


def _optional(entry: dict, key: str, problems: list[str]) -> str | None:
    value = entry.get(key)
    if value is None or (isinstance(value, str) and value.strip()):
        return value.strip() if value else None
    problems.append(f"{key} is a non-empty string")
    return None


def parse_question(entry, number: int) -> tuple[Question | None, list[str]]:
    """One line of a question set, or the problems that keep it from being one."""
    if not isinstance(entry, dict):
        return None, ["not a JSON object"]
    problems: list[str] = [f"{key} is required" for key in ("question", "type") if entry.get(key) is None]
    text = _optional(entry, "question", problems)
    kind = _optional(entry, "type", problems)
    files = _strings(entry.get("files"), "files", problems)
    nodes = _strings(entry.get("nodes"), "nodes", problems)
    if not files and not nodes and not problems:
        problems.append("no answer: give files, nodes or both")
    raw_id = entry.get("id", f"line {number}")
    if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
        problems.append("id is a string or a number")
    project = _optional(entry, "project", problems)
    repo = _optional(entry, "repo", problems)
    if problems:
        return None, problems
    question = Question(
        id=str(raw_id),
        text=text,
        type=kind,
        project=project,
        repo=repo,
        files=tuple(split_file(f) for f in files),
        nodes=tuple(nodes),
    )
    return question, []


def is_header(entry) -> bool:
    """Whether a line of a set is its header: an object with ``set`` and no ``question``."""
    return isinstance(entry, dict) and "set" in entry and "question" not in entry


def _header(entry: dict, problems: list[str]) -> tuple[str | None, int | None]:
    name, version = entry.get("set"), entry.get("version")
    if not isinstance(name, str) or not name.strip():
        problems.append("line 1: the header's set is a non-empty string")
        name = None
    if version is not None and (type(version) is not int or version < 1):
        problems.append("line 1: the header's version is a whole number from 1")
        version = None
    return (name.strip() if name else None), version


def parse_set(lines: Iterable[str]) -> QuestionSet:
    """A question set: an optional header line, then a question a line; QuestionError names every bad line."""
    questions: list[Question] = []
    problems: list[str] = []
    seen: set[str] = set()
    name = version = None
    first = True
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not JSON ({exc.msg})")
            first = False
            continue
        if first and is_header(entry):
            name, version = _header(entry, problems)
            first = False
            continue
        first = False
        question, found = parse_question(entry, number)
        if question is not None and question.id in seen:
            found = [f"id {question.id!r} is used twice"]
        problems += [f"line {number}: {p}" for p in found]
        if question is not None and not found:
            seen.add(question.id)
            questions.append(question)
    if problems:
        shown = problems[:20] + ([f"... {len(problems) - 20} more"] if len(problems) > 20 else [])
        raise QuestionError("the question set has problems:\n  " + "\n  ".join(shown))
    if not questions:
        raise QuestionError("the question set has no question")
    return QuestionSet(questions, name, version)


def parse_questions(lines: Iterable[str]) -> list[Question]:
    return parse_set(lines).questions


def load_set(path: Path | str) -> QuestionSet:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise QuestionError(f"cannot read {path}: {exc.strerror or exc}") from None
    return parse_set(text.splitlines())


def load_questions(path: Path | str) -> list[Question]:
    return load_set(path).questions


# -- matching answers --------------------------------------------------------------------------------------------


def id_file(node_id: str) -> tuple[str, str] | None:
    """(repo, path) of the file a node id names (``R:file:P``, ``R:file:P#anchor``, ``symbol:R:P::Q``), or None."""
    match = SYMBOL_ID.match(node_id) or FILE_ID.match(node_id)
    return (match["repo"], match["path"]) if match else None


def node_file(node: dict) -> tuple[str | None, str | None]:
    """(repo, path) of the file a result node stands for or lies in; the repo is None when the node names none."""
    props = node.get("props") or {}
    repo = props.get("repo")
    key = props.get("key")
    if repo is None and isinstance(key, list) and key and isinstance(key[0], str):
        repo = key[0]  # a Symbol's PG-Keys key: [repo, path, qualname]
    path = props.get("path")
    if not path and isinstance(props.get("item"), str):
        named = id_file(props["item"])  # a Section: the document it is part of
        path = named[1] if named else None
    return repo, path


def file_matches(answer: tuple[str | None, str], repo: str | None, path: str | None) -> bool:
    want_repo, want_path = answer
    return path == want_path and (want_repo is None or repo is None or repo == want_repo)


def node_ranks(question: Question, nodes: list[dict]) -> list[int | None]:
    """The rank (from 1) of the first result node that answers each answer, files first, then nodes."""
    located = [(node_file(n), {n.get("id"), *(n.get("aliases") or [])}) for n in nodes]
    ranks: list[int | None] = []
    for answer in question.files:
        ranks.append(next((i for i, ((r, p), _) in enumerate(located, 1) if file_matches(answer, r, p)), None))
    for answer in question.nodes:
        ranks.append(next((i for i, (_, ids) in enumerate(located, 1) if answer in ids), None))
    return ranks


def file_ranks(question: Question, files: list[tuple[str, str]]) -> list[int | None]:
    """The rank (from 1) of the first file that answers each answer; a node answers by the file its id names."""
    answers = list(question.files) + [id_file(node) for node in question.nodes]
    return [
        None if answer is None else next((i for i, (r, p) in enumerate(files, 1) if file_matches(answer, r, p)), None)
        for answer in answers
    ]


# -- the methods -------------------------------------------------------------------------------------------------


def run_kg(session, method: str, question: Question, limit: int, budget_tokens: int) -> Outcome:
    """Ask the graph through ``session.call`` (a local Session or the hub's RemoteSession) as an agent would."""
    if method == "kg_search":
        args, key = {"query": question.text, "limit": limit}, "results"
    else:
        args, key = {"query": question.text, "budget_tokens": budget_tokens}, "nodes"
    start = time.perf_counter()
    result = session.call(method, args)
    ms = (time.perf_counter() - start) * 1000
    text = "\n".join(b.get("text", "") for b in result.get("content") or [] if isinstance(b, dict))
    data = result.get("structuredContent") or {}
    nodes = [] if result.get("isError") else [n for n in data.get(key) or [] if isinstance(n, dict)]
    return Outcome(
        question=question,
        method=method,
        ranks=node_ranks(question, nodes),
        count=len(nodes),
        tokens=estimate_tokens(text),
        ms=ms,
        error=text.removeprefix("error: ")[:300] if result.get("isError") else None,
    )


def grep_terms(text: str) -> list[str]:
    """What grep looks for: the whole question when it has no space, then its words that carry meaning."""
    stripped = text.strip()
    terms = [stripped] if stripped and not any(ch.isspace() for ch in stripped) else []
    seen = {t.lower() for t in terms}
    for word in WORD.findall(text):
        low = word.lower()
        if len(word) >= GREP_MIN_TERM and low not in STOP_WORDS and low not in seen:
            seen.add(low)
            terms.append(word)
    return terms[:GREP_MAX_TERMS]


@dataclass
class GrepFile:
    repo: str
    path: str
    terms: set = field(default_factory=set)
    lines: int = 0
    shown: list = field(default_factory=list)  # (line number, text), at most GREP_LINES_PER_FILE


def grep_checkout(repo: str, checkout: Path, terms: list[str], ref: str | None) -> dict[str, GrepFile]:
    """Files of one checkout with a line holding a term (fixed strings, any case); raises RuntimeError when git
    fails. With ``ref``, the tree of that commit instead of the working tree."""
    command = ["git", "-C", str(checkout), "grep", "-z", "-n", "-I", "-i", "-F", "--full-name", "--no-color"]
    for term in terms:
        command += ["-e", term]
    command += [ref, "--"] if ref else ["--"]
    result = subprocess.run(command, capture_output=True, timeout=GREP_TIMEOUT)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep in {checkout}: {result.stderr.decode('utf-8', 'replace').strip()[:200]}")
    files: dict[str, GrepFile] = {}
    lowered = [t.lower() for t in terms]
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        path, _, rest = line.partition("\0")
        number, _, content = rest.partition("\0")
        if ref and path.startswith(f"{ref}:"):
            path = path[len(ref) + 1 :]
        entry = files.setdefault(path, GrepFile(repo, path))
        low = content.lower()
        entry.terms.update(t for t in lowered if t in low)
        entry.lines += 1
        if len(entry.shown) < GREP_LINES_PER_FILE:
            entry.shown.append((number, content.strip()[:GREP_LINE_CHARS]))
    return files


def grep_output(files: list[GrepFile], budget_tokens: int) -> tuple[str, list[tuple[str, str]]]:
    """The ``path:line:text`` lines of the ranked files within the budget, and the files they show."""
    budget = budget_tokens * BYTES_PER_TOKEN
    lines: list[str] = []
    used = 0
    shown: list[tuple[str, str]] = []
    for entry in files:
        for number, content in entry.shown:
            line = f"{entry.path}:{number}:{content}"
            if used + len(line.encode()) + 1 > budget:
                return "\n".join(lines), shown
            used += len(line.encode()) + 1
            lines.append(line)
            if not shown or shown[-1] != (entry.repo, entry.path):
                shown.append((entry.repo, entry.path))
    return "\n".join(lines), shown


def run_grep(question: Question, checkouts: dict[str, Path], budget_tokens: int, ref: str | None) -> Outcome:
    if question.repo is not None:
        if question.repo not in checkouts:
            return Outcome(question, "grep", skipped=f"no checkout of repo {question.repo}")
        checkouts = {question.repo: checkouts[question.repo]}
    if not checkouts:
        return Outcome(question, "grep", skipped=f"no checkout of project {question.project or '(default)'}")
    terms = grep_terms(question.text)
    if not terms:
        return Outcome(question, "grep", ranks=[None] * question.answers)
    start = time.perf_counter()
    found: list[GrepFile] = []
    try:
        for repo, checkout in sorted(checkouts.items()):
            found += grep_checkout(repo, checkout, terms, ref).values()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return Outcome(question, "grep", ranks=[None] * question.answers, error=str(exc)[:300])
    found.sort(key=lambda f: (-len(f.terms), -f.lines, f.repo, f.path))
    text, shown = grep_output(found, budget_tokens)
    ms = (time.perf_counter() - start) * 1000
    return Outcome(
        question, "grep", ranks=file_ranks(question, shown), count=len(shown), tokens=estimate_tokens(text), ms=ms
    )


# -- running and reporting ---------------------------------------------------------------------------------------


def is_checkout(path: Path) -> bool:
    """Whether ``path`` is inside a git working tree, which ``git grep`` needs."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, timeout=GREP_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def project_checkouts(project: str | None, overrides: dict[str, Path] | None = None) -> dict[str, Path]:
    """The checkouts grep reads for ``project``, by repo name: the repos of its harness that are git checkouts on this
    machine, and the harness itself under its directory's name when it is one, then ``overrides`` as given."""
    found: dict[str, Path] = {}
    try:
        harness = resolve_project(project).harness
    except ProjectError:
        harness = None
    if harness is not None:
        candidates = [(harness.root.name, harness.root)]
        candidates += [(repo.get("name"), path) for repo, path in repo_paths(harness)]
        for name, path in candidates:
            if isinstance(name, str) and path is not None and path.is_dir() and is_checkout(path):
                found[name] = path
    found.update(overrides or {})
    return found


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]


def summarize(outcomes: list[Outcome], kind: str, method: str) -> dict:
    """One row of the report: the numbers of ``method`` over the outcomes it ran on."""
    ran = [o for o in outcomes if o.skipped is None]
    n = len(ran)
    tokens = sum(o.tokens for o in ran)
    row = {"type": kind, "method": method, "n": n, "skipped": len(outcomes) - n}
    for k in KS:
        row[f"recall@{k}"] = round(sum(o.recall(k) for o in ran) / n, 4) if n else 0.0
    hit = sum(1 for o in ran if o.hit(HIT_K)) / n if n else 0.0
    recall = sum(o.recall(HIT_K) for o in ran) / n if n else 0.0
    mean = tokens / n if n else 0.0
    row[f"hit@{HIT_K}"] = round(hit, 4)
    row[f"capped_recall@{HIT_K}"] = round(sum(o.capped_recall(HIT_K) for o in ran) / n, 4) if n else 0.0
    row["mrr"] = round(sum(o.reciprocal_rank() for o in ran) / n, 4) if n else 0.0
    row["empty"] = round(sum(1 for o in ran if o.count == 0) / n, 4) if n else 0.0
    row["errors"] = sum(1 for o in ran if o.error)
    row["correct_per_1k_tokens"] = round(1000 * sum(o.found for o in ran) / tokens, 3) if tokens else 0.0
    row["mean_tokens"] = round(mean, 1)
    row["tokens_per_hit"] = round(mean / hit, 1) if hit else None
    row["tokens_per_file"] = round(mean / recall, 1) if recall else None
    row["p95_ms"] = round(p95([o.ms for o in ran]), 1)
    return row


def report_rows(outcomes: list[Outcome], methods: Iterable[str]) -> list[dict]:
    """A row per question type and method, in the order the types first appear, then one per method over all."""
    types = list(dict.fromkeys(o.question.type for o in outcomes))
    rows = []
    for kind in [*types, ALL_TYPES]:
        for method in methods:
            picked = [o for o in outcomes if o.method == method and kind in (ALL_TYPES, o.question.type)]
            if picked:
                rows.append(summarize(picked, kind, method))
    return rows


def evaluate(
    questions: list[Question],
    *,
    session_for: Callable[[str | None], object],
    checkouts_for: Callable[[str | None], dict[str, Path]],
    methods: Iterable[str] = METHODS,
    limit: int = DEFAULT_LIMIT,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    grep_ref: str | None = None,
    question_set: QuestionSet | None = None,
    split: str | None = None,
) -> Report:
    """Run every method on every question. ``session_for`` and ``checkouts_for`` take a question's project (None:
    the default one) and are called once per project. ``question_set`` and ``split`` are only named in the settings:
    pass the questions of the split."""
    methods = tuple(methods)
    sessions: dict[str | None, object] = {}
    checkouts: dict[str | None, dict[str, Path]] = {}
    outcomes: list[Outcome] = []
    for question in questions:
        for method in methods:
            if method == "grep":
                if question.project not in checkouts:
                    checkouts[question.project] = checkouts_for(question.project)
                outcomes.append(run_grep(question, checkouts[question.project], budget_tokens, grep_ref))
                continue
            if question.project not in sessions:
                sessions[question.project] = session_for(question.project)
            outcomes.append(run_kg(sessions[question.project], method, question, limit, budget_tokens))
    settings = {
        "questions": len(questions),
        "methods": list(methods),
        "limit": limit,
        "budget_tokens": budget_tokens,
        "grep_ref": grep_ref,
        "set": question_set.name if question_set else None,
        "version": question_set.version if question_set else None,
        "split": split or ALL_TYPES,
    }
    return Report(settings, report_rows(outcomes, methods), outcomes)


COLUMNS = (
    ("TYPE", "type", "{}"),
    ("METHOD", "method", "{}"),
    ("N", "n", "{}"),
    ("R@1", "recall@1", "{:.3f}"),
    ("R@5", "recall@5", "{:.3f}"),
    ("HIT@5", f"hit@{HIT_K}", "{:.3f}"),
    ("R@5 CAP", f"capped_recall@{HIT_K}", "{:.3f}"),
    ("MRR", "mrr", "{:.3f}"),
    ("EMPTY", "empty", "{:.0%}"),
    ("OK/1K TOK", "correct_per_1k_tokens", "{:.2f}"),
    ("MEAN TOK", "mean_tokens", "{:.0f}"),
    ("TOK/HIT", "tokens_per_hit", "{:.0f}"),
    ("TOK/FILE", "tokens_per_file", "{:.0f}"),
    ("P95 MS", "p95_ms", "{:.0f}"),
)


def _cell(value, fmt: str) -> str:
    return "-" if value is None else fmt.format(value)


def render(report: Report) -> str:
    """The rows as a table, then a line for each method that failed or skipped questions."""
    table = [[title for title, _, _ in COLUMNS]]
    table += [[_cell(row[key], fmt) for _, key, fmt in COLUMNS] for row in report.rows]
    widths = [max(len(cells[i]) for cells in table) for i in range(len(COLUMNS))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip() for cells in table]
    s = report.settings
    named = f"set {s['set']}" + (f" v{s['version']}" if s.get("version") else "") + ", " if s.get("set") else ""
    part = f" of part {s['split']}" if s.get("split", ALL_TYPES) != ALL_TYPES else ""
    lines.append(
        f"{named}{s['questions']} question(s){part}; kg_search limit {s['limit']}, kg_context and grep within"
        f" {s['budget_tokens']} tokens; grep on {s['grep_ref'] or 'the working tree'}"
    )
    for method in report.settings["methods"]:
        mine = [o for o in report.outcomes if o.method == method]
        failed = [o for o in mine if o.error]
        skipped = [o for o in mine if o.skipped]
        if failed:
            lines.append(
                f"{method}: {len(failed)} call(s) answered with an error; first, on {failed[0].question.id}: {failed[0].error}"
            )
        if skipped:
            reasons = sorted({o.skipped for o in skipped})
            lines.append(f"{method}: skipped {len(skipped)} question(s): {'; '.join(reasons[:3])}")
    return "\n".join(lines)
