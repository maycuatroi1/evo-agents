"""What a knowledge graph pushed to the hub is made of: the knowledge config it is built with, and the run logs.

The config is the part of a project's knowledge.yaml the build reads: the policy, the identifiers, the ontology
extension, and for each source its id, connector, label and code backend. A source's config, command, credential,
refresh and deletion settings never leave the machine: the hub runs no connector. ``knowledge_config`` cuts it out of
a loaded project; ``config_problems`` is the hub's check that the config fits the project it registered (the same
ladder, sources named once, identifiers that compile, an ontology that only extends the hub schema).

A run log is ``log/<run_id>.jsonl.gz`` exactly as ``evo_agents.kg.corpus.LogWriter`` writes it: a ``run`` header
naming the run, its source and project, then every message of the run with item text replaced by blob references.
``read_run_log`` reads one completely (a log cut short or not gzip is refused, not read up to the damage), collects
the blobs it refers to, and labels every item the way the build will: the source's label from the config, raised by
the item's own. Items whose label the hub sink does not clear make the whole run refused.

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

from evo_agents.kg.policy import DEFAULT_LEVELS, DEFAULT_LOCATIONS, Label, Policy
from evo_agents.kg.protocol import canonical_json, sha256, source_of

SOURCE_FIELDS = ("id", "connector", "label", "code")  # what the build reads of a source
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


def config_digest(config: dict) -> str:
    return sha256(canonical_json({"knowledge": config["knowledge"], "ontology": config.get("ontology")}))


def config_problems(project: str, levels: list[str], locations: list[str], config: dict) -> list[str]:
    """Why the hub cannot build ``project`` (registered with ``levels`` and ``locations``) from ``config``."""
    from evo_agents.kg.schema import validate_ontology

    knowledge, ontology = config.get("knowledge"), config.get("ontology")
    if not isinstance(knowledge, dict):
        return ["knowledge must be an object"]
    problems = []
    if len(canonical_json(config)) > MAX_CONFIG_BYTES:
        problems.append(f"the config is larger than {MAX_CONFIG_BYTES} bytes")
    if knowledge.get("project") != project:
        problems.append(f"knowledge.yaml names project {knowledge.get('project')!r}, not {project}")
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
        extra = sorted(set(source) - set(SOURCE_FIELDS))
        if extra:
            problems.append(f"source {source.get('id')}: {', '.join(extra)} must stay on the machine")
        if not isinstance(source.get("connector"), str):
            problems.append(f"source {source.get('id')}: connector must be a string")
        for key in ("label", "code"):
            if key in source and not isinstance(source[key], dict):
                problems.append(f"source {source.get('id')}: {key} must be an object")
    identifiers = knowledge.get("identifiers") or []
    if not isinstance(identifiers, list):
        problems.append("identifiers must be a list")
        identifiers = []
    for entry in identifiers:
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        if not isinstance(pattern, str) or not isinstance(entry.get("kind"), str):
            problems.append("every identifier needs a kind and a pattern")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"identifier {entry['kind']}: pattern does not compile ({exc})")
    if (ontology is None) != (knowledge.get("ontology") is None):
        problems.append("knowledge.ontology and the ontology must be given together")
    elif ontology is not None:
        if knowledge.get("ontology") != ONTOLOGY_FILE:
            problems.append(f"knowledge.ontology must be {ONTOLOGY_FILE!r}")
        issues = validate_ontology(ontology)
        problems += [
            f"ontology: {': '.join(filter(None, (i.path, i.message)))}" for i in issues if i.severity == "error"
        ]
    return problems


# Run logs


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
