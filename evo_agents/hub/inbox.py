"""What waits on a person across the hubs this machine is signed in to: the data behind ``evo-agents hub inbox``.

A person who runs plans on more than one hub, or across many projects of one, used to find what waited on them with
``hub notifications``, then ``hub decision show`` and ``hub run show`` once per item with the right ``--project``,
the browser's Inbox for what those did not show, and ``EVO_HUB_HOME`` for a second hub. ``gather`` reads it all in
one pass, per hub: the open decisions of every project the person reads, the runs that need a person (in review,
waiting or parked on a question, failed or lost lately and not run again, running with no event for a while), the
unread notices that ask for an act, and the Curator's proposals that wait. Notices that only report (a merge, a push,
files sent, a brief) are counted, not listed. ``locate`` finds one decision or run from an id or a link of the web
without its project, and ``detail`` reads it whole: the decision, its run, the run's last events and leases, and the
commands that act on it.

Each item carries ``next``, the command that acts on it, with ``EVO_HUB_HOME=<dir>`` in front when the item's hub is
not the login a bare ``evo-agents hub`` uses. Nothing here writes to a hub. Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from evo_agents.hub.client import (
    HOME_VARIABLE,
    Credentials,
    Hub,
    HubError,
    credentials_in,
    env_credentials,
    hub_dir,
    login_dirs,
)

STALE_MINUTES = 30  # a running run with no event for this long is listed as stale
SINCE_HOURS = 72  # failed and lost runs that finished this recently are listed, unless run again
EVENTS_SHOWN = 20  # the last events of a run ``detail`` reads
MAX_PAGE = 200  # the most rows one list request of the hub answers
TAIL_KINDS = ("agent_message_chunk", "tool_call", "user_message", "state", "system")  # the events ``detail`` shows
TAIL_WINDOW = 4  # ``detail`` reads this many times the events it shows, since it skips the other kinds
WORKERS = 8  # projects read at once
# The run states that need a person, and those whose silence is worth a look.
PERSON_STATES = ("review", "waiting", "parked", "failed", "lost")
LIVE_STATES = ("running", "leased", "verifying")
LATER_STATES = ("queued", "done", "cancelled")  # read only to see that a failed run's plan or step ran again
# Notices that ask for an act; every other notice only reports, and is counted.
ACTION_NOTICES = frozenset(
    {
        "run_failed",
        "author_waiting",
        "chat_mention",
        "curator_paused",
        "builder_proposal",
        "canvas_baseline",
        "merge_paused",
        "pm_escalation",
        "loop_paused",
        "goal_review",
        "plugin_disabled",
        "worker_doctor",
        "browser_needs_login",
        "opportunity_window",
        "market_install_request",
        "market_license_expiring",
        "editorial_gate",
    }
)
# Run kinds that are a person's own session or a review of the hub's, never waiting on anyone.
QUIET_KINDS = frozenset({"local", "history", "probe", "judge", "replay"})
# What an item is, in the order the person should see them.
ORDER = ("decision", "review", "credential", "failed", "lost", "waiting", "parked", "stale", "notice", "proposal")

DECISION_LINK = re.compile(r"[?&#](?:decision=|run-decision-)(\d+)")
RUN_LINK = re.compile(r"/p/([a-z0-9][a-z0-9-]*)/runs/(\d+)")
REF = re.compile(r"^(?:(d|decision|r|run)[:#]?|#)?(\d+)$", re.I)


@dataclass(frozen=True)
class Login:
    """One hub this machine is signed in to, and how a command reaches it."""

    url: str
    login: str
    token: str
    home: str | None  # the login directory to put in EVO_HUB_HOME, None for the one a bare command uses

    @property
    def prefix(self) -> str:
        return f"{HOME_VARIABLE}={self.home} " if self.home else ""

    def hub(self) -> Hub:
        return Hub(self.url, self.token)


@dataclass
class HubInbox:
    """What waits on the person on one hub."""

    url: str
    login: str
    items: list[dict] = field(default_factory=list)
    reports: int = 0  # unread notices that only report
    error: str | None = None

    def to_json(self) -> dict:
        return {"url": self.url, "login": self.login, "items": self.items, "reports": self.reports, "error": self.error}


def _home_text(directory: Path) -> str:
    home = Path.home()
    try:
        return "~/" + directory.expanduser().relative_to(home).as_posix()  # one separator on every OS
    except ValueError:
        return str(directory)


def logins(only: str | None = None) -> list[Login]:
    """The hubs to read: EVO_HUB_TOKEN with EVO_HUB_URL alone when both are set (inside a run), else every login
    directory of the machine. ``only`` keeps those whose URL holds it."""
    env = env_credentials()
    if env is not None:
        found = [Login(env.url, env.login, env.token, None)]
    else:
        default = hub_dir().expanduser().resolve()
        found = []
        for directory in login_dirs():
            try:
                saved: Credentials = credentials_in(directory)
            except HubError:
                continue
            home = None if directory.expanduser().resolve() == default else _home_text(directory)
            if all(login.url != saved.url for login in found):
                found.append(Login(saved.url, saved.login, saved.token, home))
    return [login for login in found if not only or only in login.url]


def age_minutes(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((now - moment).total_seconds() // 60))


def _project_path(project: str) -> str:
    return f"/v1/projects/{quote(project, safe='')}"


def _option_hint(decision: dict) -> str:
    recommended = decision.get("recommended") or next(
        (option["key"] for option in decision.get("options") or () if option.get("recommended")), None
    )
    return f" --option {recommended}" if recommended else " --option KEY"


def decision_item(decision: dict, login: Login, now: datetime) -> dict:
    """An open decision as an item; a credential request says how to give the value without words."""
    project = decision["project"]
    where = f" --project {project}"
    kind = "credential" if decision.get("credential_request") else "decision"
    if kind == "credential":
        variable = decision["credential_request"].get("env_var") or "the value"
        act = f"{login.prefix}evo-agents hub decision answer {decision['id']}{where} --secret  # {variable} from stdin"
    elif decision.get("category") == "wait":
        act = f"{login.prefix}evo-agents hub decision answer {decision['id']}{where} --option now  # only to wake it sooner"
    else:
        act = f"{login.prefix}evo-agents hub decision answer {decision['id']}{where}{_option_hint(decision)}"
    return {
        "kind": kind,
        "id": decision["id"],
        "project": project,
        "run_id": decision.get("run_id"),
        "plan_id": decision.get("plan_id"),
        "step_key": decision.get("step_key"),
        "category": decision.get("category"),
        "title": " ".join((decision.get("question") or "").split()),
        "asked_of": decision.get("asked_of"),
        "mine": decision.get("asked_of") in (login.login, None) or not login.login,
        "age_min": age_minutes(decision.get("asked_at"), now),
        "options": [option["key"] for option in decision.get("options") or ()],
        "next": act,
        "show": f"{login.prefix}evo-agents hub inbox --show d{decision['id']}",
        "link": f"{login.url}/inbox?decision={decision['id']}",
    }


def _run_kind(run: dict, now: datetime, stale: int) -> tuple[str, str] | None:
    """What a run needs of a person, and why; None when it needs nothing."""
    state = run["state"]
    if state == "review":
        return "review", "waits for your approval"
    if state in ("failed", "lost"):
        error = " ".join(str(run.get("error") or "").split())[:160]
        return state, ": ".join(part for part in (run.get("failure_cause"), error) if part) or state
    if state in ("waiting", "parked"):
        since = run.get("waiting_since") or run.get("parked_at")
        return state, f"{state} since {age_minutes(since, now) or 0} min, on no open decision"
    idle = age_minutes(run.get("last_activity_at") or run.get("started_at") or run.get("queued_at"), now)
    if state in LIVE_STATES and idle is not None and idle >= stale:
        return "stale", f"{state}, no event for {idle} min"
    return None


def _run_next(kind: str, run: dict, login: Login) -> str:
    project = run["project"]
    base = f"{login.prefix}evo-agents hub"
    if kind == "review":
        return f"{base} run approve {run['id']} --project {project}"
    if kind in ("failed", "lost"):
        return f"{base} run candidates {run['id']} --project {project}"
    if kind == "waiting" and run.get("kind") == "author":
        return f"{base} run chat {run['id']} --project {project}"
    return f"{login.prefix}evo-agents hub inbox --show r{run['id']}"


def ran_again(run: dict, runs: list[dict]) -> bool:
    """Whether a later run resumed or reran ``run``, or did its plan (its step, for a step run) again."""
    for other in runs:
        if other["id"] <= run["id"] or other.get("kind") in QUIET_KINDS:
            continue
        if run["id"] in (other.get("resume_of_run_id"), other.get("parent_run_id")):
            return True
        same_plan = run.get("plan_id") and other.get("plan_id") == run.get("plan_id")
        if same_plan and (not run.get("step_key") or other.get("step_key") in (run.get("step_key"), None)):
            return True
    return False


def run_items(runs: list[dict], open_runs: set[int], login: Login, now: datetime, stale: int, since: int) -> list[dict]:
    """The runs of one project that need a person. A run waiting on an open decision is that decision's item; a
    failed or lost run that a later run resumed or reran, or that ended before ``since`` hours ago, is left out."""
    items = []
    for run in runs:
        if run.get("kind") in QUIET_KINDS or run["id"] in open_runs:
            continue
        found = _run_kind(run, now, stale)
        if found is None:
            continue
        kind, why = found
        ended = age_minutes(run.get("finished_at"), now)
        if kind in ("failed", "lost") and (ran_again(run, runs) or (ended is not None and ended > since * 60)):
            continue
        items.append(
            {
                "kind": kind,
                "id": run["id"],
                "project": run["project"],
                "run_id": run["id"],
                "plan_id": run.get("plan_id") or None,
                "step_key": run.get("step_key"),
                "category": run.get("kind"),
                "title": why,
                "asked_of": run.get("dispatched_by"),
                "mine": run.get("dispatched_by") == login.login or not login.login,
                "age_min": age_minutes(run.get("last_activity_at") or run.get("queued_at"), now),
                "worker": run.get("worker"),
                "next": _run_next(kind, run, login),
                "show": f"{login.prefix}evo-agents hub inbox --show r{run['id']}",
                "link": f"{login.url}/p/{run['project']}/runs/{run['id']}",
            }
        )
    return items


def fold_same_cause(items: list[dict]) -> list[dict]:
    """Failed and lost runs of one project, kind and person that failed the same way become one item, the newest,
    whose ``also`` lists the others: a broken credential fails every chat turn alike."""
    kept: dict[tuple, dict] = {}
    out = []
    for item in sorted(items, key=lambda i: -i["id"]):
        if item["kind"] not in ("failed", "lost"):
            out.append(item)
            continue
        key = (item["kind"], item["project"], item["category"], item["asked_of"], item["title"][:60])
        if key in kept:
            kept[key].setdefault("also", []).append(item["id"])
            continue
        kept[key] = item
        out.append(item)
    return out


def notice_items(notifications: list[dict], login: Login, now: datetime) -> tuple[list[dict], int]:
    """The unread notices that ask for an act, and the Curator's proposals that wait, as items; and how many unread
    notices only report."""
    items, reports = [], 0
    for note in notifications:
        if note["kind"] == "decision":
            continue  # read from the projects, with their whole question
        if note["kind"] == "proposal":
            if note.get("proposal_state") not in (None, "open"):
                continue
            kind = "proposal"
        elif note.get("notice_kind") in ACTION_NOTICES:
            kind = "notice"
        else:
            reports += 1
            continue
        items.append(
            {
                "kind": kind,
                "id": note["id"],
                "project": note.get("project"),
                "run_id": note.get("run_id"),
                "plan_id": None,
                "step_key": None,
                "category": note.get("notice_kind") or note["kind"],
                "title": " ".join((note.get("title") or "").split()),
                "asked_of": login.login or None,
                "mine": True,
                "age_min": age_minutes(note.get("created_at"), now),
                "next": f"{login.prefix}evo-agents hub notifications --read {note['id']}",
                "show": None,
                "link": login.url + (note.get("link") or "/inbox"),
            }
        )
    return items, reports


def _read_project(hub: Hub, project: str) -> tuple[list[dict], list[dict]]:
    path = _project_path(project)
    decisions = hub.call("GET", f"{path}/decisions?{urlencode({'state': 'open', 'limit': MAX_PAGE})}")["decisions"]
    query = [*(("state", state) for state in (*PERSON_STATES, *LIVE_STATES, *LATER_STATES)), ("limit", MAX_PAGE)]
    runs = hub.call("GET", f"{path}/runs?{urlencode(query)}")["runs"]
    return decisions, runs


def _sort_key(item: dict) -> tuple:
    return (not item["mine"], ORDER.index(item["kind"]), -(item["age_min"] or 0))


def gather_one(
    login: Login,
    *,
    project: str | None = None,
    stale: int = STALE_MINUTES,
    since: int = SINCE_HOURS,
    waits: bool = False,
    now: datetime | None = None,
) -> HubInbox:
    """What waits on the person on one hub. A project they cannot read is skipped; a hub that cannot be reached or
    refuses the login is an inbox with ``error``. Decisions of category wait are the hub's to answer and only
    listed with ``waits``."""
    now = now or datetime.now(UTC)
    inbox = HubInbox(login.url, login.login)
    hub = login.hub()
    try:
        names = [p["name"] for p in hub.call("GET", "/v1/projects") if p.get("role")]
        notes = hub.call("GET", f"/v1/me/notifications?{urlencode({'unread': 'true', 'limit': MAX_PAGE})}")
    except HubError as exc:
        inbox.error = str(exc)
        return inbox
    names = [name for name in names if not project or name == project]

    def read(name: str):
        try:
            return _read_project(hub, name)
        except HubError:
            return [], []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        read_all = list(pool.map(read, names))
    for decisions, runs in read_all:
        open_runs = {decision["run_id"] for decision in decisions if decision.get("run_id")}
        shown = decisions if waits else [d for d in decisions if d.get("category") != "wait"]
        inbox.items += [decision_item(decision, login, now) for decision in shown]
        inbox.items += run_items(runs, open_runs, login, now, stale, since)
    inbox.items = fold_same_cause(inbox.items)
    notices, inbox.reports = notice_items(notes["notifications"], login, now)
    inbox.items += [item for item in notices if not project or item["project"] == project]
    inbox.items.sort(key=_sort_key)
    return inbox


def gather(found: Iterable[Login], **options) -> list[HubInbox]:
    return [gather_one(login, **options) for login in found]


# One item, whole


@dataclass(frozen=True)
class Ref:
    """A decision or a run, as the person or a link names it; project and host when the link gives them."""

    what: str  # "decision" or "run"
    id: int
    project: str | None = None
    host: str | None = None


def parse_ref(text: str) -> Ref:
    """``582``, ``d582`` or ``decision:582`` (a decision); ``r1092``, ``run:1092`` or ``#1092`` (a run); or a link of
    the web: ``.../inbox?decision=582``, ``.../p/lumen/runs/948``, ``...runs/80#run-decision-32``."""
    text = text.strip()
    if "://" in text:
        parts = urlsplit(text)
        decision = DECISION_LINK.search("?" + parts.query + "#" + parts.fragment)
        run = RUN_LINK.search(parts.path)
        project = run.group(1) if run else None
        if decision:
            return Ref("decision", int(decision.group(1)), project, parts.hostname)
        if run:
            return Ref("run", int(run.group(2)), project, parts.hostname)
        number = parse_qs(parts.query).get("run")
        if number and number[0].isdigit():
            return Ref("run", int(number[0]), None, parts.hostname)
        raise ValueError(f"{text} links to no decision and no run")
    match = REF.match(text)
    if not match:
        raise ValueError(f"{text!r} is not a decision or run: give 582, d582, r1092, #1092 or a link of the hub")
    word = (match.group(1) or ("r" if text.startswith("#") else "d")).lower()
    return Ref("run" if word.startswith("r") or text.startswith("#") else "decision", int(match.group(2)))


def _get(hub: Hub, path: str):
    """GET ``path``; None when the hub answers 403 or 404 (another project's, or one this person cannot read)."""
    try:
        return hub.call("GET", path)
    except HubError as exc:
        if exc.status in (403, 404):
            return None
        raise


def locate(ref: Ref, found: list[Login], first: str | None = None) -> tuple[Login, str, dict] | None:
    """The hub, project and decision or run ``ref`` names: on the link's hub when it gives one, in its project when
    it gives one, else ``first`` (the harness's project) and then every project the person reads."""
    hubs = [login for login in found if not ref.host or urlsplit(login.url).hostname == ref.host]
    collection = "decisions" if ref.what == "decision" else "runs"
    for login in hubs:
        hub = login.hub()
        if ref.project:
            names = [ref.project]
        else:
            names = [p["name"] for p in hub.call("GET", "/v1/projects") if p.get("role")]
            if first in names:
                names.remove(first)
                names.insert(0, first)
        for name in names:
            path = f"{_project_path(name)}/{collection}/{ref.id}"
            body = _get(hub, path)
            if body is not None:
                return login, name, body
    return None


def _tail(hub: Hub, project: str, run: dict, count: int) -> list[dict]:
    """The run's last ``count`` events of TAIL_KINDS: what the agent said and did, and the run's moves."""
    after = max(0, int(run.get("last_seq") or 0) - TAIL_WINDOW * count)
    query = [("after", after), ("limit", MAX_PAGE), *(("kind", kind) for kind in TAIL_KINDS)]
    path = f"{_project_path(project)}/runs/{run['id']}/events?{urlencode(query)}"
    return (_get(hub, path) or {}).get("events", [])[-count:]


def diagnose(run: dict, decision: dict | None, now: datetime, stale: int = STALE_MINUTES) -> str:
    """One line on where the run stands and what moves it."""
    state = run["state"]
    idle = age_minutes(run.get("last_activity_at") or run.get("started_at") or run.get("queued_at"), now)
    if decision and decision.get("state") == "open":
        if decision.get("category") == "wait":
            on = f" or event {decision['wake_on']}" if decision.get("wake_on") else ""
            return f"a timed wait of the agent until {decision.get('wake_at') or '-'}{on}; the hub answers it itself"
        return f"{state} on decision #{decision['id']} ({decision.get('category')}), asked of {decision.get('asked_of')}: answer it"
    if state in LIVE_STATES:
        lease = run.get("lease_expires_at")
        if idle is not None and idle >= stale:
            return f"{state} on {run.get('worker') or 'no worker'}, no event for {idle} min (lease until {lease or '-'}): read the last events; cancel and rerun if the worker is gone"
        return f"{state} on {run.get('worker') or 'no worker'}, last event {idle} min ago: it is working"
    if state == "queued":
        return f"queued {age_minutes(run.get('queued_at'), now)} min ago: `evo-agents hub run candidates {run['id']}` says which workers can take it"
    if state in ("failed", "lost"):
        return f"{state}: {run.get('failure_cause') or run.get('error') or 'no cause recorded'}; rerun on a fit worker"
    return f"{state}"


def detail(ref: Ref, found: list[Login], first: str | None = None, events: int = EVENTS_SHOWN) -> dict | None:
    """A decision or a run whole: the decision, its run, the run's leases and last events, a diagnosis and the
    commands that act on it. None when no hub of ``found`` has it for this person."""
    located = locate(ref, found, first)
    if located is None:
        return None
    login, project, body = located
    hub = login.hub()
    now = datetime.now(UTC)
    decision = body if ref.what == "decision" else None
    run_id = decision.get("run_id") if decision else body["id"]
    run = body if ref.what == "run" else None
    if run is None and run_id:
        run = _get(hub, f"{_project_path(project)}/runs/{run_id}")
    if decision is None and run is not None:
        query = urlencode({"run_id": run["id"], "state": "open"})
        listed = _get(hub, f"{_project_path(project)}/decisions?{query}")
        decision = ((listed or {}).get("decisions") or [None])[0]
    leases = _get(hub, f"{_project_path(project)}/runs/{run['id']}/credentials") if run else None
    nexts = []
    if decision and decision.get("state") == "open":
        nexts.append(decision_item(decision, login, now)["next"])
    if run and not nexts:
        found_kind = _run_kind(run, now, STALE_MINUTES)
        if found_kind and found_kind[0] in ("review", "failed", "lost") or (found_kind and run.get("kind") == "author"):
            nexts.append(_run_next(found_kind[0], run, login))
        nexts.append(f"{login.prefix}evo-agents hub run logs {run['id']} --project {project} --follow")
    tail = _tail(hub, project, run, events) if run else []
    if run and not run.get("last_activity_at") and tail:
        run = {**run, "last_activity_at": tail[-1].get("at")}
    return {
        "hub": login.url,
        "project": project,
        "decision": decision,
        "run": run,
        "leases": leases,
        "events": tail,
        "diagnosis": diagnose(run, decision, now) if run else None,
        "next": nexts,
    }
