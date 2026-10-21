"""The plugin/1 protocol (``evo_agents.plugins.protocol``): the framing the host writes and reads, the schema of each
method's params and result, and the errors a backend's line may carry. Standard library only, no process."""

from __future__ import annotations

import json

import pytest

from evo_agents.plugins import protocol


def test_plugin_protocol_request_is_a_json_line():
    line = protocol.request(7, "ping", {})
    assert line.endswith(b"\n")
    message = json.loads(line)
    assert message == {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}


def test_plugin_protocol_parse_line_reads_a_result_and_an_error():
    ok = protocol.parse_line(b'{"jsonrpc":"2.0","id":1,"result":{"delivered":true}}')
    assert protocol.outcome(ok) == {"delivered": True}
    err = protocol.parse_line(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"no"}}')
    with pytest.raises(protocol.RemoteError) as raised:
        protocol.outcome(err)
    assert raised.value.code == -32000


@pytest.mark.parametrize(
    "line, reason",
    [
        (b"{not json", "not JSON"),
        (b"[]", "not an object"),
        (b'{"id":1,"result":{}}', "jsonrpc"),
        (b'{"jsonrpc":"2.0","id":1,"method":"route","params":{}}', "from the backend"),
        (b'{"jsonrpc":"2.0","id":1}', "either a result or an error"),
        (b'{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":1,"message":"x"}}', "either a result or an error"),
        (b'{"jsonrpc":"2.0","id":1,"error":{"message":"x"}}', "integer code"),
    ],
)
def test_plugin_protocol_rejects_a_line_that_is_not_a_response(line, reason):
    with pytest.raises(protocol.ProtocolError) as raised:
        protocol.parse_line(line)
    assert reason in str(raised.value)


def test_plugin_protocol_a_null_id_response_is_allowed():
    message = protocol.parse_line(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"bad line"}}')
    assert message["id"] is None


def test_plugin_protocol_a_line_over_the_limit_is_refused():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_line(b"x" * (protocol.MAX_LINE + 1))


def test_plugin_protocol_params_schema_accepts_the_smallest_calls():
    assert protocol.params_problems("initialize", _init()) == []
    assert protocol.params_problems("ping", {}) == []
    assert protocol.params_problems("tool", {"tool": "echo", "arguments": {}, "caller": {"project": "p"}}) == []
    assert protocol.params_problems("event", {"event": "e", "project": "p", "payload": {}}) == []


def test_plugin_protocol_events_name_the_kinds_of_canvas_and_signals_beside_the_old_ones():
    """plan living-lean-canvas adds three kinds, only adding: the four before keep their names."""
    assert protocol.EVENT_KINDS[:7] == (
        "run.state",
        "chat.message",
        "plan.step",
        "task.result",
        "canvas.claim",
        "canvas.baseline",
        "signal.posted",
    )
    for kind in protocol.EVENT_KINDS:
        params = {"event": "e", "project": "p", "payload": {"v": 1}, "kind": kind, "delivery": 3}
        assert protocol.params_problems("event", params) == []
    unknown = {"event": "e", "project": "p", "payload": {}, "kind": "canvas.deleted"}
    assert protocol.params_problems("event", unknown)


def test_plugin_protocol_events_name_an_approved_srs_version_and_a_tasks_status_last():
    """plan loop-engine adds two kinds after the seven before, only adding: those keep their names and places."""
    assert protocol.EVENT_KINDS[7:] == ("srs.version.approved", "tasks.status")
    for kind in protocol.EVENT_KINDS[7:]:
        params = {"event": "e", "project": "p", "payload": {"v": 1}, "kind": kind, "delivery": 4}
        assert protocol.params_problems("event", params) == []
    unknown = {"event": "e", "project": "p", "payload": {}, "kind": "srs.version.dropped"}
    assert protocol.params_problems("event", unknown)


def test_plugin_protocol_params_schema_names_a_missing_field():
    problems = protocol.params_problems("initialize", {"protocol": protocol.PROTOCOL})
    assert problems and any("plugin" in p for p in problems)


def test_plugin_protocol_an_unknown_method_has_no_schema():
    assert protocol.params_problems("no-such", {}) == ["plugin/1 has no method 'no-such'"]


def test_plugin_protocol_result_schema_checks_a_route_status():
    assert protocol.result_problems("route", {"status": 200}) == []
    assert protocol.result_problems("route", {"status": 7})  # out of range
    assert protocol.result_problems("initialize", {"protocol": "plugin/1", "plugin": {"id": "x", "version": "1"}}) == []


def _init() -> dict:
    return {
        "protocol": protocol.PROTOCOL,
        "plugin": {"id": "sample", "version": "1.0.0"},
        "hub": {"url": "http://hub/", "token": "evp_x", "version": "0.0.0"},
        "config": {"instance": {}, "projects": {}},
        "grants": {},
    }
