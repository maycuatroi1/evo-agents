"""The backend of the sample plugin: plugin/1 on stdin and stdout, standard library only (docs/plugins.md).

It keeps its config in memory, answers ``hello`` with the greeting of the caller's project, echoes the arguments of
``echo``, counts its jobs, events and notifications, and answers ``ping``. Logs go to stderr.
"""

import base64
import json
import sys

PROTOCOL = "plugin/1"
TOOLS = [
    {
        "name": "echo",
        "description": "Answer with the text given",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
]
state = {"config": {}, "counts": {"job": 0, "event": 0, "channel": 0}}


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def greeting(project):
    config = state["config"]
    chosen = config.get("projects", {}).get(project) or config.get("instance", {})
    return chosen.get("greeting", "hello")


def initialize(params):
    state["config"] = params["config"]
    state["plugin"] = params["plugin"]
    return {"protocol": PROTOCOL, "plugin": params["plugin"], "tools": TOOLS}


def route(params):
    if params["route"] != "hello":
        raise Refused(-32602, f"no route {params['route']}")
    project = params["caller"].get("project", "")
    body = json.dumps({"greeting": greeting(project), "project": project}).encode()
    return {"status": 200, "headers": {"content-type": "application/json"}, "body": base64.b64encode(body).decode()}


def tool(params):
    if params["tool"] != "echo":
        raise Refused(-32602, f"no tool {params['tool']}")
    return {"content": [{"type": "text", "text": str(params["arguments"].get("text", ""))}], "is_error": False}


def counted(kind):
    def handle(params):
        state["counts"][kind] += 1
        return {"delivered": True} if kind == "channel" else {}

    return handle


HANDLERS = {
    "initialize": initialize,
    "ping": lambda params: {},
    "shutdown": lambda params: {},
    "route": route,
    "tool": tool,
    "job": counted("job"),
    "event": counted("event"),
    "channel": counted("channel"),
}


def answer(message_id, result=None, error=None):
    reply = {"jsonrpc": "2.0", "id": message_id}
    reply.update({"error": error} if error is not None else {"result": result})
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            answer(None, error={"code": -32700, "message": "not JSON"})
            continue
        method, message_id = message.get("method"), message.get("id")
        handler = HANDLERS.get(method)
        if handler is None:
            answer(message_id, error={"code": -32601, "message": f"no method {method}"})
            continue
        try:
            answer(message_id, handler(message.get("params") or {}))
        except Refused as exc:
            answer(message_id, error={"code": exc.code, "message": str(exc)})
        if method == "shutdown":
            return
    print("sample: stdin closed", file=sys.stderr)


if __name__ == "__main__":
    serve()
