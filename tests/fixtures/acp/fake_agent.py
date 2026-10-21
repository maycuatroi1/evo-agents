#!/usr/bin/env python3
"""A fake agent of the Agent Client Protocol (protocol version 1) for the worker's tests: JSON-RPC 2.0 on stdin and
stdout, one message a line, standard library only.

It answers ``initialize``, ``session/new``, ``session/load`` (when FAKE_ACP_LOAD is 1, replaying a user message and an
agent message of the session's history first), ``session/set_config_option`` (a config option of category model
whose values are FAKE_ACP_MODELS, separated by commas) and ``session/prompt``, whose turn plays the next entry of the
scenario in FAKE_ACP_SCENARIO (a JSON file: {"turns": [[action, ...], ...]}, the last one again once they run out).
Each action is one of:

- ``{"update": {...}}``: a session/update notification with that update;
- ``{"write": {"path": "text"}}``: write the files, relative to the session's cwd;
- ``{"permission": "edit"}``: ask session/request_permission for a tool call of that kind, then say in an agent message
  chunk which option it got ("permission: allow-once") or that it was cancelled;
- ``{"env": "NAME"}``: say in an agent message chunk whether NAME is set ("env NAME: set" or "env NAME: unset");
- ``{"stderr": "text"}``: write a line on stderr;
- ``{"wait_cancel": true}``: wait for session/cancel, then end the turn ``cancelled``;
- ``{"wait_file": "/path"}``: wait until that file exists;
- ``{"exit": 3}``: exit at once with that status, answering nothing;
- ``{"stop": "max_tokens"}``: end the turn with that stop reason (``end_turn`` when the turn names none).

Every message it reads and writes goes to FAKE_ACP_LOG as a JSON line {"dir": "in" | "out", "message": ...}, when set.
"""

import json
import os
import sys
import time

LOG = os.environ.get("FAKE_ACP_LOG")
MODELS = [name for name in os.environ.get("FAKE_ACP_MODELS", "").split(",") if name]
LOAD = os.environ.get("FAKE_ACP_LOAD") == "1"


def log(direction, message):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"dir": direction, "message": message}) + "\n")


def send(message):
    message = {"jsonrpc": "2.0", **message}
    log("out", message)
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    message = json.loads(line)
    log("in", message)
    return message


def update(session, body):
    send({"method": "session/update", "params": {"sessionId": session, "update": body}})


def say(session, text):
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


def options(current):
    if not MODELS:
        return []
    values = [{"value": name, "name": name} for name in MODELS]
    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": current,
            "options": values,
        }
    ]


def scenario():
    path = os.environ.get("FAKE_ACP_SCENARIO")
    if not path:
        return [[]]
    with open(path, encoding="utf-8") as handle:
        return json.load(handle).get("turns") or [[]]


class Agent:
    def __init__(self):
        self.turns = scenario()
        self.turn = 0
        self.cwd = os.getcwd()
        self.asked = 1000

    def wait_for(self, number):
        """Read until the answer to request ``number``; a cancel on the way is remembered."""
        cancelled = False
        while True:
            message = read()
            if message.get("id") == number and "method" not in message:
                return message, cancelled
            if message.get("method") == "session/cancel":
                cancelled = True

    def play(self, session, actions):
        for action in actions:
            if "update" in action:
                update(session, action["update"])
            elif "write" in action:
                for path, text in action["write"].items():
                    target = os.path.join(self.cwd, path)
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    with open(target, "w", encoding="utf-8") as handle:
                        handle.write(text)
            elif "permission" in action:
                self.asked += 1
                choices = [
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                    {"optionId": "allow-always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                ]
                call = {"toolCallId": f"call-{self.asked}", "title": "a tool", "kind": action["permission"]}
                params = {"sessionId": session, "toolCall": call, "options": choices}
                send({"id": self.asked, "method": "session/request_permission", "params": params})
                answer, cancelled = self.wait_for(self.asked)
                outcome = (answer.get("result") or {}).get("outcome") or {}
                say(session, f"permission: {outcome.get('optionId') or outcome.get('outcome')}")
                if cancelled:
                    return "cancelled"
            elif "env" in action:
                name = action["env"]
                say(session, f"env {name}: {'set' if os.environ.get(name) else 'unset'}")
            elif "stderr" in action:
                sys.stderr.write(action["stderr"] + "\n")
                sys.stderr.flush()
            elif action.get("wait_cancel"):
                while read().get("method") != "session/cancel":
                    pass
                return "cancelled"
            elif "wait_file" in action:
                while not os.path.exists(action["wait_file"]):
                    time.sleep(0.02)
            elif "exit" in action:
                sys.stderr.flush()
                os._exit(int(action["exit"]))
            elif "stop" in action:
                return action["stop"]
        return "end_turn"

    def handle(self, message):
        method, params, number = message.get("method"), message.get("params") or {}, message.get("id")
        if method == "initialize":
            capabilities = {"loadSession": LOAD, "promptCapabilities": {}}
            info = {"name": "fake-acp", "title": "Fake ACP", "version": "0.0.1"}
            result = {"protocolVersion": 1, "agentCapabilities": capabilities, "agentInfo": info, "authMethods": []}
            send({"id": number, "result": result})
        elif method == "session/new":
            self.cwd = params.get("cwd") or self.cwd
            send(
                {
                    "id": number,
                    "result": {"sessionId": "sess-1", "configOptions": options(MODELS[0] if MODELS else None)},
                }
            )
        elif method == "session/load":
            self.cwd = params.get("cwd") or self.cwd
            session = params.get("sessionId")
            update(session, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old prompt"}})
            say(session, "old answer")
            send({"id": number, "result": {}})
        elif method == "session/set_config_option":
            if params.get("configId") == "model" and params.get("value") in MODELS:
                send({"id": number, "result": {"configOptions": options(params["value"])}})
            else:
                send({"id": number, "error": {"code": -32602, "message": f"no model {params.get('value')}"}})
        elif method == "session/prompt":
            actions = self.turns[min(self.turn, len(self.turns) - 1)]
            self.turn += 1
            send({"id": number, "result": {"stopReason": self.play(params.get("sessionId"), actions)}})
        elif number is not None:
            send({"id": number, "error": {"code": -32601, "message": "method not found"}})


def main():
    agent = Agent()
    while True:
        agent.handle(read())


if __name__ == "__main__":
    main()
