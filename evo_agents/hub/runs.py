"""Runs and workers as the hub models them: the states of a run and who may move a run between them, a worker's
status as its heartbeats show it, the kinds of a run's events, which steps of a plan are ready to run, and the
decisions and notices a plan run or an author run sends its owner. Pure functions over JSON values and datetimes,
standard library only, so the api and the worker daemon share them; the prompt a run hands its agent is built in
``evo_agents.hub.run_prompts``. ``docs/workers.md`` describes the protocol built on them, and ``docs/notifications.md``
how decisions and notices reach the owner.

A run is of one of RUN_KINDS. A ``step`` run is one attempt at one plan step on one worker. A ``plan`` run is one long
session on one worker that does every step of a plan that is not done yet, in the order ``depends_on`` allows, and
reports each step as it goes (``run_prompts.build_plan_prompt`` tells its agent how); dispatched with
``session_per_step``, its worker starts a new agent session for each step instead, in the same order
(``next_session_step``), whose prompt is the step's with the handoff of the plan
(``run_prompts.build_step_session_prompt``, ``evo_agents.hub.handoff``). A ``review`` run is the Curator's Reviewer
of one night of a project's charter: it works on no plan, reads the project and writes findings and proposals, and
changes no code (``evo_agents.hub.review``). A ``judge`` run is the Curator's Judge of one change its
Builder made: it reads the change at the head of its pull request, runs the plan's verify and the project's hidden
checks, and says whether the change passes, without reading the Builder's transcript and without changing anything
(``evo_agents.hub.judge``). An ``author`` run writes an execution plan from a member's request with the create-exec-plan
skill, on one of the member's own workers, and changes no code (``evo_agents.hub.author``). A ``chat`` run is one turn
of an agent someone mentioned in a group chat, which answers in the thread, may change code on a branch ``chat/<run>``
and ends done with its turn; a reply in the thread starts the next turn as a new run in the same session
(``evo_agents.hub.chat_runs``). A ``task`` run is the work a plugin hands an agent type of its own that a project
enabled (``evo_agents.hub.task_runs``): one headless turn on a branch ``task/<run>``, whose pull request the hub opens
as a draft, never merged, with its result told back to that plugin. A ``release`` run is a cut or a fix of a cycle
of a project's daily release, which its release schedule queues (``evo_agents.hub.releases``), dispatched as the
schedule's owner and pinned to no worker. A ``local`` run is a Claude Code session on a
member's own machine, which no worker runs and the hub only watches and asks decisions for (LOCAL_STATES;
``evo_agents.hub.server.local_runs``). A worker takes a kind of run only once its heartbeat says its daemon runs that
kind (``run_kinds``): a daemon older than the review run reads every run as a step or plan run. Likewise it takes a plan
run with a session per step only once its heartbeat lists SESSION_PER_STEP among its ``features``, since an older daemon
would run it in one session. A worker whose heartbeat lists CLONES_CHECKOUTS takes a run whatever checkouts it has,
cloning those it lacks before the run starts; an older one takes only the runs of the repos it has a checkout of.
A run starts ``queued``; a worker
claims it (``leased``), starts the agent (``running``, or ``interactive`` when a person drives the agent in a
terminal), re-runs the agent's verify commands (``verifying``), and ends ``done``, ``failed`` or ``cancelled``, or in
``review`` until the owner approves it. A run whose worker stops extending its lease is ``lost``, and the hub queues a
new run for the same step (attempt + 1), or ``failed`` when it was the last of MAX_ATTEMPTS. That next attempt goes to
the worker that lost the run only once the worker is steady (STEADY_SECONDS of heartbeats, none STEADY_GAP_SECONDS
late); any other worker that may take it takes it at once. A worker whose host stalled past the lease, its agent still
working, gets the run back when its heartbeat names it within REATTACH_SECONDS of the loss and the next attempt still
waits unclaimed: the hub's one move out of an end state (``server.run_reattach``).

A plan run's agent may ask its owner a decision of DECISION_CATEGORIES. When its turn ends with a decision still open,
the run is ``waiting``: the worker keeps it, with its slot and lease, and hands an answer that comes within
DECISION_WAIT_SECONDS to the agent in the same session, and the run is ``running`` again. A run that waited longer is
``parked``, and one whose open decisions are all waits for a time or an event of the hub (WAIT_DECISION_CATEGORY) that
wake later than that at once, since nobody answers those: the worker lets it go and frees the slot, and keeps the
agent's session and the worktrees, so no wait holds a worker. An answer, or a wait that wakes, then queues a new plan
run on the same worker that goes on in that session (the run it resumes is ``done``), and a run parked PARKED_DAYS is
``cancelled``. Neither waiting nor parked counts toward the run's timeout: ``run_seconds`` counts only CLOCK_STATES. A
chat run's agent asks its owner decisions of CHAT_DECISION_CATEGORY too, and the run waits for the answers and is parked
as a plan run is; a chat run that asked none ends done with its turn. An author run waits the same way after each turn
of its agent, for its owner's reply in the run's chat (``evo_agents.hub.author``), is parked and resumed the same way
(RESUME_KINDS), and ends ``done`` when its owner ends the chat (the control ``finish``). On a daemon that lists
AUTHOR_DECISIONS among its ``features``, an author run's agent asks decisions too (its prompt names ``scope`` and
``architecture``), before its run has a plan (the decision's ``plan_id`` is then null) or after; a turn that ends with
one of them open waits until each is answered, the owner replies in the chat or ends it, so one answer alone does not
wake the agent, and a parked author run resumes only once none of its decisions is open, or on a reply. A plan run wakes
at the first answer, as before.

TRANSITIONS names, for each move, who may do it: the ``worker`` that holds the run, the ``owner`` (the member who
dispatched it, who is the owner of the worker), or the ``reaper``, the hub itself acting on an expired lease, a revoked
worker, a run nobody can claim any more, or a decision nobody answered.

A step is ready when its status is ``pending`` and every id in its ``depends_on`` names a step whose status is
``done``, the rule execute-plan uses for its frontier. A step without a status counts as pending, as the knowledge
graph reads plans; ``blocked`` is not pending, so a blocked step is never ready, and neither is a step that depends
on one. ``blocking`` holds no step back, here or in execute-plan from its 0.4.0: it marks a step the plan cannot
finish without, not a gate on the steps after it. Whether a ready step already has an active run is a question for
the database, not for this module.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Literal, get_args

from evo_agents.hub.autonomy_card import DECISION_CATEGORIES as DECISION_CATEGORIES
from evo_agents.hub.autonomy_card import DECISION_CATEGORY_TEXT as DECISION_CATEGORY_TEXT
from evo_agents.hub.autonomy_card import PROMPTED_DECISION_CATEGORIES as PROMPTED_DECISION_CATEGORIES
from evo_agents.hub.plans import step_key

PROTOCOL_HEADER = "X-Evo-Worker-Protocol"  # on every /v1/worker/* request; any other value is answered with 426
PROTOCOL_VERSION = "1"

HEARTBEAT_SECONDS = 15  # how often the daemon sends a heartbeat
OFFLINE_AFTER_SECONDS = 300  # a worker with no heartbeat for longer than this is offline
LEASE_SECONDS = 300  # a claim and each heartbeat set a held run's lease to expire this long after now
CLAIM_WAIT_SECONDS = 25  # the longest a claim waits for a run before answering that there is none
MAX_SLOTS = 16  # runs a worker holds at once at most (workers.slots, as schema 0026 bounds it); 1 is the least
MAX_WORKER_PLUGINS = 16  # plugins whose runtimes and environments a worker takes (workers.plugins, schema 0039)
MAX_ATTEMPTS = 3  # runs of one dispatch, the first included; the last one's expired lease fails it
# A run lost this recently goes back to its worker when that worker's heartbeat names it and the next attempt still
# waits unclaimed (``server.run_reattach``); a later heartbeat is told to stop the agent, as before.
REATTACH_SECONDS = 600
# The next attempt of a run lost on a worker goes back to that worker only once it is steady: its heartbeats have
# come for STEADY_SECONDS, none more than STEADY_GAP_SECONDS after the one before (unlike a laptop that slept, or one
# that woke for a moment).
STEADY_SECONDS = 120
STEADY_GAP_SECONDS = 30
# Why a run failed, as its worker says with the report of the state failed (failure_cause, an optional field of the
# worker protocol version 1, which an older daemon leaves out); the hub keeps it with the run. These are the causes
# this release's worker sends; the hub takes any name of FAILURE_CAUSE, so a newer worker's causes reach it too, and
# ``evo_agents.hub.review.run_cause`` guesses one from the error of a run that has none.
FAILURE_CAUSES = (
    "origin",  # preflight: the project lists no origin for a repo the run needs
    "credentials",  # preflight, or a fetch or push git refused: no credential reads, or pushes, a repo of the run
    "missing_tool",  # preflight: a program a verify command, or a hidden check of a judge run, calls is not on PATH
    "push_conflict",  # the remote branch moved on, and the run's own commits could not go on top of it
    "verify_failed",  # a verify command the worker ran again exited other than 0
    "timeout",  # the run ran past its timeout
    "cost_cap",  # the agent stopped at the run's cost cap
    "turn_cap",  # at its turns cap
    "time_cap",  # at its time cap
    "checkout",  # the checkout, the fetch or a worktree of the run went wrong
    "runtime",  # the worker has no adapter for the runtime, or cannot start the agent as the run asks
    "worker_stopped",  # the daemon was stopped, or failed, while the run was held
    "branch_mismatch",  # the worktree is on another branch than the run's, compared by full ref
    "rate_limit",  # the agent's account hit its usage limit and the run could not move to another one
    "account_auth",  # the agent's account failed to authenticate (its token refused) and the run could not move
    "environment",  # preflight: the worker cannot run the agent in the environment its claim names (no container engine)
    # the remote declined the push of the run's branch by a rule of its own (a hook, a protected branch, a ruleset),
    # not for its credential nor because the branch moved on: the run's commits are still in its worktree
    "push_declined",
    "no_worker",  # the hub: no worker claimed a question run in time (question_runs.QUEUE_SECONDS)
    "no_answer",  # the agent of a question run wrote no answer
    "steps_left",  # a plan run's agent kept ending its turn with steps of the plan to do and no decision open
    "command_failed",  # the shell command of a command run exited other than 0
)
PREFLIGHT_CAUSES = ("origin", "credentials", "missing_tool", "environment")  # what a run checks before its agent starts
FAILURE_CAUSE = r"^[a-z][a-z0-9_]{0,31}$"  # a cause a worker may send, known to this hub or not

MAX_BATCH_EVENTS = 500  # events in one POST of a run's events
MAX_BATCH_BYTES = 1024 * 1024  # body of that POST
MAX_EVENT_BODY_BYTES = 64 * 1024  # one event's body as JSON; a longer one is cut and marked truncated
MAX_RUN_EVENTS = 20_000  # events the hub keeps for one run
MAX_RUN_REPOS = 50  # the repos of one run (runs.repos, as schema 0010 bounds it)
MAX_MESSAGE_BYTES = 8 * 1024  # one message from the owner to a running agent
MAX_PROMPT_BYTES = 32 * 1024  # the prompt each builder of run_prompts returns
# Turns in a row the worker sends a plan run's agent back to its plan (``RunSpec.keep_going``) when a turn ends with
# steps to do and no decision open, before the run fails with steps_left; a turn after which fewer are left resets it.
KEEP_GOING_TURNS = 3

# one step; every step of a plan not done yet; a night's review of a project; the Judge of a change of the Curator; a
# plan written from a member's request; one turn of an agent a person or an agent mentioned in a group chat; the task a
# plugin hands an agent type of its own; the replay of a past step (``hub.replay``); the night's reading of sessions and
# runs (``hub.history``); a cut or a fix of a release cycle (``hub.releases``); a read-only question a core plugin asks
# an agent type of its own (``hub.question_runs``); the login session of a browser identity, which a person drives in
# the identity's Chromium with no agent (``hub.browser_login``); the weekly probe of a change of the Curator, k sessions
# with its rule on and k with it off (``hub.probes``); the shell command of a cron job, which the worker runs with no
# agent (``hub.command_runs``). A session on a member's own machine, which no worker
# runs, is LOCAL_KIND, besides RUN_KINDS: only the kinds a worker runs, announces and claims are in it
RunKind = Literal[
    "step",
    "plan",
    "review",
    "judge",
    "author",
    "chat",
    "task",
    "replay",
    "history",
    "probe",
    "release",
    "question",
    "browser",
    "command",
]
RUN_KINDS: tuple[str, ...] = get_args(RunKind)
LOCAL_KIND = "local"
ShownKind = Literal[RunKind, "local"]
SHOWN_KINDS: tuple[str, ...] = get_args(ShownKind)  # every kind a run may be shown as, local runs included
PLAN_KINDS = ("step", "plan")  # the kinds of run that work on a plan; a review run has none (its plan_id is null)
# the kinds only the night shift queues, and the Builder
CURATOR_KINDS = ("review", "judge", "replay", "history", "probe")
PLAN_RUN_AGENT = (0, 4, 0)  # the first daemon release that runs a plan run; an older one reads it as a step run
SESSION_PER_STEP = "session_per_step"  # a daemon that runs a plan run with a new agent session for each step
# a daemon whose author run's agent asks decisions (`evo-agents worker ask`) and waits for all of a turn's answers
AUTHOR_DECISIONS = "author_decisions"
# a daemon whose adapters apply a run's agent profile (evo_agents.hub.profiles): tool, skill and model, instructions
AGENT_PROFILES = "agent_profiles"
# a daemon that takes the runtimes of the plugins its owner accepted, each an ACP agent run by its adapter of ACP
# (evo_agents.plugins.runtimes, evo_agents.worker.runtimes.acp); the hub hands a run of such a runtime to no other
PLUGIN_RUNTIMES = "plugin_runtimes"
# a daemon that runs a run's agent in the environment its claim names (evo_agents.worker.environments): a container of
# a plugin's environment, through Docker or Podman; the hub hands a run of such an environment to no other
ENVIRONMENTS = "environments"
# a daemon that builds the agent packs of a run's profile, of the plugins its owner accepted, into the run's directory
# and hands them to its runtime (evo_agents.worker.packs); the hub hands a run whose profile attaches one to no other
AGENT_PACKS = "agent_packs"
# a daemon whose chat run's agent asks decisions (`evo-agents worker ask`), waits for the answers and is parked
CHAT_DECISIONS = "chat_decisions"
# a daemon that keeps the graph of each project whose night shift it is on duty for fresh on the hub: it syncs the git
# sources whose ref is origin/<branch>, fetching it first, and pushes their runs with its own token, no model involved
# (evo_agents.worker.kg_sync); the heartbeat answer names those projects (kg_sync) only to a daemon that says it
KG_SYNC = "kg_sync"
# a daemon whose chat run starts with the project's harness alone and whose agent opens each other repo it needs
# (`evo-agents worker repo open`, POST /v1/worker/runs/{id}/chat/repos); the hub hands any other daemon's chat run
# every repo of the project the worker has a checkout of (evo_agents.hub.chat_runs)
LAZY_CHAT_REPOS = "lazy_chat_repos"
# a daemon that acts on its owner's Update now (update_now of the heartbeat answer): it claims no new run, lets the
# ones it holds end, updates itself to the tag nightly and claims again, and reports in self_update.update_now how a
# request it could not carry out ended; the hub takes such a request for no other (evo_agents.hub.server.update_now)
UPDATE_NOW = "update_now"
# a daemon whose judge run judges a merge request of the hub's merge queue on GitLab when its claim names no change
# (GET .../merge-judge, POST .../merge-verdict; evo_agents.hub.server.merge_judge): the hub pins such a run to no other
MERGE_JUDGE = "merge_judge"
# a daemon that clones a repo of a run it has no checkout of before the run starts (evo_agents.worker.runner.clones):
# the hub hands such a worker a run whatever its checkouts, and keeps an older one to the runs of repos it has
CLONES_CHECKOUTS = "clones_checkouts"
# a daemon whose claude-code runs apply the run's autonomy snapshot in their PreToolUse hook (plan autonomy-levels): the
# hub hands a run with a class below auto (evo_agents.hub.autonomy_levels.restricted) to no other, on claude-code only
AUTONOMY = "autonomy"
AUTONOMY_RUNTIME = "claude-code"
# a daemon whose review run works with no repo: the KG init of a project that has none, one fed by uploads alone
# (evo_agents.hub.server.kg_init); the hub queues it only on such a worker and hands it to no other
REPOLESS_REVIEW = "repoless_review"
AGENT_HOME = "agent_home"  # a daemon that gives a claude-code agent of a non-default profile its own config dir
# a daemon whose heartbeat names apart (``orphans``) the runs a previous daemon left, and that takes a run's leases again
# when the answer says ``renew``: the hub re-attaches a run it lost for no other (evo_agents.hub.server.run_reattach)
REATTACH = "reattach"
# a daemon that installed Chromium (`evo-agents worker browser install`) and starts it for a run dispatched with
# browser (evo_agents.worker.browser); its heartbeat's browser says how many at once, and only it gets such a run
BROWSER = "browser"
MAX_BROWSERS = 64  # the most browser runs a worker may say it holds at once
# a daemon that installs Chromium when its owner asks from the hub (browser_install of the heartbeat answer), as
# `evo-agents worker browser install` does, and reports in browser_install how a request it could not carry out ended;
# the hub takes such a request for no other (evo_agents.hub.server.browser_install)
BROWSER_INSTALL = "browser_install"
# a daemon that runs `evo-agents worker doctor` when it starts and every 24 hours and sends what it found (POST
# /v1/worker/doctor; evo_agents.worker.doctor_report): only a worker whose newest report is fresh and has no high
# finding may be narrowed to units of the org chart (evo_agents.hub.server.worker_doctor)
DOCTOR_REPORT = "doctor_report"
DOCTOR_EVERY_SECONDS = 24 * 3600  # how often the daemon sends a doctor report, besides when it starts
DOCTOR_FRESH_SECONDS = 48 * 3600  # a worker is narrowed to a unit only with a report this recent
# a daemon of a worker pool node that writes the hub's global skills into the skills directories of its runtimes when
# it starts and every 6 hours, with its worker token (GET /v1/worker/skills; evo_agents.worker.skill_sync_loop), as
# `hub skills sync` would, without `hub login`
SKILL_SYNC = "skill_sync"
SKILL_SYNC_EVERY_SECONDS = 6 * 3600  # how often a pool node syncs the skills, besides when it starts
# what this package's daemon says it does besides its kinds of run (features of its heartbeat)
WORKER_FEATURES = (
    SESSION_PER_STEP,
    AUTHOR_DECISIONS,
    AGENT_PROFILES,
    PLUGIN_RUNTIMES,
    ENVIRONMENTS,
    AGENT_PACKS,
    CHAT_DECISIONS,
    KG_SYNC,
    LAZY_CHAT_REPOS,
    UPDATE_NOW,
    MERGE_JUDGE,
    CLONES_CHECKOUTS,
    AUTONOMY,
    AGENT_HOME,
    BROWSER,
    DOCTOR_REPORT,
    BROWSER_INSTALL,
    SKILL_SYNC,
    REPOLESS_REVIEW,
    REATTACH,
)
HUB_FEATURES = WORKER_FEATURES  # the features a heartbeat may name that the hub keeps
Runtime = Literal["claude-code", "opencode", "codex"]
RUNTIMES: tuple[str, ...] = get_args(Runtime)  # the runtimes of the core; a plugin's is <plugin>/<name>
# A runtime's name, any included: one of RUNTIMES, or a plugin's <plugin>/<name> (evo_agents.plugins.runtimes). The hub
# takes a run of a runtime registered in its project (evo_agents.hub.server.runs.service.plugin_runtimes), not every
# name of this shape; schema 0039 holds runs to it.
RUNTIME_NAME = "^(" + "|".join(("any", *RUNTIMES)) + r"|[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9_-]{0,62})$"
# The runtime of a chat run of an agent that runs on the hub (profile runs_on hub): a job of the hub's queue takes each
# turn through the hub's gateway, with no worker (``evo_agents.hub.server.hub_runtime``). No dispatch names it; schema
# 0219 lets a run of it go past queued without a worker.
HUB_RUNTIME = "hub"
MAX_MODEL_CHARS = 200  # a run's model, one line (runs.model, as schema 0010 bounds it), and each model a runtime lists
MAX_RUNTIME_MODELS = 200  # the models a heartbeat lists for one runtime
MAX_RUNTIME_LOGINS = 50  # the runtimes whose login one heartbeat reports
MAX_EVIDENCE_BYTES = 16 * 1024  # runs.evidence, as schema 0009 bounds it
Mode = Literal["headless", "interactive"]
MODES: tuple[str, ...] = get_args(Mode)
Approval = Literal["auto", "review"]
APPROVALS: tuple[str, ...] = get_args(Approval)
# What a heartbeat answer carries: per run, whether to cancel, take over, hand back, open the terminal, park or end
# an author run's chat (finish), how many inbox messages and open decisions it has; for the worker, whether to drain.
CONTROLS = ("cancel", "takeover", "handback", "terminal_open", "park", "finish", "inbox", "decisions", "drain")

# a run waiting this long for its owner's answer is parked, so a worker's slot is held only while an answer may come
# at once; one that waits only for a time or an event of the hub, later than this, is parked at once
DECISION_WAIT_SECONDS = 10 * 60
PARKED_DAYS = 7  # a run parked this long is cancelled, and its open decisions expire
RESUME_KINDS = ("plan", "author", "chat")  # the kinds of run that wait for their owner, are parked and resumed
# the kinds whose run may go on in the session and worktrees of an earlier one (resume_of_run_id); a chat run that
# waits for nothing ends done with its turn, and its next turn is a new run too (``evo_agents.hub.chat_runs``)
SESSION_KINDS = RESUME_KINDS
PlanTimeout = Literal[2, 4, 8, 24]
PLAN_TIMEOUT_CHOICES: tuple[int, ...] = get_args(
    PlanTimeout
)  # hours of agent time a plan run may take; waiting and parked do not count

HeldState = Literal["leased", "running", "interactive", "verifying", "waiting"]
HELD_STATES: tuple[str, ...] = get_args(HeldState)  # a worker holds the run, extends its lease
ActiveState = Literal["queued", HeldState, "review", "parked"]
ACTIVE_STATES: tuple[str, ...] = get_args(ActiveState)  # at most one run of a step, or plan run of a plan
TerminalState = Literal["done", "failed", "lost", "cancelled"]
TERMINAL_STATES: tuple[str, ...] = get_args(TerminalState)
RunState = Literal[ActiveState, TerminalState]
RUN_STATES: tuple[str, ...] = get_args(RunState)
# A ``local`` run (``evo_agents.hub.server.local_runs``) is a Claude Code session on a member's machine, which the
# hub only watches: no worker holds it, so it has no lease, no slot and no attempts, and the moves above are not its
# own. Its state is the session's: running (a turn is under way), waiting (the agent asked a decision and waits for
# it), idle (the turn ended, the person has the word) or ended (the session is over, or went silent for
# LOCAL_SILENT_SECONDS).
# ``running`` and ``waiting`` are states of RUN_STATES; idle and ended are only a local run's, and ended is final
# until the session registers again.
LOCAL_STATES = ("running", "waiting", "idle", "ended")
LocalOnlyState = Literal["idle", "ended"]
LOCAL_ONLY_STATES: tuple[str, ...] = get_args(LocalOnlyState)
ShownState = Literal[RunState, LocalOnlyState]
SHOWN_STATES: tuple[str, ...] = get_args(ShownState)  # every state a run may be shown in, local runs included
LOCAL_SILENT_SECONDS = 24 * 3600  # a local run that sent nothing for this long is ended; the session may register again
MAX_LOCAL_FIELD = 500  # a local run's machine, cwd and account, one line each
# The time a run spends in these counts toward its timeout; waiting for an answer, parked, queued and review do not.
CLOCK_STATES = ("leased", "running", "interactive", "verifying")
TAKEOVER_STATES = ("leased", "running")  # the owner may ask for a takeover while the agent is not driven by a person
HANDBACK_STATES = ("interactive",)  # and for a handback while it is
MESSAGE_STATES = ("queued", *HELD_STATES)  # a message waits in the inbox only while an agent may still read it
ACTORS = ("worker", "owner", "reaper")

_W, _O, _R = frozenset({"worker"}), frozenset({"owner"}), frozenset({"reaper"})
_HELD_EXITS = {"failed": _W | _R, "cancelled": _W | _R, "lost": _R}  # the worker gives up or stops; the lease ends

TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    # the reaper cancels the next attempt of a lost run whose worker came back for it (``server.run_reattach``)
    "queued": {"leased": _W, "cancelled": _O | _R, "failed": _R},
    "leased": {"running": _W, "interactive": _W, **_HELD_EXITS},
    "running": {"interactive": _W, "verifying": _W, "waiting": _W, **_HELD_EXITS},
    "interactive": {"running": _W, "verifying": _W, **_HELD_EXITS},
    "verifying": {"done": _W, "review": _W, **_HELD_EXITS},
    # the agent's turn ended with a decision open: an answer sets it going again, DECISION_WAIT_SECONDS parks it
    "waiting": {"running": _W, "parked": _R, **_HELD_EXITS},
    "review": {"done": _O, "cancelled": _O},
    # no worker holds it: the owner's answer queues the run that resumes it, and this one is done, as does the reaper
    # when a wait of the run wakes (``server.run_waits``); PARKED_DAYS, or the owner, cancel it
    "parked": {"done": _O | _R, "cancelled": _O | _R},
    **{state: {} for state in TERMINAL_STATES},
}

# Decisions and notices of a plan run (docs/notifications.md). The agent asks only a decision of one of these
# categories, and decides everything else itself; each class of action that takes ask has its own (``autonomy_card``).
LOCAL_DECISION_CATEGORY = "question"  # what the agent of a local run asks that is none of DECISION_CATEGORIES
LOCAL_DECISION_CATEGORIES = (*DECISION_CATEGORIES, LOCAL_DECISION_CATEGORY)
CHAT_DECISION_CATEGORY = "triage"  # the one category the agent of a chat run asks in (see the triage of a failed run)
ASKED_DECISION_CATEGORIES = (*DECISION_CATEGORIES, CHAT_DECISION_CATEGORY)  # what a worker's run may ask
# what the hub alone asks the owner of a release cycle that stopped, on its last run (``server.release_cycles``)
RELEASE_DECISION_CATEGORY = "release"
# what the hub alone asks the person a work item of the Loop stands for after its third failure, on its last run
# (``server.loop.stall``)
LOOP_DECISION_CATEGORY = "loop"
# what a gate asks the principal of a run whose class is ask before it merges, deploys or releases (``autonomy_gates``)
AUTONOMY_DECISION_CATEGORY = "autonomy"
# the hub does what the answer chooses
HUB_DECISION_CATEGORIES = (RELEASE_DECISION_CATEGORY, LOOP_DECISION_CATEGORY, AUTONOMY_DECISION_CATEGORY)
# those of them about what the run they hang on stands for (a release cycle, a work item of the Loop) rather than the run
# itself: the run's end leaves them open (``server.run_state.move_run``)
OUTLIVING_DECISION_CATEGORIES = (RELEASE_DECISION_CATEGORY, LOOP_DECISION_CATEGORY)
# what the agent of a plan run opens with `evo-agents worker wait` to wait for a time or an event of the hub rather than
# for its owner (``server.run_waits``): the hub answers it when either comes, the owner may answer it sooner
WAIT_DECISION_CATEGORY = "wait"
MAX_WAIT_DAYS = PARKED_DAYS  # the latest a wait wakes; a run parked a day after it asked is still there to wake
SHOWN_DECISION_CATEGORIES = (
    *LOCAL_DECISION_CATEGORIES,
    CHAT_DECISION_CATEGORY,
    *HUB_DECISION_CATEGORIES,
    "login",
    WAIT_DECISION_CATEGORY,
)
DECISION_STATES = ("open", "answered", "expired", "cancelled")
DECISION_OPTIONS = (2, 6)  # the fewest and the most options a decision offers
OPTION_KEY = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"  # the key of an option, as `worker ask --option KEY=LABEL` gives it
MAX_DECISION_CONTEXT_BYTES = 16 * 1024  # the markdown context of a decision
MAX_QUESTION_CHARS = 2000  # the question of a decision
MAX_ANSWER_BYTES = 4 * 1024  # the owner's own text in an answer, which goes to the agent in an inbox message
MAX_NOTICE_BODY_BYTES = 16 * 1024  # the body of a notice, and of any notification
MAX_NOTICE_COMMITS = 100  # the commits a notice of a push or merge names
# The notices a plan run's worker may send (POST /v1/worker/runs/{id}/notices); the hub sends the last two itself too.
WORKER_NOTICE_KINDS = ("push_default_branch", "merge_default_branch", "plan_finished", "run_failed")
# curator_brief: the morning brief of a project's Curator, which the hub alone sends at the charter's brief_at;
# curator_paused: the circuit breaker of a project's night shift paused it, which the hub alone sends too;
# author_waiting: an author run waits for its owner's reply in its chat, which the hub alone sends as the run waits;
# chat_mention and chat_added: someone mentioned the member in a group chat, or added them to one (``chats``);
# plugin_disabled: the hub turned off an installed plugin that no longer runs on its release (``server.plugin_store``);
# builder_proposal: @builder proposed a change of settings that waits for the member's approval (core plugin builder);
# canvas_baseline: a baseline of a Lean Canvas waits for the review of the project's writers (core plugin canvas)
# merge_paused: the breaker of a repo of the merge queue tripped, which the hub alone sends (``server.merge_breaker``)
# pm_daily: the digest of a project's morning, pm_escalation: a blocker pm escalates (core plugin pm)
# release: a release cycle published its version, or the hub cannot read its CI (``server.release_watch``)
# loop_paused: the breaker of a project's Loop tripped on the work items it made in an hour (``server.loop.breaker``)
# goal_review: the first day of a month, the admins and goal owners of a project whose Loop is on review its goals;
# loop_brief: the Loop's part of the morning brief, for a project whose Curator sends none (``server.loop.notices``)
# autonomy: a gate of the hub acted for a run whose class is notify, told to the run's principal
# run_outputs: files a run's agent sent, or left in its outputs directory, gathered a minute a run, which the hub alone
# sends to the run's owner (``server.output_notices``)
# curator_probe_report: the weekly report of a project's probes, on Monday at the charter's brief_at, to the owner of its
# night shift's schedule (``server.probe_report``)
OUTPUT_NOTICE = "run_outputs"
PROBE_REPORT_NOTICE = "curator_probe_report"
NOTICE_KINDS = (
    *WORKER_NOTICE_KINDS,
    "curator_brief",
    "curator_paused",
    "author_waiting",
    "chat_mention",
    "chat_added",
    "plugin_disabled",
    "builder_proposal",
    "canvas_baseline",
    "merge_paused",
    "release",
    "pm_daily",
    "pm_escalation",
    OUTPUT_NOTICE,
    "loop_paused",
    "goal_review",
    "loop_brief",
    "plugin_notice",  # what an isolated plugin granted notifications:send sends (``server.plugin_notify``)
    AUTONOMY_DECISION_CATEGORY,
    "worker_doctor",  # the doctor of a worker narrowed to units found something high (``server.worker_doctor``)
    "browser_needs_login",  # a browser identity of the member waits for a new login (``server.browser_logins``)
    PROBE_REPORT_NOTICE,
    "opportunity_window",  # the window of an open opportunity closes or closed (``server.opportunity_deadlines``)
    "market_install_request",  # a project admin asks the hub admins to install a plugin of a market (``server.market``)
    "market_license_expiring",  # a paid plugin's licence ends in 7 days (``server.market_licenses``)
    "today",  # Today's summary push, to the member's devices alone, once a day (``server.today_digest``)
    "kg_optimize",  # the report of a project's KG optimize run, to its night shift's owner (``server.kg_optimize``)
    "agent_pool_empty",  # a run of an agent in pool mode queued with no node of its pool ready (``agent_placement``)
    "srs_adopt",  # a plan holds use cases of an SRS goal's backlog out of the Loop's builds (``server.loop.notices``)
    "kg_pipeline",  # the flow after a kg upload ended ready or failed, to its sender and admins (``server.kg_pipeline``)
    "system_daily_cap",  # a system's hub agents reached limits.daily_usd today, to its owner (``server.hub_runtime``)
    "srs_review",  # a version of an SRS document waits for its reviewers, after @ba's review (``server.srs.notices``)
    "srs_reviewed",  # a reviewer asked for changes on a version, or approved it, told to its author (same)
    "srs_comment",  # a comment reached a thread of an SRS document the member is in (``server.srs.threads``)
    "ops_cap",  # the ops threads of a project's default system reached limits.ops_daily_usd (``server.ops_threads``)
    "ops_paused",  # two ops runs in a row failed of their agent, which paused the system's ops (same)
    "ops_digest",  # what the ops of a project handled in a day and what waits on a person, to its owner (same)
    "cron_paused",  # the hub paused a cron job: its owner lost a role or seat, or its runs failed (``server.cron_fire``)
)
# The notices that only report what went as it should (plan hub-attention-runtime, criterion a4): a push or merge into a
# default branch, a plan finished, a run's files. Stored read, delivered on the web alone (no channel, no device), and
# counted in Today's summary (``server.today``) rather than the unread count. A notice of these kinds whose details carry
# an ``ask`` (the merge queue's audit sample, revert or release, ``server.post_merge``) waits for an answer: it is not one.
REPORT_NOTICE_KINDS = ("push_default_branch", "merge_default_branch", "plan_finished", OUTPUT_NOTICE)
REPORT_ASK = "ask"  # the key of a notice's details that makes a notice of REPORT_NOTICE_KINDS ask something


def report_only(kind: str | None, notice_kind: str | None, details: dict | None) -> bool:
    """Whether a notification of ``kind`` and ``notice_kind``, with ``details``, only reports (REPORT_NOTICE_KINDS)."""
    if kind != "notice" or notice_kind not in REPORT_NOTICE_KINDS:
        return False
    return not (isinstance(details, dict) and details.get(REPORT_ASK) is not None)


MUTABLE_NOTICE_KINDS = (OUTPUT_NOTICE,)  # the notices a member may turn off, and still get every other notification
OUTPUT_NOTICE_SECONDS = 60  # a notice of a run's files waits this long for more before any channel sends it
NOTIFICATION_KINDS = ("decision", "notice", "proposal")  # proposal: a tier 2 proposal of the Curator, in the Inbox
DELIVERY_STATES = ("pending", "delivered", "failed")  # of one notification on one channel
MAX_DELIVERY_ATTEMPTS = 5

WORKER_STATUSES = ("online", "offline", "draining", "revoked")
# What a daemon says of updating itself to the tag nightly (self_update.state of its heartbeat): current, it runs the
# commit nightly points to; waiting, nightly points to another one and it waits until every daemon of its install is
# idle; updating, it is installing that commit and restarts on it; locked, its owner locked the worker's version;
# unsupported, it cannot update itself, and self_update.reason says why. The hub keeps a state it does not know as the
# daemon names it.
SELF_UPDATE_STATES = ("current", "waiting", "updating", "locked", "unsupported")
SELF_UPDATE_REASON_CHARS = 500  # the most characters of self_update.reason a heartbeat carries
SELF_UPDATE_ERROR_CHARS = 2000  # and of self_update.error
# What a daemon of feature KG_SYNC last did for each git source of a project it is on duty for (kg_sync of its
# heartbeat): ok, the source was read or found at the commit read last, and every run of it is on the hub; failed, the
# sync, the pull of the hub's runs or the push went wrong, and error says how; skipped, the worker does not read it
# (a ref that is not origin/<branch>, a path: source, no checkout of its repo here), and error says why.
KG_SYNC_STATES = ("ok", "failed", "skipped")
KG_SYNC_ERROR_CHARS = 500  # the most characters of an error of kg_sync a heartbeat carries
MAX_KG_SYNC_PROJECTS = 50  # the projects of kg_sync in one heartbeat
MAX_KG_SYNC_SOURCES = 50  # and the sources of one project
# The repository and the tag the daemons update themselves to, and whose commit the hub reads once a minute and hands
# every daemon in its heartbeat answer (nightly; evo_agents.hub.nightly).
NIGHTLY_REPO_URL = "https://github.com/maycuatroi1/evo-agents"
NIGHTLY_TAG = "nightly"
# How an owner's Update now of a worker ended (workers.update_outcome): current, its daemon runs the commit of nightly
# the hub read; cancelled, the owner or a hub admin cancelled it; failed, the install failed (self_update.error says
# how); impossible, the daemon cannot update itself (locked, an install it cannot update, a daemon of its install
# locked or run by hand; self_update.reason says why). A daemon reports the last two in self_update.update_now.
UPDATE_NOW_OUTCOMES = ("current", "cancelled", "failed", "impossible")
UPDATE_NOW_DAEMON_ENDS = ("failed", "impossible")  # the outcomes a daemon reports
# How an owner's Install browser of a worker ended (workers.browser_install_outcome): installed, its heartbeat lists
# the feature browser; cancelled, the owner or a hub admin cancelled it; failed, the install failed (the daemon's
# browser_install.error says how); impossible, its daemon no longer says browser_install. A daemon reports failed.
BROWSER_INSTALL_OUTCOMES = ("installed", "cancelled", "failed", "impossible")
BROWSER_INSTALL_DAEMON_ENDS = ("failed",)
MAX_BROWSER_INSTALL_ERROR = 2000  # the characters of why an install failed that a heartbeat carries

ACP_EVENT_KINDS = (  # the session/update kinds of the Agent Client Protocol
    "agent_message_chunk",
    "agent_thought_chunk",
    "tool_call",
    "tool_call_update",
    "plan",
    "usage_update",
)
HUB_EVENT_KINDS = ("user_message", "state", "system", "output")  # output: a runtime event no adapter knows, raw
EVENT_KINDS = ACP_EVENT_KINDS + HUB_EVENT_KINDS
WORKER_EVENT_KINDS = ACP_EVENT_KINDS + ("system", "output")  # the hub alone writes user_message and state

EVENT_CUT_MARK = "\n[cut by the hub: the event was over {limit} bytes]"
MAX_CUT_PASSES = 64  # strings of one body fit_event_body cuts before it keeps the start of the body's JSON instead
NUL_STAND_IN = "�"  # what a NUL character of an event body is stored as: Postgres keeps NUL out of jsonb
NUL = re.compile("\0")
SURROGATE = re.compile("[\ud800-\udfff]")  # a lone one: JSON decodes a pair to one character
SURROGATE_STAND_IN = "\ufffd"  # what a lone surrogate of an event body is stored as: UTF-8 cannot encode one

RESULT_DIR = ".evo-run"  # in the run's checkout, or above a plan run's worktrees; never committed
# What a run makes beside the work to run it, at any depth: a virtualenv, or a link to one in a cache of the machine.
# The worker leaves a new one out of a run's commit (``worker.gitops.commit_run``); the hub opens no draft pull request
# of a branch that changes nothing else (``hub.server.draft_pulls``).
WORKER_MADE = (".venv",)


def worker_made(path: str) -> bool:
    """Whether ``path`` (relative to the work tree, with /) is, or lies under, a name of WORKER_MADE at any depth."""
    return any(part in WORKER_MADE for part in path.split("/"))


RESULT_FILE = f"{RESULT_DIR}/result.json"  # {"verify_commands": [str, ...], "summary": str}, written by the agent
PLAN_FILE = f"{RESULT_DIR}/plan.yaml"  # a plan run's plan as it was claimed, written by the daemon

OUTPUT_COMMAND = "evo-agents worker output"  # the agent of any run sends files to the run's outputs

TRUNCATION_MARK = "\n[cut by the hub: {omitted} of {total} bytes left out; the plan on the hub has the full text]"
SHORT_BYTES = 200  # a plan id, step key, title, repo or branch in the prompt


class RunProblem(ValueError):
    """A move, a run or a plan the hub refuses; ``str`` says why for the person or worker who asked."""


class TransitionRefused(RunProblem):
    pass


# States


def can_transition(old: str, new: str, actor: str) -> bool:
    """Whether ``actor`` may move a run from ``old`` to ``new``."""
    return actor in TRANSITIONS.get(old, {}).get(new, ())


def check_transition(old: str, new: str, actor: str) -> None:
    """Raise TransitionRefused unless TRANSITIONS lets ``actor`` move a run from ``old`` to ``new``."""
    for state in (old, new):
        if state not in TRANSITIONS:
            raise TransitionRefused(f"{state!r} is not a run state; the states are {', '.join(RUN_STATES)}")
    if actor not in ACTORS:
        raise TransitionRefused(f"{actor!r} cannot move runs; the ones who can are {', '.join(ACTORS)}")
    if old == new:
        raise TransitionRefused(f"the run is already {new}")
    if old in TERMINAL_STATES:
        raise TransitionRefused(f"the run is {old}, which is final: running the step again takes a new run")
    allowed = TRANSITIONS[old].get(new)
    if not allowed:
        raise TransitionRefused(f"a run cannot go from {old} to {new}")
    if actor not in allowed:
        who = " or the ".join(name for name in ACTORS if name in allowed)
        raise TransitionRefused(f"the {actor} cannot move a run from {old} to {new}; only the {who} can")


def check_reattach(old: str, new: str, actor: str) -> None:
    """Raise TransitionRefused unless the move re-attaches a lost run: the reaper's, from lost back to a held state
    (REATTACH_SECONDS, ``server.run_reattach``), the one move out of an end state."""
    if old != "lost" or new not in HELD_STATES or actor != "reaper":
        raise TransitionRefused(f"only the reaper re-attaches a run, from lost to a held state, not {old} to {new}")


# Time toward the timeout


def run_seconds(spent: int, state: str, since: datetime | None, now: datetime) -> int:
    """The seconds of a run's timeout used by ``now``: ``spent``, those counted up to ``since``, plus the time from
    ``since`` to ``now`` while the run is in one of CLOCK_STATES. A run waiting for its owner's answer, parked, queued
    or in review adds nothing, and neither does a ``since`` after ``now`` (two clocks apart). The hub settles the count
    at each move and heartbeat, so a run that ran an hour, waited a day for an answer and ran another hour has used two
    hours of its timeout."""
    if since is None or state not in CLOCK_STATES:
        return spent
    return spent + max(0, int((now - since).total_seconds()))


def past_timeout(spent: int, state: str, since: datetime | None, now: datetime, timeout_s: int) -> bool:
    """Whether a run has used more than ``timeout_s`` seconds of agent time by ``now`` (see ``run_seconds``)."""
    return run_seconds(spent, state, since, now) > timeout_s


# Events


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _longest_string(value) -> tuple[object, object, int] | None:
    """(container, key, size) of the string in ``value`` whose JSON is the largest, among those longer than a cut mark;
    None when there is none. A walk with a stack of its own, so a deeply nested body cannot exhaust the interpreter's."""
    best = None
    stack = [value]
    while stack:
        node = stack.pop()
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for key, child in items:
            if isinstance(child, str):
                size = _json_size(child)
                if size > len(EVENT_CUT_MARK) + 16 and (best is None or size > best[2]):
                    best = (node, key, size)
            elif isinstance(child, (dict, list)):
                stack.append(child)
    return best


def _cut_strings(body: dict, limit: int) -> bool:
    """Cut the longest strings of ``body`` in place until its JSON takes at most ``limit`` bytes; whether it does."""
    mark = EVENT_CUT_MARK.format(limit=limit)
    mark_size = _json_size(mark) - 2  # in a JSON string, without its quotes
    for _ in range(MAX_CUT_PASSES):
        size = _json_size(body)
        if size <= limit:
            return True
        found = _longest_string(body)
        if found is None:
            return False
        node, key, _ = found
        text = node[key].removesuffix(mark)  # a string cut before is cut again from its text, not its mark
        size -= _json_size(node[key]) - _json_size(text)
        data = text.encode()
        # Drop as many bytes as the text's JSON takes per byte (more where it is escaped); a string escaped unevenly
        # may need another pass.
        ratio = (_json_size(text) - 2) / len(data)
        drop = math.ceil((size + mark_size - limit) / ratio)
        node[key] = data[: max(0, len(data) - drop)].decode("utf-8", "ignore") + mark
    return _json_size(body) <= limit


def fit_event_body(body: dict, limit: int = MAX_EVENT_BODY_BYTES) -> tuple[dict, bool]:
    """``body`` and False when its JSON takes at most ``limit`` bytes of UTF-8. Otherwise a cut copy and True: its
    longest strings end early with EVENT_CUT_MARK, so the keys of the body stay and the web shows the start of the
    text; and when cutting strings is not enough (a body of many short values), ``{"cut": ...}`` with the start of
    the body's JSON as text. A lone UTF-16 surrogate, which JSON's ``\\ud800`` escapes decode to and UTF-8 cannot
    encode, comes back as U+FFFD first (``without_surrogates``)."""
    body = without_surrogates(body)
    if _json_size(body) <= limit:
        return body, False
    copy = json.loads(json.dumps(body))
    if _cut_strings(copy, limit):
        return copy, True
    fallback = {"cut": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}
    if not _cut_strings(fallback, limit):
        fallback = {"cut": EVENT_CUT_MARK.format(limit=limit).strip()}
    return fallback, True


def _mended(body: dict, bad: re.Pattern, stand_in: str) -> dict:
    """A copy of ``body`` with each match of ``bad`` in its keys and strings as ``stand_in``. A walk with a stack of
    its own, as ``_longest_string``."""
    copy = json.loads(json.dumps(body))
    stack = [copy]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in [key for key in node if bad.search(key)]:
                node[bad.sub(stand_in, key)] = node.pop(key)
        items = list(node.items() if isinstance(node, dict) else enumerate(node))
        for key, child in items:
            if isinstance(child, str):
                node[key] = bad.sub(stand_in, child)
            elif isinstance(child, (dict, list)):
                stack.append(child)
    return copy


def without_nul(body: dict) -> dict:
    """``body`` when none of its keys and strings holds a NUL character; else a copy with each NUL as NUL_STAND_IN.
    Postgres refuses a jsonb value with a NUL in it, so one tool's binary output would fail its whole batch, again at
    each resend."""
    if "\\u0000" not in json.dumps(body):
        return body
    return _mended(body, NUL, NUL_STAND_IN)


def without_surrogates(body: dict) -> dict:
    """``body`` when its keys and strings encode to UTF-8; else a copy with each lone UTF-16 surrogate as U+FFFD. JSON
    decodes a pair of surrogate escapes to the one character they make, so a surrogate left is a lone one, which
    neither UTF-8 nor Postgres takes: one tool's broken output would fail its whole batch, again at each resend."""
    try:
        json.dumps(body, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        return _mended(body, SURROGATE, SURROGATE_STAND_IN)
    return body


def step_title(step: dict) -> str | None:
    """The title a run keeps of its step: one line of at most SHORT_BYTES characters, or None."""
    title = step.get("title") if isinstance(step, dict) else None
    if title is None:
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text_of(title)).split())
    return text[:SHORT_BYTES] or None


# Workers


def worker_status(
    *, last_heartbeat_at: datetime | None, drained_at: datetime | None, revoked_at: datetime | None, now: datetime
) -> str:
    """``revoked`` once revoked; else ``offline`` when the last heartbeat is more than OFFLINE_AFTER_SECONDS old or
    there was none; else ``draining`` once drained (it finishes its runs and claims no new one); else ``online``.
    Offline wins over draining: a drained worker that is down finishes nothing."""
    if revoked_at is not None:
        return "revoked"
    if last_heartbeat_at is None or now - last_heartbeat_at > timedelta(seconds=OFFLINE_AFTER_SECONDS):
        return "offline"
    if drained_at is not None:
        return "draining"
    return "online"


def takes_plan_runs(agent_version: str | None) -> bool:
    """Whether a daemon of ``agent_version`` runs a plan run: PLAN_RUN_AGENT or later. A version whose release
    cannot be read does not, as an older daemon would fail the run."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", (agent_version or "").strip())
    if match is None:
        return False
    return tuple(int(part or 0) for part in match.groups()) >= PLAN_RUN_AGENT


def version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


# Which steps are ready


def plan_steps(body: dict) -> list:
    """The steps of the plan ``body``; none when it has no list of them."""
    steps = body.get("steps")
    return steps if isinstance(steps, list) else []


def step_status(step: dict):
    """The status of ``step``; pending when it has none."""
    status = step.get("status")
    return "pending" if status is None else status


def unready_reason(body: dict, step) -> str | None:
    """Why ``step``, one of the steps of ``body``, is not ready to run; None when it is."""
    if not isinstance(step, dict):
        return "it is a bare string, not a mapping, so it has no status"
    if step_status(step) != "pending":
        return f"its status is {step_status(step)}, not pending"
    depends_on = step.get("depends_on")
    if depends_on is None:
        return None
    if not isinstance(depends_on, list):
        return "its depends_on is not a list of step ids"
    statuses: dict[str, object] = {}
    for index, other in enumerate(plan_steps(body)):
        if isinstance(other, dict):
            statuses.setdefault(step_key(other, index), step_status(other))
    waiting = []
    for dep in depends_on:
        key = str(dep)
        if key not in statuses:
            waiting.append(f"step {key}, which the plan does not have")
        elif statuses[key] != "done":
            waiting.append(f"step {key} ({statuses[key]})")
    return f"it waits for {', '.join(waiting)}" if waiting else None


def ready_steps(body: dict) -> list[dict]:
    """The steps of the plan ``body`` that are ready to run, in plan order (see the module's docstring)."""
    return [step for step in plan_steps(body) if unready_reason(body, step) is None]


def next_session_step(body: dict, skip: Collection[str] = ()) -> tuple[str, dict] | None:
    """The step a plan run with a session per step gives the next session: the first, in plan order, whose key is not
    in ``skip`` (the steps that had a session in the run already), whose status is pending or in_progress (a step an
    earlier session or run left started, since the plan run alone works on the plan), and whose depends_on names only
    steps done. Its key and the step; None when no step is left to start."""
    for index, step in enumerate(plan_steps(body)):
        if not isinstance(step, dict) or step_status(step) not in ("pending", "in_progress"):
            continue
        key = step_key(step, index)
        if key not in skip and unready_reason(body, {**step, "status": "pending"}) is None:
            return key, step
    return None


def steps_to_go(body: dict) -> list[str]:
    """The keys of the steps of the plan ``body`` a plan run still has to do, in plan order: each pending or
    in_progress one whose depends_on names only steps done. A blocked or descoped step, or one that waits for a step
    not done, is not among them."""
    return [
        step_key(step, index)
        for index, step in enumerate(plan_steps(body))
        if isinstance(step, dict)
        and step_status(step) in ("pending", "in_progress")
        and unready_reason(body, {**step, "status": "pending"}) is None
    ]


def verify_of(step) -> list[str]:
    """The verify of ``step`` as a list of commands: its verify, one shell command line as plan.schema.json has it, or
    none when it has none."""
    verify = step.get("verify") if isinstance(step, dict) else None
    return [verify.strip()] if isinstance(verify, str) and verify.strip() else []


def open_verify(body: dict) -> list[tuple[str, str]]:
    """(step key, verify command) of every step of the plan ``body`` not done yet, in plan order, each command once:
    what a plan run's worker checks before its agent starts, naming the step a problem comes from."""
    found: list[tuple[str, str]] = []
    for index, step in enumerate(plan_steps(body)):
        if isinstance(step, dict) and step.get("status") != "done":
            key = step_key(step, index)
            found += [(key, command) for command in verify_of(step) if all(command != had for _, had in found)]
    return found


def plan_repo(plan: dict, step: dict) -> dict | None:
    """The entry of the plan's ``repos`` for the repo ``step`` names; the plan's only repo when the step names none.
    A repo the step names but the plan does not list gets an entry without a branch."""
    repos = [entry for entry in plan.get("repos") or [] if isinstance(entry, dict)]
    name = step.get("repo")
    if name is None:
        return repos[0] if len(repos) == 1 else None
    return next((entry for entry in repos if entry.get("repo") == name), {"repo": name})


# Texts cut to a budget


def clip(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` UTF-8 bytes: whole when it fits, else its start, cut on a character boundary,
    and TRUNCATION_MARK saying how much was left out."""
    data = text.encode()
    if len(data) <= limit:
        return text
    longest_mark = TRUNCATION_MARK.format(omitted=len(data), total=len(data)).encode()
    if len(longest_mark) >= limit:
        return data[:limit].decode("utf-8", "ignore")
    head = data[: limit - len(longest_mark)].decode("utf-8", "ignore")
    return head + TRUNCATION_MARK.format(omitted=len(data) - len(head.encode()), total=len(data))


def text_of(value) -> str:
    """``value`` as text: a string stripped, nothing for None, anything else as JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)
