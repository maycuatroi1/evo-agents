"""A plugin backend that crashes or hangs on command, to test the host's timeout and backoff. Standard library only.

The ``crash`` tool ends the process at once, ``hang`` never answers, ``ok`` answers, ``orphan`` starts a process
that leaves the backend's process group and session (setsid) and answers its pid, ``clone_parent`` makes a child that
exits at once with clone(CLONE_PARENT), whose parent is then the backend's own (the host), and answers its pid (Linux,
x86_64 and aarch64), and ``limits`` answers the resource limits the backend runs with, ``token`` the hub token
``initialize`` gave it. ``initialize``, ``ping`` and
``shutdown`` behave. Each live start writes its pid to the file $EVO_FAULTY_PIDFILE when set, so a test can see the
host started a fresh process after a crash.
"""

import json
import os
import subprocess
import sys
import time

TOOLS = ("crash", "hang", "ok", "orphan", "clone_parent", "limits", "token")
state = {"token": None}


def _record_pid():
    path = os.environ.get("EVO_FAULTY_PIDFILE")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")


def _clone_parent():
    import ctypes
    import platform
    import signal

    number = {"x86_64": 56, "aarch64": 220}[platform.machine()]  # clone(2)
    syscall = ctypes.CDLL(None, use_errno=True).syscall
    syscall.restype = ctypes.c_long
    flags = 0x00008000 | signal.SIGCHLD  # CLONE_PARENT; no new stack, so the child goes on from here, as after fork()
    child = syscall(*(ctypes.c_long(arg) for arg in (number, flags, 0, 0, 0, 0)))
    if child == 0:
        os._exit(0)
    if child < 0:
        raise OSError(ctypes.get_errno(), "clone")
    return child


def tool(params):
    name = params["tool"]
    if name == "crash":
        os._exit(7)
    if name == "hang":
        while True:
            time.sleep(3600)
    if name == "orphan":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"content": [{"type": "text", "text": str(child.pid)}]}
    if name == "clone_parent":
        return {"content": [{"type": "text", "text": str(_clone_parent())}]}
    if name == "limits":
        import resource

        names = ("CORE", "NOFILE", "AS", "NPROC")
        limits = {n.lower(): resource.getrlimit(getattr(resource, "RLIMIT_" + n))[0] for n in names}
        return {"content": [{"type": "text", "text": json.dumps(limits)}]}
    if name == "token":
        return {"content": [{"type": "text", "text": str(state["token"])}]}
    return {"content": [{"type": "text", "text": "ok"}]}


def initialize(params):
    state["token"] = params["hub"]["token"]
    return {
        "protocol": "plugin/1",
        "plugin": params["plugin"],
        "tools": [{"name": n, "input_schema": {"type": "object"}} for n in TOOLS],
    }


HANDLERS = {
    "initialize": initialize,
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
