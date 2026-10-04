"""A scriptable in-process connector for tests.

Its source config names a JSON file; tests rewrite that file between syncs to simulate edits, additions,
deletions and failures at the source."""

import json
from pathlib import Path

from evo_agents.kg.protocol import finalize_item, hello


def make_item(
    source: str, key: str, text: str, rev: str, rev_time: str = "2026-10-01T00:00:00Z", kind: str = "doc"
) -> dict:
    from evo_agents.kg.text import split_markdown

    return {
        "id": f"{source}:{kind}:{key}",
        "kind": kind,
        "rev": rev,
        "rev_time": rev_time,
        "rev_exact": True,
        "title": key,
        "uri": f"https://example.test/{key}",
        "body": {"format": "markdown", "text": text},
        "fragments": split_markdown(text),
    }


def run(ctx):
    spec = json.loads(Path(ctx.source["config"]["file"]).read_text())
    sid = ctx.source["id"]
    if spec.get("bad_hello"):
        yield {"type": "hello", "protocol": "kg/9", "connector": "fake", "version": "1"}
    else:
        yield hello("fake", "1", list_complete=True, tombstones=True, rev_exact=True)
    emitted = 0
    for raw in spec.get("items", []):
        item = make_item(sid, raw["key"], raw["text"], raw["rev"], raw.get("rev_time", "2026-10-01T00:00:00Z"))
        if raw.get("label"):  # the item's own label, which raises the source's
            item["label"] = raw["label"]
        item = finalize_item(item)
        if raw.get("corrupt"):
            item["hash"] = "sha256:" + "0" * 64
        yield item
        emitted += 1
        if spec.get("die_after") is not None and emitted >= spec["die_after"]:
            raise RuntimeError("source went away")
    for key in spec.get("errors", []):
        yield {"type": "error", "failure": "transient", "id": f"{sid}:doc:{key}", "message": "timeout"}
    for key in spec.get("tombstones", []):
        yield {"type": "tombstone", "id": f"{sid}:doc:{key}"}
    if spec.get("cursor") is not None:
        yield {"type": "state", "cursor": spec["cursor"]}
    if spec.get("listing", True):
        yield {"type": "listing", "scope": f"{sid}:", "complete": True, "count": emitted}
    yield {"type": "closed", "status": "ok"}
