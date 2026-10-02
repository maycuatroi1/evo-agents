import json
import sys

from evo_agents.kg.protocol import StreamChecker, finalize_item, hello, item_hash
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorRun
from evo_agents.kg.text import slugify, split_markdown


def sample_item(**over):
    item = {
        "id": "src:doc:one",
        "kind": "doc",
        "rev": "3",
        "rev_time": "2026-10-01T03:10:00Z",
        "rev_exact": True,
        "body": {"format": "markdown", "text": "# One\n\nhello\n"},
        "fragments": split_markdown("# One\n\nhello\n"),
    }
    item.update(over)
    return finalize_item(item)


def feed_all(messages, source_id="src"):
    checker = StreamChecker(source_id)
    for m in messages:
        checker.feed(m)
    checker.finish()
    return checker


def test_valid_stream_has_no_issues():
    msgs = [
        hello("t", "1", list_complete=True),
        sample_item(),
        {"type": "listing", "scope": "src:", "complete": True, "count": 1},
        {"type": "closed", "status": "ok"},
    ]
    checker = feed_all(msgs)
    assert checker.issues == []
    assert checker.complete_run


def test_hash_is_stable_and_ignores_span():
    a = sample_item()
    b = dict(a)
    b["fragments"] = [{**f, "span": "lines 9-9"} for f in a["fragments"]]
    assert item_hash(a) == item_hash(b)
    assert item_hash(a) != item_hash(sample_item(title="changed"))


def test_checker_catches_protocol_violations():
    bad_hash = sample_item()
    bad_hash["hash"] = "sha256:" + "1" * 64
    msgs = [
        sample_item(),
        hello("t", "1"),
        bad_hash,
        sample_item(id="other:doc:x"),
        {"type": "mystery"},
        {"type": "listing", "scope": "src:", "complete": True, "count": 5},
    ]
    messages = [str(i) for i in feed_all(msgs).issues]
    text = "\n".join(messages)
    assert "first message must be hello" in text
    assert "wrong content hash" in text
    assert "does not belong to source" in text
    assert "unknown message type" in text
    assert "reports 5 item(s)" in text
    assert "without closed" in text


def test_unsupported_major_is_rejected():
    checker = feed_all(
        [{"type": "hello", "protocol": "kg/2", "connector": "t", "version": "1"}, {"type": "closed", "status": "ok"}]
    )
    assert any("unsupported protocol" in str(i) for i in checker.issues)


def test_split_markdown_is_disjoint_and_ignores_fences():
    text = "intro\n# A\none\n```\n# not a heading\n```\n## B\ntwo\n# A\nthree\n"
    frags = split_markdown(text)
    assert [f["anchor"] for f in frags] == ["_preamble", "a", "b", "a-1"]
    assert "".join(f["text"] for f in frags) == text
    assert frags[2]["parent"] == "a"
    assert slugify("3.2 Quy tắc phân quyền `api`") == "32-quy-tắc-phân-quyền-api"


def test_exec_connector_streams_and_can_be_killed(tmp_path):
    script = tmp_path / "conn.py"
    lines = [
        hello("x", "1"),
        sample_item(id="src:doc:a"),
        sample_item(id="src:doc:b"),
        {"type": "closed", "status": "ok"},
    ]
    data = tmp_path / "messages.jsonl"
    data.write_text("".join(json.dumps(m) + "\n" for m in lines))
    script.write_text(
        "import json, os, sys\n"
        "assert json.loads(os.environ['KG_SOURCE'])['id'] == 'src'\n"
        f"for line in open({str(data)!r}):\n"
        "    print(line.strip(), flush=True)\n"
    )
    source = {"id": "src", "connector": "exec", "command": [sys.executable, str(script)]}
    run = ConnectorRun(ConnectorContext("p", source))
    msgs = list(run)
    assert [m["type"] for m in msgs] == ["hello", "item", "item", "closed"]
    assert run.returncode == 0
    killed = ConnectorRun(ConnectorContext("p", source), kill_after=2)
    assert len(list(killed)) == 2 and killed.killed
