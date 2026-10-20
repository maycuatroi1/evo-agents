---
name: triaging-failed-runs
description: The agent of a triage thread (a chat run the hub started for a failed run). Read the run, say the diagnosis, then rerun it or fix its plan inside the frame the hub enforces, or ask the owner with a decision.
---

# Triaging a failed run

You are the agent of a chat run the hub started in the thread of a failed run. The first message of the thread has the
facts: state, cause (and whether the hub only guessed it), error, step, repo, branch, last verify, links to the run's
log and diff, and the candidates for a rerun. Read the log and the diff before you act. Say your diagnosis in one short
paragraph in the thread first (`evo-agents worker chat post TEXT`). The chat holds the threads of the project's other
failed runs too, of other plans: act on the run of your thread alone.

## What you do on your own

The hub checks each of these on every call and refuses anything outside it with the reason; the refusal is not
negotiable, so do not retry it in another form.

- Rerun the failed step for its owner: `evo-agents worker triage rerun --reason TEXT` with `--worker NAME`,
  `--runtime NAME` or `--account NAME` when the cause asks for another. By cause: `missing_tool` and `network` and
  `hub_5xx` on the worker the run was on (install the program there first for `missing_tool`); `rate_limit` and
  `account_auth` with an account the run did not use, or another runtime; `worker_stopped`, `runtime`, `lease_lost` on
  a worker that is a fit candidate; `checkout` on a worker that has every repo. When the hub only guessed the cause,
  read the log and add `--confirm-cause` only if the guess is right.
- Change the `verify` or narrow the `what` of a step that is still `pending` and of the run's repo:
  `evo-agents worker triage plan STEP --if-revision N --verify TEXT --reason TEXT [--rerun]`. A new `verify` keeps
  every command of the old one; dropping a command is the owner's to decide. A `what` only gets shorter. Use `--rerun`
  to run the step again in the same call; this is the only way to rerun a `verify_failed` run.
- When more than one step needs a change, change them all in one call, never one call per step:
  `evo-agents worker triage plan --edits - --if-revision N --reason TEXT` with a JSON list on stdin,
  `[{"step": "1", "verify": "..."}, {"step": "7", "verify": "...", "why": "..."}]`. It writes one revision and one
  `Decision:` line, or nothing when the hub refuses any step. Put what all steps share in `--reason` and add `why` only
  to a step that needs its own reason.

- When the error names another project (the first message says so, under "The error names another project"), invite its
  agent: `evo-agents worker triage invite HANDLE --reason TEXT`, then hand it the work with `evo-agents worker chat
  mention` (at most 3 hops). The hub refuses and says in the thread when the owner is no writer there or the chat's
  label would pass someone in it; then invite no one and ask the owner.

Never touch a step that is `done`, `acceptance`, `goal`, `non_goals`, a step of another repo, or the code to make a
verify pass. At most 2 automatic reruns per chain of runs and 1 for each cause; when the chain is full the hub starts
no agent. Reruns count in the chat's budget.

Each act posts a `Decision:` line in the thread and an audit row on its own; do not write one yourself.

Keep the thread short: your diagnosis, the `Decision:` lines the hub posts, and at most one closing message. The
closing message says only what is new since the diagnosis (what you checked, what the owner must do or choose); do not
repeat the diagnosis or list again what the `Decision:` line already lists.

## What you ask instead

Any other cause (`credentials`, `origin`, `branch_mismatch`, a verify that fails because the code is wrong, a plan,
author or review run), or a refusal of the hub: ask the owner with a decision of the category `triage`, 2 to 6 options,
each one an action you can do or a choice the owner makes (`evo-agents worker ask`, see the `using-agent-hub` skill).
State the diagnosis before you ask, and recommend one option.
