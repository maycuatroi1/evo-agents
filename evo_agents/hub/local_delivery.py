"""The talk of one hook of a local session with the hub (``evo_agents.hub.local_sync``): registering the session as
a local run, its state, its events, the questions of an AskUserQuestion dialog and their answers, and what the member
answered on the hub for the agent. Standard library only.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evo_agents.hub import hooks, local_account, local_outputs
from evo_agents.hub.client import Hub, HubError, transient
from evo_agents.hub.local_store import cut, prune

BATCH = 100  # events in one request
BATCHES = 3  # requests of events one run sends at most
MAX_QUESTIONS = 4  # questions of one dialog put to the hub
MAX_ASKS = 5  # dialogs whose answers are still awaited, kept in the session's file
QUESTION_CHARS = 2000
LABEL_CHARS = 200
DESCRIPTION_CHARS = 1000
OPTIONS = (2, 6)


def machine_name() -> str:
    name = socket.gethostname().strip().removesuffix(".local") or "unknown"
    return cut(" ".join(name.split()), 200)


class Late(Exception):
    """The run is past its deadline: nothing more is sent, and what waits stays on disk."""


class Delivery:
    """One hook's talk with the hub for one session."""

    def __init__(
        self,
        hub: Hub,
        login: str,
        rec: dict,
        sid: str,
        where: Path,
        hook: str,
        state: hooks.HookState,
        payload: dict | None = None,
        *,
        deadline: float,
    ):
        self.hub, self.login, self.rec, self.sid, self.where, self.hook, self.state = (
            hub,
            login,
            rec,
            sid,
            where,
            hook,
            state,
        )
        self.payload = payload or {}
        self.notes: list[str] = []
        self.deadline = deadline

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        if time.monotonic() > self.deadline:
            raise Late
        return self.hub.call(method, path, body)

    def base(self) -> str:
        return f"/v1/projects/{quote(self.rec['project'], safe='')}/local-runs"

    def register(self, title: str | None) -> None:
        rec = self.rec
        if rec.get("account") is None:
            found = local_account.read_account(self.sid)
            rec["account"] = {"email": found.email, "profile": found.profile}
        body = {"machine": machine_name(), "cwd": rec["cwd"], "claude_email": rec["account"]["email"]}
        if rec["account"].get("profile"):
            body["claude_profile"] = rec["account"]["profile"]
        if rec.get("title") or title:
            body["title"] = rec["title"] = rec.get("title") or title
        view = self.call("PUT", f"{self.base()}/{quote(self.sid, safe='')}", body)
        rec["run_id"] = int(view["id"])
        rec["sent"] = view.get("state")
        if not rec["events"] and isinstance(view.get("last_seq"), int):
            rec["seq"] = view["last_seq"]  # the hub has events of an earlier life of this session
        prune(time.time())

    def moved(self) -> bool:
        """Whether a Desktop session's account reads otherwise than the one it registered with (read before Desktop's
        variables were, or by an earlier evo-ak); the next register sends the new one. Other sessions keep theirs."""
        if local_account.desktop_email(os.environ) is None:
            return False
        found = local_account.read_account(self.sid)
        account = {"email": found.email, "profile": found.profile}
        if account == self.rec.get("account"):
            return False
        self.rec["account"] = account
        return True

    def state_post(self, wanted: str) -> None:
        for attempt in range(2):
            try:
                self.call("POST", f"{self.base()}/{self.rec['run_id']}/state", {"state": wanted})
                self.rec["sent"] = wanted
                return
            except HubError as exc:
                if attempt or transient(exc) or exc.status not in (404, 409):
                    raise
                if wanted == "ended":
                    self.rec["sent"] = "ended"  # it ended already, or the hub never had it
                    return
                self.register(None)  # the run ended or is gone: the session registers again
                if wanted == self.rec["sent"]:
                    return

    def flush(self) -> None:
        rec = self.rec
        for _ in range(BATCHES):
            events = rec["events"]
            if not events:
                break
            batch = events[:BATCH]
            answer = self.call("POST", f"{self.base()}/{rec['run_id']}/events", {"events": batch})
            ack = answer.get("ack_seq") if isinstance(answer, dict) else None
            if not isinstance(ack, int):
                break
            if (
                ack < batch[0]["seq"] - 1
            ):  # the hub lacks events this machine dropped: number what is left after its last
                for number, event in enumerate(events, start=ack + 1):
                    event["seq"] = number
                rec["seq"] = ack + len(events)
                continue
            rec["events"] = [e for e in events if e["seq"] > ack]
            rec["flushed"] = time.time()

    def ask(self) -> None:
        """Put the questions of the AskUserQuestion dialog about to open to the hub, as decisions of the run, each
        carrying the files the session sent since the person's last message (``local_outputs.for_question``)."""
        rec = self.rec
        data = self.payload.get("tool_input")
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list):
            return
        attached = local_outputs.for_question(self.sid, self.hub.url, self.deadline)
        entries = []
        for q in questions[:MAX_QUESTIONS]:
            found = _question(q)
            if found is None:
                continue
            body, keys = found
            view = self._decision({**body, "output_ids": attached} if attached else body)
            if view is None:
                continue  # the hub will not take this question: the dialog still opens
            entries.append({"decision": int(view["id"]), "question": q["question"], "keys": keys})
            rec["sent"] = "waiting"
        if entries:
            asks = rec.setdefault("asks", {})
            asks[str(self.payload.get("tool_use_id") or "last")] = entries
            for stale in list(asks)[:-MAX_ASKS]:
                del asks[stale]

    def _decision(self, body: dict) -> dict | None:
        """The hub's view of the decision ``body`` asks; None when the hub will not take it. A question whose files
        the hub refuses (422: an output of another run, or deleted) is asked again without them."""
        for attempt in (body, {k: v for k, v in body.items() if k != "output_ids"}):
            try:
                return self.call("POST", f"{self.base()}/{self.rec['run_id']}/decisions", attempt)
            except HubError as exc:
                if transient(exc) or exc.status not in (404, 409, 422):
                    raise
                if exc.status != 422 or "output_ids" not in attempt:
                    return None
        return None

    def answered(self) -> None:
        """Dialogs were answered in the session: report each answer to the decision it was put as, the ones an earlier
        hook could not report too (they wait in the session's file)."""
        rec = self.rec
        waiting = rec.get("answers") or {}
        for key in list(waiting):
            self._report(key, waiting[key] if isinstance(waiting[key], dict) else {})
            del waiting[key]

    def _report(self, key: str, given: dict) -> None:
        rec = self.rec
        asks = rec.get("asks") or {}
        entries = asks.get(key) or (asks.get("last") if key != "last" else None) or []
        for entry in entries:
            said = given.get(entry["question"])
            body = {"text": "answered in the session"}
            if isinstance(said, str) and said.strip():
                from evo_agents.hub.redact import redact

                body = {"text": cut(redact(said.strip()), 1000)}
                if said in entry["keys"]:
                    body = {"option": entry["keys"][said]}
            try:
                self.call("POST", f"{self.base()}/{rec['run_id']}/decisions/{entry['decision']}/answered", body)
            except HubError as exc:
                if transient(exc) or exc.status not in (404, 409, 422):
                    raise  # kept in the file: the next hook tells the hub
        for name in [k for k, v in asks.items() if v is entries]:
            del asks[name]
        if entries:
            rec["sent"] = "running"

    def inbox(self) -> None:
        """What the member answered on the hub since the agent last heard: for the agent, at the person's message."""
        rec = self.rec
        ack = rec.get("inbox_ack")
        try:
            answer = self.call(
                "POST", f"{self.base()}/{rec['run_id']}/inbox", {"ack": ack} if isinstance(ack, int) else None
            )
        except HubError as exc:
            if transient(exc) or exc.status not in (404, 405):
                raise
            return  # a hub that has no inbox for local runs: nothing to hand over
        messages = answer.get("messages") if isinstance(answer, dict) else None
        for message in messages or []:
            if isinstance(message, dict) and isinstance(message.get("text"), str) and message["text"].strip():
                by = message.get("sent_by")
                self.notes.append(f"{by + ': ' if isinstance(by, str) and by else ''}{message['text'].strip()}")
                if isinstance(message.get("id"), int):
                    rec["inbox_ack"] = max(message["id"], rec.get("inbox_ack") or 0)

    def run(self, title: str | None, wanted: str, due: bool) -> None:
        rec = self.rec
        if not rec["resolved"]:
            from evo_agents.hub.memory import MemorySync

            rec["project"] = MemorySync(self.hub, self.login, lock_timeout=hooks.LOCK_WAIT).project_of(self.where)
            rec["resolved"] = True
        if rec["project"] is None:
            rec["events"] = []
            return
        if rec["run_id"] is None or (self.hook == "user-prompt" and self.moved()):
            self.register(title)
        if wanted == "ended" and due:
            self.flush()
        if wanted != rec["sent"]:
            self.state_post(wanted)
        if wanted != "ended" and due:
            self.flush()
        if rec.get("answers"):  # before a new dialog is put: the earlier ones' answers end their wait first
            self.answered()
        if self.hook == "ask":
            self.ask()
        elif self.hook == "user-prompt":
            self.inbox()


def _options(options: list) -> tuple[list[dict], dict[str, str]]:
    """The options of a question as the hub takes them, keyed 1, 2, .., and {label: key}."""
    from evo_agents.hub.redact import redact

    keys, sent = {}, []
    for n, option in enumerate(options[: OPTIONS[1]], start=1):
        label = " ".join(str(option.get("label") if isinstance(option, dict) else option).split())
        if not label:
            continue
        shown = {"key": str(n), "label": cut(redact(label), LABEL_CHARS)}
        if isinstance(option, dict) and isinstance(option.get("description"), str) and option["description"]:
            shown["description"] = cut(redact(option["description"]), DESCRIPTION_CHARS)
        keys[label] = shown["key"]
        sent.append(shown)
    return sent, keys


def _question(q) -> tuple[dict, dict[str, str]] | None:
    """(the decision a question of the dialog is put as, {label: key}); None for one the hub cannot take."""
    from evo_agents.hub.redact import redact

    options = q.get("options") if isinstance(q, dict) else None
    if not isinstance(q.get("question") if isinstance(q, dict) else None, str) or not isinstance(options, list):
        return None
    sent, keys = _options(options)
    if len(sent) < OPTIONS[0]:
        return None
    body = {"category": "question", "question": cut(redact(q["question"]).strip(), QUESTION_CHARS), "options": sent}
    header = q.get("header")
    multi = "Several options may be picked in the session. " if q.get("multiSelect") else ""
    if multi or isinstance(header, str) and header.strip():
        body["context"] = cut(multi + (redact(header) if isinstance(header, str) else ""), 1000).strip()
    return body, keys
