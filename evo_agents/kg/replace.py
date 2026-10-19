"""Replacing what one source holds in a corpus with everything it has now, for a source read in the builder's own
process rather than by a connector run on a machine: the sources the hub's core plugins keep (``evo_agents.hub.
kg_sources``).

``replace_source`` takes every item the source has now, each a finished kg/1 item. An item whose record would change,
new ones included, is written; an item the corpus holds alive and the source no longer gives gets a tombstone; both
go into the log of one run (``LogWriter``) and are merged, so the corpus stays what its log rebuilds. When nothing
changed nothing is written, neither a log nor a run, and the state digest stays as it was, so a build of unchanged
data builds nothing (``evo_agents.kg.build.unchanged``) and gives the same content hash.

The merge keeps, per item, the record of the largest ``(epoch, rev_time, rev, run_id)`` (``Corpus.merge``). Every run
here takes the first epoch, each record's ``rev`` is its content hash, which carries no order, its ``rev_time`` never
goes below that of the record it replaces, and the run's id is a UUIDv7: the newest run's records win, whatever times
the source gives. Every item's text goes into the corpus's blobs, written or not, so a build finds the blobs of each
item it holds without fetching them.

``drop_sources`` removes, the same way, every item of the sources whose id starts with a prefix and that are not kept:
the sources of a plugin a project no longer enables.
"""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.kg.corpus import BASE_EPOCH, Corpus, LogWriter
from evo_agents.kg.ids import epoch_to_iso, normalize_time, utc_now, uuid7

FIRST_TIME = epoch_to_iso(0)  # the revision time of an item that gives none


@dataclass(frozen=True)
class Replaced:
    """What ``replace_source`` wrote: its run (None when nothing changed), the items written and those removed."""

    run_id: str | None
    written: int
    removed: int


def _record(corpus: Corpus, item: dict, known) -> dict:
    """The log record of ``item`` (its text moved into blobs) with its hash as its revision, at a time no earlier than
    that of ``known``, the record or tombstone it replaces (None for an item the corpus never held)."""
    record = corpus.to_log_record({**item, "rev": item["hash"], "rev_time": item.get("rev_time") or FIRST_TIME})
    if known is not None:
        held = known.record.get("rev_time") or FIRST_TIME
        if normalize_time(held) > normalize_time(record["rev_time"]):
            record["rev_time"] = held
    return record


def replace_source(corpus: Corpus, source: str, items: list[dict], *, connector: str, project: str) -> Replaced:
    """Make ``items`` everything source ``source`` holds in ``corpus`` (see the module); what was written."""
    held = {record.item_id: record for record in corpus.items(source, include_deleted=True)}
    records = []
    for item in sorted(items, key=lambda found: found["id"]):
        known = held.pop(item["id"], None)  # a removed item's tombstone counts for the time too
        record = _record(corpus, item, known)
        if known is None or known.deleted or known.record != record:
            records.append(record)
    written = len(records)
    for gone in sorted((r for r in held.values() if not r.deleted), key=lambda found: found.item_id):
        tombstone = {"type": "tombstone", "id": gone.item_id, "rev": gone.rev, "rev_time": gone.record.get("rev_time")}
        records.append({key: value for key, value in tombstone.items() if value is not None})
    if not records:
        return Replaced(None, 0, 0)
    run_id = uuid7()
    header = {"source": source, "connector": connector, "project": project, "epoch": BASE_EPOCH}
    writer = LogWriter(corpus, run_id, header)
    for record in records:
        writer.write(record)
    writer.close()
    with corpus.db:
        corpus.merge(writer.records, run_id)
        corpus.record_run(
            run_id,
            source=source,
            connector=connector,
            status="ok",
            started_at=writer.header["started_at"],
            finished_at=utc_now(),
            detail={"written": written, "removed": len(records) - written},
        )
    return Replaced(run_id, written, len(records) - written)


def drop_sources(corpus: Corpus, prefix: str, keep: set[str], *, connector: str, project: str) -> list[str]:
    """Remove every item of each source whose id starts with ``prefix`` and is not in ``keep``; those sources."""
    dropped = [source for source in corpus.sources() if source.startswith(prefix) and source not in keep]
    for source in dropped:
        replace_source(corpus, source, [], connector=connector, project=project)
    return dropped


__all__ = ["FIRST_TIME", "Replaced", "drop_sources", "replace_source"]
