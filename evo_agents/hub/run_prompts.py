"""The prompt a run hands its agent: a run of one step's (``build_prompt``), a plan run's (``build_plan_prompt``) and
the prompt of a step's session in a plan run with a session per step (``build_step_session_prompt``). Each holds the
rules the agent works by, the plan's goal and context, the step or the steps not done yet, and the evidence of the
steps a step depends on, each part cut to its own budget and the whole to ``runs.MAX_PROMPT_BYTES``. Pure functions
over JSON values, so the api and the worker daemon share them; the states of a run and the protocol they move by are
``evo_agents.hub.runs``."""

from __future__ import annotations

from evo_agents.hub.autonomy_card import checkpoint_rule, decision_lines, ships, with_card
from evo_agents.hub.decision_asks import ASK_RULE
from evo_agents.hub.outputs import OUTPUTS_DIR
from evo_agents.hub.plans import step_key
from evo_agents.hub.proofs import criteria, prompt_lines, result_how, worker_how
from evo_agents.hub.record_rule import record_rule
from evo_agents.hub.runs import (
    DECISION_OPTIONS,
    MAX_PROMPT_BYTES,
    MAX_WAIT_DAYS,
    OUTPUT_COMMAND,
    PLAN_FILE,
    RESULT_DIR,
    RESULT_FILE,
    SHORT_BYTES,
    clip,
    plan_steps,
    step_status,
    text_of,
)

# Said to the agent of a run of one step, a plan run, a step's session, a chat run and a review run.
OUTPUTS_RULE = (
    "- To show the people who read this run a file you made (a screenshot, a video, a report), send it with "
    f"`{OUTPUT_COMMAND} FILE --caption TEXT` (several files at once if you like), or leave it in {OUTPUTS_DIR}/ "
    "under the current directory, which the worker sends at each step you report and when the run ends. The run's "
    "page on the hub shows them; the images of your tools' results may get there by themselves."
)
ATTACH_RULE = "  Add `--attach FILE` for a screenshot or a file the owner should see with the question."

GOAL_BYTES = 2 * 1024
CONTEXT_BYTES = 4 * 1024
WHAT_BYTES = 10 * 1024
VERIFY_BYTES = 3 * 1024
NOTE_BYTES = 2 * 1024
ACCEPTANCE_BYTES = 4 * 1024  # the criteria a step names, with the plan's words, and how to prove each
EVIDENCE_BYTES = 1536  # the evidence of one step this one depends on
DEPENDENCIES_BYTES = 5 * 1024  # all of it
LIST_TITLE_BYTES = 120  # the title of a step in a plan run's list of steps
LIST_DEPENDS_BYTES = 120  # and its depends_on
STEP_LIST_BYTES = 12 * 1024  # the whole list; the steps past it are counted, and the plan file has them
REPOS_BYTES = 3 * 1024  # a plan run's repos with their branches and worktrees


# The prompt of a run of one step


def _short(value, missing: str) -> str:
    text = " ".join(text_of(value).split())
    return clip(text, SHORT_BYTES) if text else missing


def _rules(branch: str) -> list[str]:
    return [
        "Rules for this run:",
        "- Do this one step and nothing else. The other steps of the plan get runs of their own.",
        "- Do not record progress in the plan: do not call the hub's plan_step tool, `evo harness step` or "
        "`evo-agents hub plan`. The hub writes this step's status and evidence from what this run reports.",
        f"- Work in the current directory and commit there, on the branch it is on ({branch}). Do not switch "
        "branches, merge or push: the worker pushes the branch when the run ends.",
        f"- Before you finish, write {RESULT_FILE} as a JSON object with two keys, and keep {RESULT_DIR}/ out of "
        "your commits:",
        '  "verify_commands": the shell commands you ran to check this step, each one a string;',
        '  "summary": what you changed, how you checked it, and anything left undone.',
        "  The worker runs every verify command again in this directory, and the step counts as verified only when "
        "each one exits 0.",
        OUTPUTS_RULE,
    ]


def _dependencies(plan: dict, step: dict) -> list[str]:
    depends_on = step.get("depends_on")
    if not isinstance(depends_on, list) or not depends_on:
        return []
    by_key: dict[str, dict] = {}
    for index, other in enumerate(plan_steps(plan)):
        if isinstance(other, dict):
            by_key.setdefault(step_key(other, index), other)
    parts = []
    for dep in depends_on:
        other = by_key.get(str(dep))
        if other is None:
            parts.append(f"## Step {_short(dep, '?')}\nThe plan has no such step.")
            continue
        evidence = clip(text_of(other.get("evidence")) or "No evidence recorded.", EVIDENCE_BYTES)
        parts.append(f"## Step {_short(dep, '?')}: {_short(other.get('title'), 'untitled')}\n{evidence}")
    return ["# Evidence from the steps this one depends on", clip("\n\n".join(parts), DEPENDENCIES_BYTES)]


def _step_lines(step: dict, key: str, plan: dict | None = None, how: str = "") -> list[str]:
    """The step's title, what, verify, the criteria of ``plan``'s acceptance it names with ``how`` the agent proves
    each (none when it names none), and its note, each cut to its own budget, as the prompt of a run of one step has
    them."""
    lines = ["", f"# Step {key}: {_short(step.get('title'), 'untitled')}"]
    for heading, value, budget in (
        ("## What to do", step.get("what"), WHAT_BYTES),
        ("## How the step is verified", step.get("verify"), VERIFY_BYTES),
    ):
        if text_of(value):
            lines += ["", heading, clip(text_of(value), budget)]
    wanted = prompt_lines(criteria(plan, step), how)
    if wanted:
        lines += ["", clip("\n".join(wanted), ACCEPTANCE_BYTES)]
    if text_of(step.get("note")):
        lines += ["", "## Note", clip(text_of(step.get("note")), NOTE_BYTES)]
    return lines


def build_prompt(plan: dict, step: dict, repo: dict | None) -> str:
    """The prompt of a run of ``step``, one of the steps of ``plan``, in the checkout of ``repo`` (an entry of the
    plan's ``repos``, as ``plan_repo`` finds it): what the agent must and must not do, the plan's goal and its
    context, shortened, the step's what, verify and note, and the evidence of the steps it depends on. Each part is
    cut to its own budget, and the whole to MAX_PROMPT_BYTES, with TRUNCATION_MARK where something was cut."""
    index = next((position for position, other in enumerate(plan_steps(plan)) if other is step), 0)
    key = _short(step_key(step, index), "?")
    repo = repo or {}
    branch = _short(repo.get("branch"), "the branch checked out")
    lines = [
        f"You are running step {key} of the plan {_short(plan.get('id'), 'without an id')} from the evo-agents hub.",
        "",
        *_rules(branch),
        "",
        f"Repository: {_short(repo.get('repo'), 'not named by the plan')}. Branch: {branch}.",
    ]
    for heading, value, budget in (
        ("# Goal of the plan", plan.get("goal"), GOAL_BYTES),
        ("# Context of the plan, shortened", plan.get("context"), CONTEXT_BYTES),
    ):
        if text_of(value):
            lines += ["", heading, clip(text_of(value), budget)]
    lines += _step_lines(step, key, plan, result_how())
    dependencies = _dependencies(plan, step)
    if dependencies:
        lines += ["", *dependencies]
    return clip("\n".join(lines) + "\n", MAX_PROMPT_BYTES)


# The prompt of a plan run


def _push_rule(autonomy) -> str:
    """The rule of a plan run on its pushes and merges. The merge of a pull request the run opened into a repo's
    default branch is K5: the agent's with no question when merging is its own (``autonomy_card.ships``), and as its
    card says when it is not; run #820 asked its owner twice whether it might merge its own pull request, at A5. Runs
    #1404, #1414 and #1415 asked their owner to merge release pull requests gh called blocked: the workers' App is an
    "Always allow" bypass actor of evo-agents' main ruleset, which gh shows it as BLOCKED until it merges with
    ``--admin``."""
    if ships((autonomy or {}).get("modes")):
        merge = (
            ", and merge a pull request you opened into that repo's default branch with no question to the owner: "
            "merging is yours."
        )
    else:
        merge = "; a pull request you opened merges into that repo's default branch only as your autonomy card says."
    return (
        "- Commit in each repo's worktree, on the branch it is on. You may push, and merge into, the branch the plan "
        f"names for a repo, even when it is the repo's default branch{merge} Never push to a branch the plan does not "
        "name for that repo, never merge into any other branch, never force-push, and never rewrite history you "
        "pushed. After each push or merge into a repo's default branch that you make yourself (the pushes of "
        "`evo-agents worker step` send their own notice), run `evo-agents worker notify --kind push_default_branch "
        "--title TEXT --body TEXT`, or `--kind merge_default_branch`, naming the repo, the branch and the commits. "
        "In a repo whose policy has the merge check on, check first and merge after: `evo-agents worker merge-check "
        "REPO` has the hub judge your pull request at its head (end your turn while it waits), and only a passed "
        "verdict opens `gh pr merge N --match-head-commit HEAD` (or `gh api -X PUT repos/OWNER/NAME/pulls/N/merge -f "
        "sha=HEAD`) with that same head; the hook refuses a merge without it, and a push after the verdict needs a new "
        "check. The hub reads the merge afterwards and tells the owner when it was at another head or off the base. "
        "When gh answers that the base branch policy prohibits the merge, run the same `gh pr merge` again with "
        "`--admin`: a ruleset that lets the hub's GitHub App bypass it as Always allow still shows the pull "
        "request as blocked to the App, and GitHub refuses `--admin` to an actor it does not let bypass. Ask the owner "
        "to merge only when GitHub refuses that too."
    )


# A decision the agent of a plan run must not ask: one that only asks the owner to answer another (run #820 asked its
# owner to answer the hub's release decision #462, which the owner had in the Inbox already).
RELAY_RULE = (
    "- Never ask the owner to answer another open decision (one the hub asked, such as a stopped release cycle's, or "
    "one of another run): they have it already. Wait for what it brings instead, with `evo-agents worker wait "
    "--until +30m`, then look again."
)


# How the agent of a plan run waits for a time or an event of the hub (``server.run_waits``) instead of asking.
WAIT_RULE = (
    "- When the work waits for a time, or for something that happens on the hub (a step of another plan done, a "
    "baseline of a canvas approved, a claim or a task changing, signals posted), wait for it rather than ask the owner "
    "to tell you when it came: `evo-agents worker wait --reason TEXT --until TIME --on KIND --contains KEY=VALUE "
    f"--step {{step}}`, then end your turn. TIME is 2026-10-30T09:00Z or a span such as +6h, at most {MAX_WAIT_DAYS} "
    "days ahead; KIND is plan.step, canvas.baseline, canvas.claim, tasks.status, signal.posted or "
    "srs.version.approved, and each --contains a key of the event's payload with its value: a step of another plan "
    "done is `--on plan.step --contains plan=PLAN --contains step=STEP --contains status=done`, which wakes as soon "
    "as it is done. Give --until, --on or both. The hub wakes this session with a message naming "
    "the wait when the first of them comes. An option of a decision that only means not yet goes with `--wait-option "
    "KEY` of `evo-agents worker ask`: choosing it leaves the decision open, and you hear nothing until a real answer."
    "\n- A GitHub Actions run (the CI of a tag or of a commit pushed, a deploy) is waited for on the run itself: its "
    "id is the databaseId of `gh run list --repo OWNER/NAME --workflow FILE --branch REF --limit 1 --json databaseId`, "
    "then `evo-agents worker wait --reason TEXT --on github.workflow_run --contains repo=OWNER/NAME --contains "
    f"run_id=ID --until +90m --step {{step}}` and end your turn. The hub reads that run every minute and wakes you on "
    "its first failed job, so fix a red tag forward at once, or when it completes; `--contains status=completed` "
    "waits for its end alone."
    "\n- Something else elsewhere that takes more than a few minutes (the checks of a pull request, a package on an "
    "index, a build or a run on the hub) is waited for the same way with a time: `evo-agents worker wait --reason "
    f"TEXT --until +15m --step {{step}}`, then end your turn; when it wakes, look once and wait again if it has not "
    "come. Never wait for it in this session (no Monitor, no loop that sleeps, no long sleep, no `gh run watch`): the "
    "session holds the worker's slot while it waits, and only a wait parks the run."
    "\n- Start now what you can start rather than wait for a schedule: a release is `evo-agents hub release run "
    "--project P`. Refused, ask the owner for what the refusal names and end your turn, never a long wait."
)


# How the agent of a plan run asks for a credential it lacks (``credential_requests``), rather than asking the owner to
# sign in on the worker's machine or to run `evo cred` there (plan owner-run-access).
CREDENTIAL_RULE = (
    "- When a command lacks a credential, a variable such as an API key or a sign-in to the hub (`evo-agents hub` "
    "says not signed in), never ask the owner to sign in on this machine or to put it into `evo cred` here: ask for "
    "it with `evo-agents worker ask --category credential --question TEXT --env-var NAME --hub-account --step "
    "{step}`, --env-var for the variable, --hub-account for the owner's account on the hub, or both in one decision. "
    'The hub gives the decision its options. Once the owner answered, read what it gave with `eval "$(evo-agents '
    'worker env --run "$EVO_RUN_ID")"` before the command that needs it.'
)


def _plan_rules(autonomy) -> list[str]:
    fewest, most = DECISION_OPTIONS
    return [
        "Rules for this plan run:",
        "- If your runtime has the execute-plan skill, use it: it knows how to work inside a worker's plan run "
        "(EVO_RUN_KIND=plan). Without it, follow these rules yourself.",
        f"- {PLAN_FILE} holds the whole plan as this run was claimed. Before each step, read the plan as the hub "
        "holds it now with `evo-agents worker plan`: the hub keeps the plan's progress, not your memory of it.",
        "- Start every step whose depends_on are all done at once, each in a fresh subagent, all spawned in one "
        "message. The first of a repo works in the run's worktree of that repo; another of the same repo works in a "
        "git worktree of its own under the current directory, on a local branch, and is merged into the run's "
        "worktree before `evo-agents worker step KEY done`, which commits whatever the run's worktree holds. Run two "
        "steps of a repo one after another only when they will certainly conflict on merge.",
        checkpoint_rule(autonomy),
        record_rule("KEY", "a step"),
        *decision_lines("its step", autonomy),
        "- To ask: `evo-agents worker ask --category CATEGORY --question TEXT --context-file FILE "
        f"--option KEY=LABEL[:DESCRIPTION] --recommended KEY --recommended-reason TEXT --step KEY`, with {fewest} to "
        f"{most} options. It prints the decision's id. Go on with the work that does not depend on the answer; when "
        "none is left, end your turn. The answer comes back in this session as a message naming the decision's id, "
        "and the time you wait does not count toward the run's timeout.",
        ASK_RULE,
        WAIT_RULE.format(step="KEY"),
        CREDENTIAL_RULE.format(step="KEY"),
        RELAY_RULE,
        ATTACH_RULE,
        "- A step blocked by what only the owner can change (a permission of the GitHub App, access to a service, a "
        "push the remote refuses; a credential it lacks is asked as above) is a scope decision, not a stop: ask it, "
        "naming what the owner must do, with a way around the block among the options when there is one, and end "
        "your turn. When the answer did not lift the block, ask again with what you found; never end the run on it.",
        _push_rule(autonomy),
        OUTPUTS_RULE,
        f"- Keep {RESULT_DIR}/ out of your commits. A turn that ends with no decision open ends the run: the worker "
        "pushes what you committed and lets the session go. So stop only when every step is done, or when the "
        f'owner\'s answer tells you to; then write {RESULT_FILE} as a JSON object whose "summary" says which steps '
        "you finished, what you decided yourself, and what is left and why. The worker runs no verify command at "
        "the end of a plan run: `evo-agents worker step` ran each step's.",
    ]


def _line(value, limit: int, missing: str) -> str:
    """``value`` as one line of at most ``limit`` UTF-8 bytes: blanks collapsed, and a longer one cut on a character
    boundary and ended with "...", since a list keeps one line per entry; ``missing`` when it is empty. Only the start
    of a long value is read, so a list of many long values costs little."""
    text = value.lstrip() if isinstance(value, str) else text_of(value)
    head = 4 * limit  # characters enough to fill the line, unless the value is mostly blanks
    cut = len(text) > head
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text[:head]).split())
    if not text:
        return missing
    data = text.encode()
    if len(data) <= limit and not cut:
        return text
    return data[: max(0, limit - 3)].decode("utf-8", "ignore").rstrip() + "..."


def _packed(lines: list[str], budget: int, rest: str) -> list[str]:
    """As many of ``lines`` as fit in ``budget`` bytes, whole, then ``rest`` formatted with how many were left out."""
    kept, used = [], 0
    for shown, line in enumerate(lines):
        if used + len(line.encode()) + 1 > budget:
            return [*kept, rest.format(left=len(lines) - shown)]
        kept.append(line)
        used += len(line.encode()) + 1
    return kept


def _repo_lines(repos: list, worktrees: dict) -> list[str]:
    lines = []
    for entry in repos:
        entry = entry if isinstance(entry, dict) else {"repo": entry}
        name = text_of(entry.get("repo"))
        branch = _line(entry.get("branch"), SHORT_BYTES, "the branch checked out")
        worktree = _line(worktrees.get(name, name), SHORT_BYTES, "not made yet")
        lines.append(f"- {_line(name, SHORT_BYTES, 'unnamed')}: branch {branch}, worktree {worktree}")
    if not lines:
        return ["- none named; the plan file says which repos the steps are in"]
    return _packed(lines, REPOS_BYTES, f"- and {{left}} more repos, which {PLAN_FILE} lists")


def _step_line(step, index: int) -> str:
    if not isinstance(step, dict):
        return f"- {index} [pending] {_line(step, LIST_TITLE_BYTES, 'untitled')}"
    repo = _line(step.get("repo"), SHORT_BYTES, "")
    facts = [f"repo {repo}"] if repo else []
    depends_on = step.get("depends_on")
    if isinstance(depends_on, list) and depends_on:
        names, used = [], 0
        for dep in depends_on:  # as many as the line shows, and one more so that it ends with "..."
            names.append(_line(dep, SHORT_BYTES, "?"))
            used += len(names[-1].encode()) + 2
            if used > LIST_DEPENDS_BYTES:
                break
        facts.append("after " + _line(", ".join(names), LIST_DEPENDS_BYTES, "?"))
    tail = f" ({'; '.join(facts)})" if facts else ""
    key = _line(step_key(step, index), SHORT_BYTES, "?")
    status = _line(step_status(step), SHORT_BYTES, "pending")
    return f"- {key} [{status}] {_line(step.get('title'), LIST_TITLE_BYTES, 'untitled')}{tail}"


def _step_list(plan: dict) -> list[str]:
    steps = plan_steps(plan)
    open_steps = [
        (index, step) for index, step in enumerate(steps) if not isinstance(step, dict) or step_status(step) != "done"
    ]
    lines = [
        f"# Steps not done yet: {len(open_steps)} of {len(steps)}",
        f"{PLAN_FILE} has each step's what, verify and note; the steps not listed here are done.",
    ]
    listed = [_step_line(step, index) for index, step in open_steps]
    return lines + _packed(listed, STEP_LIST_BYTES, f"- and {{left}} more steps not done, which {PLAN_FILE} lists")


def build_plan_prompt(plan: dict, repos: list, worktrees: dict | None = None, autonomy=None) -> str:
    """The prompt of a plan run of ``plan`` over ``repos`` (the run's repos, each ``{"repo", "branch"}`` as the
    plan's ``repos`` names them), each checked out in the directory ``worktrees`` maps its name to (the repo's name,
    under the agent's directory, when it maps none): what the agent must and must not do, the repos, the plan's goal
    and its context, shortened, and a line for each step not done yet. The steps' what, verify and note stay in
    PLAN_FILE, which the daemon writes, so the prompt stays within MAX_PROMPT_BYTES however long the plan is. The
    autonomy card of the run (``autonomy``, ``autonomy_card.Card.spec``) opens it and says what the agent asks."""
    worktrees = worktrees or {}
    lines = [
        f"You are running the plan {_line(plan.get('id'), SHORT_BYTES, 'without an id')} from the evo-agents hub, as "
        "one plan run on a worker: do every step of it that is not done yet, in the order their depends_on allows, the "
        "steps ready at the same time together.",
        "",
        *_plan_rules(autonomy),
        "",
        "Repositories of this run, each in a worktree of its own under the current directory:",
        *_repo_lines(repos if isinstance(repos, list) else [], worktrees),
    ]
    for heading, value, budget in (
        ("# Goal of the plan", plan.get("goal"), GOAL_BYTES),
        ("# Context of the plan, shortened", plan.get("context"), CONTEXT_BYTES),
    ):
        if text_of(value):
            lines += ["", heading, clip(text_of(value), budget)]
    lines += ["", *_step_list(plan)]
    return clip(with_card("\n".join(lines) + "\n", autonomy), MAX_PROMPT_BYTES)


# The prompt of a step's session in a plan run with a session per step


def _step_session_rules(key: str, autonomy) -> list[str]:
    fewest, most = DECISION_OPTIONS
    return [
        "Rules for this session:",
        f"- Do step {key} and nothing else. This plan run starts a new agent session for each step, in the order "
        "their depends_on allows, so the steps after this one get sessions of their own: do not run the plan, with "
        "the execute-plan skill or otherwise.",
        f"- {PLAN_FILE} holds the whole plan as this run was claimed, and `evo-agents worker plan` prints it as the "
        "hub holds it now. The handoff at the end of this prompt says what the sessions and runs before this one did; "
        "read the plan for what it left out.",
        record_rule(key, "the step"),
        *decision_lines("the step", autonomy),
        "- To ask: `evo-agents worker ask --category CATEGORY --question TEXT --context-file FILE "
        f"--option KEY=LABEL[:DESCRIPTION] --recommended KEY --recommended-reason TEXT --step {key}`, with {fewest} "
        f"to {most} options. It prints the decision's id. Go on with the work of the step that does not depend on the "
        "answer; when none is left, end your turn. The answer comes back in this session as a message naming the "
        "decision's id, and the time you wait does not count toward the run's timeout.",
        ASK_RULE,
        WAIT_RULE.format(step=key),
        CREDENTIAL_RULE.format(step=key),
        RELAY_RULE,
        ATTACH_RULE,
        _push_rule(autonomy),
        OUTPUTS_RULE,
        f"- Keep {RESULT_DIR}/ out of your commits. When you stop, because the step is done or because you cannot go "
        f'on, write {RESULT_FILE} as a JSON object whose "summary" says what you did of step {key}, what you decided '
        "yourself, and what is left and why. The worker runs no verify command at the end of the session: "
        "`evo-agents worker step` ran the step's. When this session ends with the step not done, the plan run ends "
        "with it.",
    ]


def build_step_session_prompt(
    plan: dict, step: dict, repos: list, worktrees: dict | None = None, handoff: str | None = None, autonomy=None
) -> str:
    """The prompt of the session a plan run with a session per step gives ``step``, one of the steps of ``plan``,
    over ``repos`` checked out in ``worktrees`` (as ``build_plan_prompt`` takes them): what the agent must and must not
    do in that session, the repos, the plan's context, shortened, the step's what, verify and note as the prompt of a
    run of one step has them, and ``handoff``, the handoff of the plan for the step (``evo_agents.hub.handoff``), which
    holds the plan's goal and what the steps before it did. The whole within MAX_PROMPT_BYTES; the worker builds the
    handoff in the room the rest leaves it (``handoff_room``)."""
    index = next((position for position, other in enumerate(plan_steps(plan)) if other is step), 0)
    key = _short(step_key(step, index), "?")
    lines = [
        f"You are running step {key} of the plan {_line(plan.get('id'), SHORT_BYTES, 'without an id')} from the "
        "evo-agents hub, in an agent session of its own: this plan run gives each step not done yet a new session, "
        "with the handoff of the plan instead of the sessions before it.",
        "",
        *_step_session_rules(key, autonomy),
        "",
        "Repositories of this run, each in a worktree of its own under the current directory:",
        *_repo_lines(repos if isinstance(repos, list) else [], worktrees or {}),
    ]
    if text_of(plan.get("context")):
        lines += ["", "# Context of the plan, shortened", clip(text_of(plan.get("context")), CONTEXT_BYTES)]
    lines += _step_lines(step, key, plan, worker_how())
    if handoff and handoff.strip():
        lines += ["", handoff.strip()]
    return clip(with_card("\n".join(lines) + "\n", autonomy), MAX_PROMPT_BYTES)


def handoff_room(prompt: str) -> int:
    """The UTF-8 bytes a handoff may take in the prompt ``prompt``, built by build_step_session_prompt without one,
    so that the prompt with it stays within MAX_PROMPT_BYTES whole."""
    return max(0, MAX_PROMPT_BYTES - len(prompt.encode()) - 2)
