# The browser of this run

This run has a Chromium of its own on the worker. The tools of the `playwright` MCP server drive it: it is attached to
that browser, so every page you open lives there, and nothing else on this machine is touched.

## Signing in

When a page asks you to sign in (a login form, a single sign-on redirect, a "verify it is you" screen):

1. Do not type a password, a code or a recovery phrase yourself, and do not try to solve a captcha or a
   two-factor challenge. You have none of those; the person who dispatched this run does.
2. Run `evo-agents worker browser login --identity NAME --url URL --reason "WHY"`. `NAME` is the name (or the number) of
   a browser identity this run was dispatched with; `URL` is the page to sign in at;
   `WHY` says in one sentence what you need the session for.
3. The command asks the person a decision and prints its number. The run waits for them, with its browser kept open.
   They sign in through the live view of this run, on a phone as well as a computer, and answer Done; or they answer
   Skip.
4. End your turn, as the command says. The answer comes back to you as a message naming the decision.
   - Done: the browser holds the session now. Reload the page you were on and go on. If a browser tool says the
     browser closed, call `browser_close` once and use the tool again.
   - Skip: the person will not sign in. Do the part of the task that needs no login, and say in your result what you
     could not do.

If the same login page shows again after Done, ask once more with a reason that says what went wrong; do not ask in a
loop.

A run of one step has nobody to ask. There the command marks the identity as needing a new login and fails the run, with
the reason in its log, so that the person signs in first and runs the step again.

## Reading the session

`evo-agents worker browser state --out FILE` writes the browser's cookies as a Playwright storageState
file (local storage is not in it), for a script of yours that needs the session. A relative FILE lies in the run's own directory; a path in a
worktree, or outside the run's directory, is refused: it holds secrets that no commit may carry. Delete it when you are done.
