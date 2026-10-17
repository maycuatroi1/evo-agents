"""A plugin backend that crashes or hangs on command, to test the host's timeout and backoff. Standard library only.

The ``crash`` tool ends the process at once, ``hang`` never answers, ``ok`` answers. ``initialize``, ``ping`` and
``shutdown`` behave. Each live start writes its pid to the file $EVO_FAULTY_PIDFILE when set, so a test can see the
host started a fresh process after a crash.
"""

import json
import os
import sys
import time


def _record_pid():
    path = os.environ.get("EVO_FAULTY_PIDFILE")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")


def tool(params):
    name = params["tool"]
    if name == "crash":
        os._exit(7)
    if name == "hang":
        while True:
            time.sleep(3600)
    return {"content": [{"type": "text", "text": "ok"}]}


HANDLERS = {
    "initialize": lambda params: {"protocol": "plugin/1", "plugin": params["plugin"],
                                  "tools": [{"name": n, "input_schema": {"type": "object"}}
                                            for n in ("crash", "hang", "ok")]},
    "ping": lambda params: {},
    "shutdown": lambda params: {},
    "tool": tool,
}


def answer(message_id, result=None, error=None):
    reply = {"jsonrpc": "2.0", "id": message_id}
    reply.update({"error": error} if error is not None else {"result": result})
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()


def serve():
    _record_pid()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            answer(None, error={"code": -32700, "message": "not JSON"})
            continue
        handler = HANDLERS.get(message.get("method"))
        if handler is None:
            answer(message.get("id"), error={"code": -32601, "message": "no such method"})
            continue
        answer(message.get("id"), handler(message.get("params") or {}))
        if message.get("method") == "shutdown":
            return


if __name__ == "__main__":
    serve()
