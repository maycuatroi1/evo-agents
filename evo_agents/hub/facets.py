"""The deterministic facets of a session or a run: what its transcript or its log says, counted without any model.
``evo_agents.hub.facet_readers`` reads a Claude Code transcript, a Codex rollout and a run's events into a ``Tally``,
the job ``hub.analyze_facets`` (``evo_agents.hub.facet_job``) keeps each facet in session_facets, and
``evo_agents.hub.server.collect`` counts the night's figures from them. Pure functions over JSON values, standard
library only.

A facet has FACET_VERSION: a facet of an older version is made again. Its body holds, besides the fields of a session
digest that the figures and the evidence read (``tools``, ``bash``, ``errors``, ``commands``, ``repeated_commands``,
``user_turns``, ``files_edited``; ``evo_agents.hub.digest``), with the same shapes:

- ``errors``: each with its ``cause``, the environment cause ``review.environment_cause`` reads in its text, else
  ``work``, and ``at``, where it first came; ``causes``, the failures per cause;
- ``corrections``: the indexes of the turns of ``user_turns`` that read as a correction (``review.is_correction``);
- ``files_read`` and ``files_edited``: the paths the read tools read and the edit tools wrote, in order;
- ``kg``: the calls of the knowledge graph's tools (a name ending in ``kg_<op>``): per tool, the calls and those whose
  result named no node (``empty``); the searches (Grep, Glob, a shell grep, rg or find) after a kg call (``searches``)
  and the first search after an empty one (``fallbacks``); and of the files edited after the first kg call
  (``edited``), those an earlier kg result named (``in_results``) and those among its first KG_TOP paths (``in_top``);
- ``context``: the context of the first request of the model in the main thread and the largest (``first``, ``peak``);
  ``compactions`` and ``compaction`` (by trigger, the largest context compacted);
- ``tools[].output_chars``, the characters of the results the model read in the main thread, and ``tools[].ms`` with
  ``tools[].buckets``, the time of the calls that finished, in the buckets of BUCKETS_MS;
- ``usage``: the tokens, cache reads and cache writes included (``digest.USAGE_KEYS``), and ``cost_usd``, null when
  the source says none;
- ``hooks``: per hook (its name and its command), how many ran, their time and buckets, how many failed or timed out;
- ``skills``: the skills the agent loaded (the Skill tool) or that came back into its context, and the characters of
  their text that came into the main thread (``chars``: a load, by the Skill tool or typed as /name, or a compaction
  bringing it back);
- ``api_errors``: the errors of the model's API the runtime wrote, by text, each with its cause;
- ``sources``: the characters each source of context (CONTEXT_SOURCES) put into the main thread of a Claude Code
  session: the always-loaded files (CLAUDE.md, AGENTS.md, a memory index), the instructions of MCP servers, the list of
  skills, the text of hooks; the output of tools is ``tools[].output_chars``. Besides them, ``skills_again``, the part of
  ``skills`` that came as a whole list while the context, since its last compaction, already held one, and
  ``skill_bodies``, the text of the skills loaded into the context, which none of them counts. ``loaded``: each always-loaded file, its
  path (a home directory as ``~/``), its type, its largest size in characters and how often it came. Both are null for
  a Codex session, a run and a digest, whose sources the facet does not see.

A facet built from a digest (``digest_facet``) holds what the digest holds and null for what it does not know. Each
list is bounded and ordered by its counts and then its text, so the same content makes the same facet. ``summary``
gives the numbers of a facet the weekly series add up.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from evo_agents.hub import digest, review
from evo_agents.hub.digest import MAX_COUNT, TOOL_NAME, USAGE_KEYS

FACET_VERSION = 2  # 2: sources.skills_again, sources.skill_bodies and skills[].chars
KINDS = ("session", "run")
SOURCES = ("transcript", "digest", "run_log", "events")
WORK = "work"  # the cause of a failure of the work itself, not of its environment
BUCKETS_MS = (100, 300, 1000, 3000, 10_000, 30_000, 100_000, 300_000)  # the upper bounds; the last bucket is above
KG_TOP = 5
KG_TOOL = re.compile(r"(?:^|__|/)(kg_[a-z_]+)$")  # mcp__<server>__kg_search, or <server>/kg_search in a run
KG_LOOKUPS = frozenset({"kg_search", "kg_context", "kg_node", "kg_impact", "kg_path", "kg_more"})
SEARCH_TOOLS = frozenset({"Grep", "Glob"})
SEARCH_PROGRAMS = frozenset({"grep", "rg", "ag", "ack", "find", "git grep", "fd"})
MAX_ERRORS = 50
MAX_COMMANDS = 100
MAX_REPEATED = 30
MAX_USER_TURNS = 60
MAX_FILES = 200
MAX_TOOLS = 200
MAX_HOOKS = 50
MAX_SKILLS = 50
MAX_LOADED = 50  # always-loaded files a facet names
MAX_KG_PATHS = 500  # paths of kg results a facet remembers
MAX_RESULT_PATHS = 50  # paths of one kg result
MAX_NAME = digest.MAX_NAME
TEXT_CHARS = digest.ERROR_CHARS
COMMAND_CHARS = digest.COMMAND_CHARS
TURN_CHARS = digest.USER_TURN_CHARS
HOOK_CHARS = 200
# The sources of context a Claude Code transcript names besides the output of tools (``Tally.context_text``).
CONTEXT_SOURCES = ("always_loaded", "mcp_instructions", "skills", "hooks")
SKILLS_AGAIN = "skills_again"  # of ``skills``: a whole list that came while the context already held one
SKILL_BODIES = "skill_bodies"  # the text of the skills loaded into the context, on demand rather than fixed
FACET_SOURCES = (*CONTEXT_SOURCES, SKILLS_AGAIN, SKILL_BODIES)  # the keys of a facet's ``sources``
VERSION_NUMBER = re.compile(r"(?<=[=@v])\d+(?:\.\d+)+")
HOME = re.compile(r"(?:/Users|/home)/[^/\s]+/")
NODE_FILE = re.compile(r"^[^:\s]+:file:([^#]+)")
NODE_SYMBOL = re.compile(r"^symbol:[^:\s]+:(.+?)::")


def cut(text, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def timestamp(value) -> datetime | None:
    """A moment from an ISO 8601 text, in UTC when it names no zone; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def count(value) -> int | None:
    """A count from a JSON value: a whole number of 0 or more, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return min(int(value), MAX_COUNT)


def ranked(counts: Counter) -> list:
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))


def bucket(ms: int) -> int:
    for index, bound in enumerate(BUCKETS_MS):
        if ms <= bound:
            return index
    return len(BUCKETS_MS)


def hook_command(command) -> str:
    """A hook's command as its facet names it: blanks folded, a home directory as ``~/`` and version numbers as N, so
    the same hook on two machines or in two releases is one."""
    text = HOME.sub("~/", " ".join(str(command or "").split()))
    return cut(VERSION_NUMBER.sub("N", text), HOOK_CHARS)


@dataclass
class Timing:
    """How many times something ran, how long in all, the longest, and the buckets of BUCKETS_MS."""

    runs: int = 0
    ms: int = 0
    max_ms: int = 0
    buckets: list = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))

    def add(self, ms: int | None) -> None:
        if ms is None:
            return
        self.runs += 1
        self.ms = min(self.ms + ms, MAX_COUNT)
        self.max_ms = max(self.max_ms, ms)
        self.buckets[bucket(ms)] += 1


@dataclass
class Hook:
    timing: Timing = field(default_factory=Timing)
    seen: int = 0
    failed: int = 0
    timed_out: int = 0


# The knowledge graph's calls


def kg_op(tool: str) -> str | None:
    """The kg tool a call's name ends with (``kg_search``), or None for any other tool."""
    found = KG_TOOL.search(tool or "")
    return found.group(1) if found else None


def _node_path(node: dict) -> str | None:
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    if isinstance(props.get("path"), str) and props["path"]:
        return props["path"]
    ident = node.get("id") if isinstance(node.get("id"), str) else ""
    found = NODE_FILE.match(ident) or NODE_SYMBOL.match(ident)
    return found.group(1) if found else None


def kg_paths(text: str) -> tuple[int, list[str]]:
    """(the nodes, the paths of files in their order) a kg tool's result names: its JSON walked depth first, each
    object with an ``id`` a node. A result that is not JSON names none."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return 0, []
    nodes, paths, stack = 0, [], [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            if isinstance(item.get("id"), str):
                nodes += 1
                path = _node_path(item)
                if path and path not in paths and len(paths) < MAX_RESULT_PATHS:
                    paths.append(path)
            stack.extend(reversed([value for value in item.values() if isinstance(value, (list, dict))]))
    return nodes, paths


def _same_file(edited: str, path: str) -> bool:
    return edited == path or edited.endswith("/" + path.lstrip("/"))


class KgTrail:
    """The calls of the knowledge graph's tools of one session, and what the agent searched and edited after them."""

    def __init__(self):
        self.calls: Counter = Counter()
        self.empty: Counter = Counter()
        self.ranks: dict[str, int] = {}  # a path an earlier result named: the best rank it had, from 1
        self.searches = 0
        self.fallbacks = 0
        self.edited: dict[str, int | None] = {}  # a file edited after the first kg call: its best rank before
        self._missed = False  # the last kg lookup named no node, and no search came since

    @property
    def used(self) -> bool:
        """Whether a lookup of the graph came yet (``kg_status`` and the like name no file)."""
        return any(op in KG_LOOKUPS for op in self.calls)

    def result(self, op: str, text: str) -> None:
        self.calls[op] += 1
        if op not in KG_LOOKUPS:
            return
        nodes, paths = kg_paths(text)
        self._missed = nodes == 0
        if self._missed:
            self.empty[op] += 1
        for rank, path in enumerate(paths, start=1):
            if path not in self.ranks and len(self.ranks) >= MAX_KG_PATHS:
                continue
            self.ranks[path] = min(rank, self.ranks.get(path, rank))

    def failed(self, op: str) -> None:
        self.calls[op] += 1

    def search(self) -> None:
        if not self.used:
            return
        self.searches += 1
        if self._missed:
            self.fallbacks += 1
            self._missed = False

    def edit(self, path: str) -> None:
        if not self.used or path in self.edited or len(self.edited) >= MAX_FILES:
            return
        ranks = [rank for known, rank in self.ranks.items() if _same_file(path, known)]
        self.edited[path] = min(ranks) if ranks else None

    def body(self) -> dict:
        ranks = list(self.edited.values())
        return {
            "calls": sum(self.calls.values()),
            "empty": sum(self.empty.values()),
            "tools": [{"tool": op, "calls": n, "empty": self.empty[op]} for op, n in ranked(self.calls)],
            "searches": self.searches,
            "fallbacks": self.fallbacks,
            "edited": len(ranks),
            "in_results": sum(1 for rank in ranks if rank is not None),
            "in_top": sum(1 for rank in ranks if rank is not None and rank <= KG_TOP),
        }


# The tally


class Tally:
    """What a session's transcripts or a run's events say, one call, result, turn and line at a time; ``body`` is the
    facet. ``at`` names where a thing first came: ``FILE:LINE`` in a transcript, the seq of an event in a run."""

    def __init__(self, runtime: str | None):
        self.runtime = runtime
        self.messages = 0
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.models: Counter = Counter()
        self.calls: Counter = Counter()
        self.failed: Counter = Counter()
        self.output: Counter = Counter()
        self.timing: dict[str, Timing] = {}
        self.bash_calls: Counter = Counter()
        self.bash_failed: Counter = Counter()
        self.errors: dict[tuple[str, str], list] = {}  # (tool, text): [n, at]
        self.commands: dict[str, list] = {}  # command: [n, at]
        self.user_turns: list[str] = []
        self.read: dict[str, None] = {}
        self.edited: dict[str, None] = {}
        self.kg = KgTrail()
        self.context_first: int | None = None
        self.context_peak: int | None = None
        self.compactions: dict[str, str] = {}  # its id: its trigger
        self.compacted_tokens: int | None = None
        self.usage = dict.fromkeys(USAGE_KEYS, 0)
        self.cost: float | None = None
        self.hooks: dict[tuple[str, str], Hook] = {}
        self.skills: dict[str, list] = {}  # name: [calls, times it came back into the context, characters of its text]
        self.api_errors: Counter = Counter()
        self.sources: Counter | None = None  # FACET_SOURCES: characters; None until a Claude Code main thread came
        self.listed = False  # the main thread's context holds a whole skill list, since its last compaction
        self.loaded: dict[str, list] = {}  # an always-loaded file's path: [its type, its largest size, how often]
        self.files: list[dict] = []
        self._open: dict[str, tuple[str, str | None, datetime | None, str | None]] = {}  # call: (tool, program, at, kg)

    # Moments and messages

    def moment(self, at: datetime | None) -> None:
        if at is not None:
            self.first = at if self.first is None or at < self.first else self.first
            self.last = at if self.last is None or at > self.last else self.last

    def message(self, at: datetime | None = None) -> None:
        self.messages += 1
        self.moment(at)

    def model(self, name) -> None:
        if isinstance(name, str) and name and name != digest.SYNTHETIC_MODEL:
            self.models[cut(name, MAX_NAME)] += 1

    def context(self, tokens: int | None) -> None:
        if tokens:
            self.context_first = tokens if self.context_first is None else self.context_first
            self.context_peak = tokens if self.context_peak is None else max(self.context_peak, tokens)

    def add_usage(self, usage: dict) -> None:
        for key in USAGE_KEYS:
            found = count(usage.get(key))
            if found:
                self.usage[key] = min(self.usage[key] + found, MAX_COUNT)

    def turn(self, text: str) -> None:
        text = (text or "").strip()
        if text and not text.startswith("<") and len(self.user_turns) < MAX_USER_TURNS:
            self.user_turns.append(cut(text, TURN_CHARS))

    # Calls and results

    def call(self, call_id: str, tool: str, given, at: datetime | None, where) -> None:
        """A call of ``tool`` with input ``given`` (a mapping, or the command of a shell)."""
        tool = cut(tool or "unknown", MAX_NAME)
        self.calls[tool] += 1
        given = given if isinstance(given, dict) else {"command": given} if isinstance(given, str) else {}
        ran = self._shell(tool, given, where)
        self._paths(tool, given)
        if tool == "Skill" and isinstance(given.get("skill"), str) and given["skill"]:
            self.skills.setdefault(cut(given["skill"], MAX_NAME), [0, 0, 0])[0] += 1
        if tool in SEARCH_TOOLS or (ran is not None and ran in SEARCH_PROGRAMS):
            self.kg.search()
        if call_id:
            self._open[call_id] = (tool, ran, at, kg_op(tool))

    def _shell(self, tool: str, given: dict, where) -> str | None:
        command = given.get("command")
        if isinstance(command, list):
            command = " ".join(str(word) for word in command)
        if not (tool in digest.SHELL_TOOLS or tool in ("shell", "execute")) or not isinstance(command, str):
            return None
        command = command.strip()
        if not command:
            return None
        found = self.commands.setdefault(cut(command, COMMAND_CHARS), [0, where])
        found[0] += 1
        ran = digest.program(command) or "unknown"
        self.bash_calls[ran] += 1
        return ran

    def _paths(self, tool: str, given: dict) -> None:
        path = given.get("file_path") or given.get("notebook_path") or given.get("path")
        if not isinstance(path, str) or not path:
            return
        path = cut(path, digest.PATH_CHARS)
        if tool in digest.READ_TOOLS and len(self.read) < MAX_FILES:
            self.read.setdefault(path)
        elif tool in digest.EDIT_TOOLS:
            self.edit(path)

    def edit(self, path: str) -> None:
        if len(self.edited) < MAX_FILES:
            self.edited.setdefault(cut(path, digest.PATH_CHARS))
        self.kg.edit(path)

    def result(self, call_id: str, at: datetime | None, failed: bool, text: str, chars: int | None, where) -> None:
        """The result of call ``call_id``; ``chars`` the characters the model read of it, None outside the main
        thread."""
        tool, ran, started, op = self._open.pop(call_id, ("unknown", None, None, None))
        if chars is not None:
            self.output[tool] = min(self.output[tool] + chars, MAX_COUNT)
        if started is not None and at is not None:
            self.timing.setdefault(tool, Timing()).add(max(0, round((at - started).total_seconds() * 1000)))
        if op is not None and failed:
            self.kg.failed(op)
        elif op is not None:
            self.kg.result(op, text)
        if not failed:
            return
        self.failed[tool] += 1
        if ran is not None:
            self.bash_failed[ran] += 1
        key = (tool, cut(text.strip(), TEXT_CHARS))
        if key[1] and (key in self.errors or len(self.errors) < MAX_ERRORS * 4):
            self.errors.setdefault(key, [0, where])[0] += 1

    # Hooks, skills, compactions, the API

    def hook(self, name: str, command, ms, *, failed: bool = False, timed_out: bool = False) -> None:
        key = (cut(name or "unknown", MAX_NAME), hook_command(command))
        if key not in self.hooks and len(self.hooks) >= MAX_HOOKS * 4:
            return
        found = self.hooks.setdefault(key, Hook())
        found.seen += 1
        found.timing.add(count(ms))
        found.failed += failed
        found.timed_out += timed_out

    def skill_back(self, name) -> None:
        if isinstance(name, str) and name:
            self.skills.setdefault(cut(name, MAX_NAME), [0, 0, 0])[1] += 1

    def compaction(self, ident: str, trigger, tokens=None) -> None:
        self.compactions[ident] = str(trigger or "auto")
        self.listed = False
        tokens = count(tokens)
        if tokens:
            self.compacted_tokens = max(self.compacted_tokens or 0, tokens)

    def api_error(self, text: str) -> None:
        text = cut(text, TEXT_CHARS)
        if text:
            self.api_errors[text] += 1

    # The sources of context

    def sees_context(self) -> None:
        """A Claude Code main thread came: its sources of context count, from 0."""
        if self.sources is None:
            self.sources = Counter(dict.fromkeys(FACET_SOURCES, 0))

    def context_text(self, source: str, text) -> None:
        """``text`` came into the main thread's context from ``source`` (one of FACET_SOURCES)."""
        self.sees_context()
        if isinstance(text, str) and text:
            self.sources[source] = min(self.sources[source] + len(text), MAX_COUNT)

    def skill_list(self, text, whole: bool) -> None:
        """The skill list came into the main thread's context: ``whole``, or the skills added to it. A whole list while
        the context, since its last compaction or a /clear, already held one counts in SKILLS_AGAIN too."""
        self.context_text("skills", text)
        if whole and isinstance(text, str) and text:
            if self.listed:
                self.context_text(SKILLS_AGAIN, text)
            self.listed = True

    def cleared(self) -> None:
        """The person cleared the main thread's context (/clear)."""
        self.listed = False

    def skill_body(self, name, text) -> None:
        """The text of skill ``name`` came into the main thread's context: loaded, or brought back by a compaction."""
        if isinstance(text, str) and text:
            self.context_text(SKILL_BODIES, text)
            if isinstance(name, str) and name:
                found = self.skills.setdefault(cut(name, MAX_NAME), [0, 0, 0])
                found[2] = min(found[2] + len(text), MAX_COUNT)

    def load_file(self, path, kind, content) -> None:
        """An always-loaded file (CLAUDE.md, AGENTS.md, a memory index) came into the main thread's context."""
        self.context_text("always_loaded", content)
        path = cut(HOME.sub("~/", str(path or "").strip()), digest.PATH_CHARS)
        if not path or (path not in self.loaded and len(self.loaded) >= MAX_LOADED * 4):
            return
        found = self.loaded.setdefault(path, [cut(kind or "unknown", MAX_NAME), 0, 0])
        found[1] = max(found[1], len(content) if isinstance(content, str) else 0)
        found[2] += 1

    # The facet

    def body(self) -> dict:
        """The facet, as the module says."""
        return {
            "version": FACET_VERSION,
            "runtime": self.runtime,
            "messages": min(self.messages, MAX_COUNT),
            "started_at": self.first.isoformat() if self.first else None,
            "ended_at": self.last.isoformat() if self.last else None,
            **self._models(),
            "files": self.files,
            **self._tools(),
            **self._failures(),
            **self._commands(),
            "user_turns": self.user_turns,
            "corrections": [index for index, turn in enumerate(self.user_turns) if review.is_correction(turn)],
            "files_read": list(self.read),
            "files_edited": list(self.edited),
            "kg": self.kg.body(),
            "context": {"first": self.context_first, "peak": self.context_peak},
            "compactions": len(self.compactions),
            "compaction": {
                **{trigger: n for trigger, n in ranked(Counter(self.compactions.values()))},
                "largest_tokens": self.compacted_tokens,
            },
            "usage": dict(self.usage),
            "cost_usd": round(self.cost, 6) if self.cost is not None else None,
            "hooks": self._hooks(),
            "skills": [
                {"skill": name, "calls": calls, "returned": back, "chars": chars}
                for name, (calls, back, chars) in sorted(
                    self.skills.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[1][2], kv[0])
                )
            ][:MAX_SKILLS],
            "api_errors": [
                {"text": text, "n": n, "cause": review.environment_cause(text) or WORK}
                for text, n in ranked(self.api_errors)[:MAX_ERRORS]
            ],
            "sources": None if self.sources is None else {name: self.sources[name] for name in FACET_SOURCES},
            "loaded": None if self.sources is None else self._loaded(),
        }

    def _loaded(self) -> list[dict]:
        ordered = sorted(self.loaded.items(), key=lambda kv: (-kv[1][1], kv[0]))[:MAX_LOADED]
        return [{"path": path, "type": kind, "chars": chars, "loads": n} for path, (kind, chars, n) in ordered]

    def _models(self) -> dict:
        per_model = ranked(self.models)
        return {
            "model": per_model[0][0] if per_model else None,
            "models": [{"model": name, "messages": n} for name, n in per_model[: digest.MAX_MODELS]],
        }

    def _tools(self) -> dict:
        tools = []
        for name, n in ranked(self.calls)[:MAX_TOOLS]:
            timing = self.timing.get(name, Timing())
            entry = {TOOL_NAME: name, "calls": n, "errors": min(self.failed[name], n)}
            entry.update(output_chars=self.output[name], timed=timing.runs, ms=timing.ms, buckets=timing.buckets)
            tools.append(entry)
        bash = [
            {"program": name, "calls": n, "errors": min(self.bash_failed[name], n)}
            for name, n in ranked(self.bash_calls)[: digest.MAX_PROGRAMS]
        ]
        return {"tools": tools, "bash": bash}

    def _failures(self) -> dict:
        found = sorted(self.errors.items(), key=lambda kv: (-kv[1][0], kv[0][0], kv[0][1]))[:MAX_ERRORS]
        errors = []
        causes: Counter = Counter()
        for (tool, text), (n, where) in found:
            cause = review.environment_cause(text) or WORK
            errors.append({TOOL_NAME: tool, "text": text, "n": n, "cause": cause, "at": where})
        for (_, text), (n, _) in self.errors.items():
            causes[review.environment_cause(text) or WORK] += n
        return {"errors": errors, "causes": [{"cause": name, "n": n} for name, n in ranked(causes)]}

    def _commands(self) -> dict:
        ordered = sorted(self.commands.items(), key=lambda kv: (-kv[1][0], kv[0]))
        listed = ordered[:MAX_COMMANDS]
        return {
            "commands": [command for command, _ in listed],
            "command_at": [where for _, (_, where) in listed],
            "repeated_commands": [
                {"command": command, "n": n, "at": where} for command, (n, where) in ordered if n > 1
            ][:MAX_REPEATED],
        }

    def _hooks(self) -> list[dict]:
        ordered = sorted(self.hooks.items(), key=lambda kv: (-kv[1].seen, kv[0]))[:MAX_HOOKS]
        return [
            {
                "hook": name,
                "command": command,
                "runs": found.seen,
                "timed": found.timing.runs,
                "ms": found.timing.ms,
                "max_ms": found.timing.max_ms,
                "buckets": found.timing.buckets,
                "failed": found.failed,
                "timed_out": found.timed_out,
            }
            for (name, command), found in ordered
        ]


# A facet from a digest, and the numbers of a facet


def _list(value) -> list:
    return value if isinstance(value, list) else []


def digest_facet(body: dict, runtime: str | None = None) -> dict:
    """The facet of a session the hub holds only the digest of: the digest's own lists, each error with its cause,
    and null for what a digest does not say (the files read, the knowledge graph, the hooks, the skills, the cost)."""
    body = body if isinstance(body, dict) else {}
    chars = {
        item.get(TOOL_NAME): count(item.get("chars")) or 0
        for item in _list(body.get("tool_result_chars"))
        if isinstance(item, dict)
    }
    tools = [
        {**item, "output_chars": chars.get(item.get(TOOL_NAME), 0)}
        for item in _list(body.get("tools"))
        if isinstance(item, dict)
    ]
    errors, causes = [], Counter()
    for item in _list(body.get("errors")):
        if isinstance(item, dict):
            cause = review.environment_cause(str(item.get("text") or "")) or WORK
            errors.append({**item, "cause": cause})
            causes[cause] += count(item.get("n")) or 1
    turns = [turn for turn in _list(body.get("user_turns")) if isinstance(turn, str)]
    return {
        "version": FACET_VERSION,
        "runtime": runtime,
        **{key: body.get(key) for key in ("messages", "started_at", "ended_at", "model")},
        "models": _list(body.get("models")),
        "files": [],
        "tools": tools,
        "bash": _list(body.get("bash")),
        "errors": errors,
        "causes": [{"cause": name, "n": n} for name, n in ranked(causes)],
        "commands": _list(body.get("commands")),
        "command_at": [],
        "repeated_commands": _list(body.get("repeated_commands")),
        "user_turns": turns,
        "corrections": [index for index, turn in enumerate(turns) if review.is_correction(turn)],
        "files_read": None,
        "files_edited": _list(body.get("files_edited")),
        "kg": None,
        "context": {"first": None, "peak": count(body.get("peak_context_tokens"))},
        "compactions": count(body.get("compactions")) or 0,
        "compaction": None,
        "usage": {key: count((body.get("usage") or {}).get(key)) or 0 for key in USAGE_KEYS}
        if isinstance(body.get("usage"), dict)
        else dict.fromkeys(USAGE_KEYS, 0),
        "cost_usd": None,
        "hooks": None,
        "skills": None,
        "api_errors": None,
        "sources": None,
        "loaded": None,
    }


def _sum(items, key: str) -> int:
    return sum(count(item.get(key)) or 0 for item in _list(items) if isinstance(item, dict))


def summary(body: dict) -> dict:
    """The numbers of a facet that add up over sessions and runs: what the weekly series of the figures count."""
    body = body if isinstance(body, dict) else {}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    kg = body.get("kg") if isinstance(body.get("kg"), dict) else {}
    environment = sum(
        count(item.get("n")) or 0
        for item in _list(body.get("causes"))
        if isinstance(item, dict) and item.get("cause") != WORK
    )
    return {
        "messages": count(body.get("messages")) or 0,
        "tool_calls": _sum(body.get("tools"), "calls"),
        "tool_errors": _sum(body.get("tools"), "errors"),
        "environment_errors": environment,
        "corrections": len(_list(body.get("corrections"))),
        "commands": len(_list(body.get("commands"))),
        "files_edited": len(_list(body.get("files_edited"))),
        "kg_calls": count(kg.get("calls")) or 0,
        "kg_empty": count(kg.get("empty")) or 0,
        "compactions": count(body.get("compactions")) or 0,
        "context_first": count(context.get("first")),
        "context_peak": count(context.get("peak")),
        "input_tokens": count(usage.get("input_tokens")) or 0,
        "output_tokens": count(usage.get("output_tokens")) or 0,
        "cache_read_tokens": count(usage.get("cache_read_input_tokens")) or 0,
        "cache_write_tokens": count(usage.get("cache_creation_input_tokens")) or 0,
        "cost_usd": body.get("cost_usd") if isinstance(body.get("cost_usd"), (int, float)) else None,
        "hook_ms": _sum(body.get("hooks"), "ms"),
    }
