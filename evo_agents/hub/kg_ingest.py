"""What a knowledge graph pushed to the hub is made of: the knowledge config it is built with, and the run logs.

The config is the part of a project's knowledge.yaml the build reads: the policy, the identifiers, the ontology
extension, and for each source its id, connector, label and code backend, and for a git source the names of the repo
and ref it reads, which the hub measures the graph's lag with. A source's config, command, credential, path, refresh,
fetch and deletion settings never leave the machine: the hub runs no connector. ``knowledge_config`` cuts it out of
a loaded project; ``config_problems`` is the hub's check that the config fits the project it registered (the same
ladder, sources named once, identifiers that compile, an ontology that only extends the hub schema).

A run log is ``log/<run_id>.jsonl.gz`` exactly as ``evo_agents.kg.corpus.LogWriter`` writes it: a ``run`` header
naming the run, its source and project, then every message of the run with item text replaced by blob references.
``read_run_log`` reads one completely (a log cut short or not gzip is refused, not read up to the damage), collects
the blobs it refers to, and labels every item the way the build will: the source's label from the config, raised by
the item's own. Items whose label the hub sink does not clear make the whole run refused. It also keeps what the run
says of its source (``RunFacts``): how it ended, the lineage and epoch its header names, when it started and ended, and
the cursor of its last state message, which kg_ingests keeps for the source state a sync reads
(``evo_agents.hub.kg_source_state``).

Standard library and the kg package only: the client uses the config half, the server both.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.protocol import canonical_json, sha256, source_of
from evo_agents.schema.policy import DEFAULT_LEVELS, DEFAULT_LOCATIONS, Label, Policy

SOURCE_FIELDS = ("id", "connector", "label", "code", "repo", "ref")  # what the build and the health measure read
NAMED_FIELDS = ("repo", "ref")  # names, not paths; an older hub refuses them and a push resends without
ONTOLOGY_FILE = "ontology.yaml"  # where the hub writes the ontology, next to the knowledge.yaml it writes
MAX_CONFIG_BYTES = 1024 * 1024
MAX_LOG_BYTES = 2 * 1024**3  # uncompressed: a log of at most 256 MiB compressed must not unpack without bound
MAX_RUN_BLOBS = 500_000
REFUSED_SHOWN = 5
BLOB_REF = re.compile(r"sha256:([0-9a-f]{64})")
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOG_SUFFIX = ".jsonl.gz"


class LogProblem(ValueError):
    """A run log the hub does not take, for a reason that names no content."""


# The config


def knowledge_config(project) -> dict:
    """``{knowledge, ontology}`` of a loaded ``evo_agents.kg.project.Project``, as ``hub kg push`` sends it. Raises
    ProjectError when its ontology does not load."""
    knowledge = project.knowledge
    sources = [
        {key: source[key] for key in SOURCE_FIELDS if key in source}
        for source in project.sources()
        if isinstance(source, dict)
    ]
    cut = {
        "project": project.name,
        "policy": knowledge.get("policy") or {},
        "identifiers": knowledge.get("identifiers") or [],
        "sources": sources,
    }
    ontology = project.ontology
    if ontology is not None:
        cut["ontology"] = ONTOLOGY_FILE
    return json.loads(json.dumps({"knowledge": cut, "ontology": ontology}, default=str))  # plain JSON values only


def without_names(config: dict) -> dict:
    """``config`` with no source naming its repo or ref, as a hub that refuses them takes it."""
    knowledge = config["knowledge"]
    sources = [{k: v for k, v in s.items() if k not in NAMED_FIELDS} for s in knowledge.get("sources") or []]
    return {**config, "knowledge": {**knowledge, "sources": sources}}


def config_digest(config: dict) -> str:
    return sha256(canonical_json({"knowledge": config["knowledge"], "ontology": config.get("ontology")}))


def _source_problems(source: dict) -> list[str]:
    problems = []
    extra = sorted(set(source) - set(SOURCE_FIELDS))
    if extra:
        problems.append(f"source {source.get('id')}: {', '.join(extra)} must stay on the machine")
    if not isinstance(source.get("connector"), str):
        problems.append(f"source {source.get('id')}: connector must be a string")
    for key in ("label", "code"):
        if key in source and not isinstance(source[key], dict):
            problems.append(f"source {source.get('id')}: {key} must be an object")
    for key in NAMED_FIELDS:
        if key in source and not isinstance(source[key], str):
            problems.append(f"source {source.get('id')}: {key} must be a string")
    return problems


def config_problems(project: str, levels: list[str], locations: list[str], config: dict) -> list[str]:
    """Why the hub cannot build ``project`` (registered with ``levels`` and ``locations``) from ``config``."""
    knowledge, ontology = config.get("knowledge"), config.get("ontology")
    if not isinstance(knowledge, dict):
        return ["knowledge must be an object"]
    problems = []
    if len(canonical_json(config)) > MAX_CONFIG_BYTES:
        problems.append(f"the config is larger than {MAX_CONFIG_BYTES} bytes")
    if knowledge.get("project") != project:
        problems.append(f"knowledge.yaml names project {knowledge.get('project')!r}, not {project}")
    problems += _policy_problems(knowledge, levels, locations)
    problems += _sources_problems(knowledge)
    problems += _identifier_problems(knowledge)
    problems += _ontology_problems(knowledge, ontology)
    return problems


def _policy_problems(knowledge: dict, levels: list[str], locations: list[str]) -> list[str]:
    """Why the policy of ``knowledge`` does not match the levels and locations the project was registered with."""
    problems = []
    policy = knowledge.get("policy") or {}
    if not isinstance(policy, dict):
        problems.append("policy must be an object")
        policy = {}
    for key, registered, default in (("levels", levels, DEFAULT_LEVELS), ("locations", locations, DEFAULT_LOCATIONS)):
        found = policy.get(key) or default
        if list(found) != list(registered):
            problems.append(
                f"policy.{key} {list(found)} differ from the registered {list(registered)}: run "
                "`evo-agents hub project register` from the harness first"
            )
    return problems


def _sources_problems(knowledge: dict) -> list[str]:
    """Why the sources of ``knowledge`` cannot be built: the list, their ids, each source."""
    problems = []
    sources = knowledge.get("sources")
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        problems.append("sources must be a list of objects")
        sources = []
    ids = [s.get("id") for s in sources]
    if not all(isinstance(i, str) and i for i in ids):
        problems.append("every source needs an id")
    elif len(set(ids)) != len(ids):
        problems.append("a source id is declared twice")
    for source in sources:
        problems += _source_problems(source)
    return problems


def _identifier_problems(knowledge: dict) -> list[str]:
    """Why the identifiers of ``knowledge`` cannot be used: each needs a kind and a pattern that compiles."""
    identifiers = knowledge.get("identifiers") or []
    if not isinstance(identifiers, list):
        return ["identifiers must be a list"]
    problems = []
    for entry in identifiers:
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        if not isinstance(pattern, str) or not isinstance(entry.get("kind"), str):
            problems.append("every identifier needs a kind and a pattern")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"identifier {entry['kind']}: pattern does not compile ({exc})")
    return problems


def _ontology_problems(knowledge: dict, ontology) -> list[str]:
    """Why ``ontology`` and the file ``knowledge`` names for it do not go together, and its errors."""
    from evo_agents.schema.ontology import validate_ontology

    if (ontology is None) != (knowledge.get("ontology") is None):
        return ["knowledge.ontology and the ontology must be given together"]
    if ontology is None:
        return []
    problems = []
    if knowledge.get("ontology") != ONTOLOGY_FILE:
        problems.append(f"knowledge.ontology must be {ONTOLOGY_FILE!r}")
    issues = validate_ontology(ontology)
    problems += [f"ontology: {': '.join(filter(None, (i.path, i.message)))}" for i in issues if i.severity == "error"]
    return problems


# Run logs


EPOCH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_FACT_CHARS = 200  # a lineage, an epoch or a time longer than this is not one a client writes
MAX_CURSOR_BYTES = 64 * 1024
MAX_HELD_IDS = 100_000  # item ids a run_end may say the deletion guard held back, in all its scopes
MAX_LISTED = 10_000  # scopes a run_end may say it listed completely
MAX_SCOPE_CHARS = 1000


@dataclass
class RunFacts:
    """What a run log says of its source, as kg_ingests keeps it: ``status`` ok or failed (failed when no ``run_end``
    says ok), the ``lineage`` and ``epoch`` of its header, its ``started_at`` and the ``finished_at`` of its run_end,
    and ``cursor``, ``{"cursor": ...}`` of its last state message, None when it has none or one over
    MAX_CURSOR_BYTES. ``held``: the removals the deletion guard of the run held back, which its run_end lists
    (``evo_agents.kg.sync``), each ``{"scope", "reason", "ids"}``; None when it lists none or more than MAX_HELD_IDS
    ids, or one that is not of that shape. ``listed``: the scopes its run_end says it listed completely, whose removals
    it worked out (held or not); None when it names none, more than MAX_LISTED or one that is not a string, and when
    its ``held`` is one the hub does not keep, so that the run clears no hold."""

    status: str = "failed"
    lineage: str | None = None
    epoch: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    cursor: dict | None = None
    held: list[dict] | None = None
    listed: list[str] | None = None

    def feed(self, record: dict) -> None:
        kind = record.get("type")
        if kind == "run":
            self.lineage = _fact(record.get("lineage"))
            epoch = record.get("epoch")
            self.epoch = epoch if isinstance(epoch, str) and EPOCH.fullmatch(epoch) else None
            self.started_at = _fact(record.get("started_at"))
        elif kind == "run_end":
            self.status = "ok" if record.get("status") == "ok" else "failed"
            self.finished_at = _fact(record.get("finished_at"))
            self.held = _held(record.get("held"))
            # a hold the hub cannot keep (over MAX_HELD_IDS, or not of its shape) must not read as none: the run then
            # names no scope it listed, so the holds of earlier runs stay
            unread_hold = self.held is None and record.get("held") not in (None, [])
            self.listed = None if unread_hold else _listed(record.get("listed"))
        elif kind == "state":
            held = {"cursor": record.get("cursor")}
            self.cursor = held if len(canonical_json(held)) <= MAX_CURSOR_BYTES else None

    def columns(self) -> dict:
        """The columns of kg_ingests and kg_pending_runs these facts fill."""
        return {
            "status": self.status,
            "lineage": self.lineage,
            "epoch": self.epoch,
            "run_started_at": self.started_at,
            "run_finished_at": self.finished_at,
            "cursor": self.cursor,
            "held": self.held,
            "listed": self.listed,
        }


def _fact(value) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_FACT_CHARS else None


def _held(value) -> list[dict] | None:
    """The held removals of a run_end, each ``{"scope", "reason", "ids"}`` with strings only; None for anything else."""
    if not isinstance(value, list) or not value:
        return None
    held, count = [], 0
    for entry in value:
        if not isinstance(entry, dict):
            return None
        scope, reason, ids = entry.get("scope"), entry.get("reason"), entry.get("ids")
        if not _scope(scope) or not isinstance(reason, str) or not isinstance(ids, list):
            return None
        if not all(isinstance(item_id, str) for item_id in ids):
            return None
        count += len(ids)
        if count > MAX_HELD_IDS:
            return None
        held.append({"scope": scope, "reason": reason[:MAX_FACT_CHARS], "ids": ids})
    return held


def _scope(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_SCOPE_CHARS


def _listed(value) -> list[str] | None:
    """The scopes a run_end says it listed completely; None for anything but a list of up to MAX_LISTED scopes."""
    if not isinstance(value, list) or not value or len(value) > MAX_LISTED or not all(_scope(s) for s in value):
        return None
    return sorted(set(value))


def run_facts(path: Path) -> RunFacts:
    """The facts of the run log at ``path``, read whole. Raises LogProblem."""
    facts = RunFacts()
    for record in _lines(path):
        facts.feed(record)
    return facts


@dataclass
class RunLog:
    """One run log read whole: its header, the blobs its items refer to, and the items the check refused."""

    run_id: str
    source: str
    project: str | None
    records: int = 0
    items: int = 0
    blobs: set[str] = field(default_factory=set)  # hex SHA-256 of every blob an item or fragment refers to
    refused: list[tuple[str, Label]] = field(default_factory=list)  # (item id, label), the first few
    refused_count: int = 0
    source_refused: bool = False  # the source's own label is not cleared
    facts: RunFacts = field(default_factory=RunFacts)


def run_id_of(path: Path) -> str | None:
    """The run id a log file is named after, None for anything else (such as a log still being written)."""
    name = path.name
    if not name.endswith(LOG_SUFFIX):
        return None
    run_id = name[: -len(LOG_SUFFIX)]
    return run_id if RUN_ID.fullmatch(run_id) else None


def log_header(path: Path) -> dict | None:
    """The first record of a run log, None when it cannot be read."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
    except (OSError, EOFError, zlib.error, UnicodeDecodeError, ValueError):
        return None
    return first if isinstance(first, dict) else None


def blob_refs(record: dict) -> list[str]:
    body = record.get("body")
    refs = [body.get("blob")] if isinstance(body, dict) and "blob" in body else []
    for fragment in record.get("fragments") or []:
        if isinstance(fragment, dict) and "blob" in fragment:
            refs.append(fragment["blob"])
    found = []
    for ref in refs:
        match = BLOB_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None:
            raise LogProblem(f"item {record.get('id')} refers to a blob that is not sha256:<hex>")
        found.append(match.group(1))
    return found


def _lines(path: Path):
    """The JSON object of every line of a gzip file; LogProblem when it is not gzip, is cut short, or holds
    anything but JSON objects."""
    read = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                read += len(line)
                if read > MAX_LOG_BYTES:
                    raise LogProblem(f"the log unpacks to more than {MAX_LOG_BYTES} bytes")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    raise LogProblem(f"line {number} of the log is not JSON") from None
                if not isinstance(record, dict):
                    raise LogProblem(f"line {number} of the log is not a JSON object")
                yield record
    except (OSError, EOFError, zlib.error, UnicodeDecodeError) as exc:
        reason = "it is cut short" if isinstance(exc, EOFError) else "it is not a complete jsonl.gz file"
        raise LogProblem(f"the log cannot be read: {reason}") from None


def read_run_log(path: Path, project: str, run_id: str, policy: Policy, cleared: Callable[[Label], bool]) -> RunLog:
    """Read the log at ``path`` of run ``run_id`` of ``project`` completely. ``policy`` (from the project's config)
    labels each item as the build will; ``cleared`` says whether the hub sink clears a label. Raises LogProblem when
    the log is not one of this run; refused items are counted in the result, not raised."""
    found: RunLog | None = None
    for record in _lines(path):
        if found is None:
            if record.get("type") != "run" or not isinstance(record.get("source"), str) or not record["source"]:
                raise LogProblem("the log does not start with the header of a run")
            if record.get("run_id") != run_id:
                raise LogProblem(f"the log is the log of run {record.get('run_id')}, not of run {run_id}")
            if record.get("project") not in (None, project):
                raise LogProblem(f"the log is a run of project {record.get('project')}, not of {project}")
            found = RunLog(run_id, record["source"], record.get("project"))
            found.source_refused = not cleared(policy.source_label(found.source))
        found.records += 1
        found.facts.feed(record)
        if record.get("type") != "item":
            continue
        item_id = record.get("id")
        if not isinstance(item_id, str) or ":" not in item_id:
            raise LogProblem(f"item record {found.records} has no id")
        found.items += 1
        found.blobs.update(blob_refs(record))
        if len(found.blobs) > MAX_RUN_BLOBS:
            raise LogProblem(f"the run refers to more than {MAX_RUN_BLOBS} blobs")
        raised = record.get("label") if isinstance(record.get("label"), dict) else None
        label = policy.source_label(source_of(item_id), raised)
        if not cleared(label):
            found.refused_count += 1
            if len(found.refused) < REFUSED_SHOWN:
                found.refused.append((item_id, label))
    if found is None:
        raise LogProblem("the log is empty")
    return found


def refusal(log: RunLog, policy: Policy, sink: str) -> str | None:
    """Why the hub refuses the run, naming the items (their ids, never their content); None when it is cleared."""

    def named(label: Label) -> str:
        names = policy.describe(label)
        return names["level"] + ("" if names["location"] == policy.locations[0] else f"/{names['location']}")

    if log.source_refused:
        source = policy.source_label(log.source)
        return (
            f"source {log.source} is labelled {named(source)}, above what hub sink {sink!r} clears: nothing of this "
            "run was written"
        )
    if not log.refused_count:
        return None
    shown = ", ".join(f"{item} ({named(label)})" for item, label in log.refused)
    return (
        f"{log.refused_count} item(s) of run {log.run_id} carry labels hub sink {sink!r} does not clear, such as "
        f"{shown}: nothing of this run was written. Raise the sink's clearance in knowledge.yaml and register the "
        "project again if these levels may leave the machine"
    )
