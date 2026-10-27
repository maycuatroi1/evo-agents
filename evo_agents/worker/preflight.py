"""What a run checks once it holds its leases, before it fetches anything or starts its agent (``docs/workers.md``,
Preflight and failure causes).

Each check that does not pass is a ``Problem`` with its cause, one of ``evo_agents.hub.runs.PREFLIGHT_CAUSES``, and a
message that names the repo or the program; the run (``evo_agents.worker.run``) fails with all of them, the cause of the
first, before its agent starts, and the hub queues no next attempt of a failed run.

- ``origin``: the project lists no origin for a repo the run needs, as the hub's answer for the run's leases says (a
  repo it names missing with no origin): no credential can be leased for it.
- ``credentials``: git cannot read a repo of the run from its origin, or cannot push to it when the run pushes that
  repo, with what the run holds: its leases, else this machine's own credentials. ``can_reach`` asks the remote
  itself, as cheaply as git can: ``git ls-remote origin HEAD`` to read, ``git push --dry-run`` of the checkout's HEAD to
  a branch no run pushes to push, which sends nothing. Only a refusal for want of a credential (``refused``) fails the
  run; a remote that does not answer, or any other trouble, is the fetch's to report. An origin that is a path on this
  machine takes no credential and is not asked. For a repo on github.com whose branch the run's agent lands through a
  pull request it opens itself (``runner.access.opens_pull``), no credential the agent has can open one there:
  ``pull_refusal`` asks GitHub with each token, the one gh takes in the agent's environment (``gh_token``) and the
  run's git lease. A user's token (gh's login, a personal access token) is asked for the repo, ``GET
  /repos/{owner}/{repo}``, read only: its user may push there (``permissions.push``), and a classic or OAuth token has
  the scope repo (public_repo for a public repo, X-OAuth-Scopes). No route shows an installation token of a GitHub App
  (``ghs_``) its own permissions, and its ``permissions`` on the repo read all false, so it is asked to open a pull
  request with an empty body: GitHub answers 403 when the token lacks pull_requests: write and 422 when it has it, and
  opens nothing. Only a clear refusal counts (401, 403, 404, no push, a scope or a permission missing); GitHub not
  answering, or answering anything else, is ``Unclear`` and fails nothing.
- ``missing_tool``: a program a verify command calls (``evo_agents.hub.judge.programs``) is not on the run's PATH, or
  the verify is not runnable as a shell command at all (``evo_agents.hub.verify_words.shell_problem``: a quote left
  open, or prose whose words are no command names), which says so and lists those words instead of blaming the PATH.
  Each message names the step the verify belongs to when the run knows it. A judge run checks the project's hidden
  checks the same way, and names a hidden check by its place alone, never its command or its program.
- ``environment``: the run's claim names a plugin's environment, a container, that this worker cannot run the agent
  in: no container engine, one that does not answer, an image it cannot pull, a kind or mode of run a container does
  not take (``evo_agents.worker.environments.Container.preflight``, which ``runner.access`` asks).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents import __version__
from evo_agents.hub.credentials import normalize_origin
from evo_agents.hub.judge import programs
from evo_agents.hub.verify_words import COMMAND_NAME, shell_problem
from evo_agents.worker import gitops

PROBE_TIMEOUT = 60.0  # seconds git has to answer one question to a remote
GITHUB_API_URL = "https://api.github.com"
GITHUB_TIMEOUT = 20.0  # seconds GitHub has to answer one call of the pull request check
GH_TIMEOUT = 10.0  # seconds `gh auth token` has to answer
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")  # what gh takes for github.com before its own login, in its order
INSTALLATION_TOKEN = "ghs_"  # how an installation token of a GitHub App starts
PULL_PERMISSION = "pull_requests: write"
PROBE_BRANCH = "refs/heads/evo-run/preflight-{id}"  # where a dry-run push would go: a branch nothing pushes to
# What git prints when the remote refused it for want of a credential, or a credential it does not take: gitops'
# AUTH_FAILURE, SSH without a key the host takes, a token that may not push, and a repo the credential does not see.
REFUSED = re.compile(
    rf"{gitops.AUTH_FAILURE.pattern}|terminal prompts disabled|Permission denied \(publickey|Permission to \S+ denied"
    r"|marked as read only|Repository not found|repository '[^']*' not found|returned error: 403|HTTP 403",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Problem:
    """One check that did not pass: its cause (``runs.PREFLIGHT_CAUSES``) and what it says, naming the repo or the
    program."""

    cause: str
    message: str


def is_local(url: str) -> bool:
    """Whether ``url`` names a repository on this machine (a path, or file://), which takes no credential."""
    return not normalize_origin(url).startswith("https://")


def refused(text: str) -> bool:
    """Whether git's message says the remote refused it for want of a credential."""
    return bool(REFUSED.search(text or ""))


async def can_reach(checkout: Path, run_id: int, *, push: bool, env: Mapping[str, str]) -> tuple[str, str] | None:
    """None when git can read origin from ``checkout`` (and push to it, with ``push``) with ``env``, or when the remote
    gave no clear answer; else (``read`` or ``push``, the end of git's message), the remote having refused it for want
    of a credential."""
    questions = [("read", ("ls-remote", "--quiet", "origin", "HEAD"))]
    if push:
        target = PROBE_BRANCH.format(id=run_id)
        questions.append(("push", ("push", "--dry-run", "--no-verify", "--quiet", "origin", f"HEAD:{target}")))
    for what, question in questions:
        try:
            code, out, err = await gitops.git(checkout, *question, env=env, check=False, timeout=PROBE_TIMEOUT)
        except gitops.GitError:  # no answer in time: the fetch will say what is wrong
            return None
        said = err or out
        if code != 0 and refused(said):
            return what, _tail(said)
    return None


# Pull requests on GitHub


class Unclear(Exception):
    """gh or GitHub gave no clear answer to the pull request check: the run goes on, with a note saying so."""


async def gh_token(env: Mapping[str, str], *, login: bool = True) -> tuple[str | None, str]:
    """The token gh takes for github.com with ``env``, and where it comes from: GH_TOKEN, else GITHUB_TOKEN, else (with
    ``login``) gh's own login on this machine, ``gh``; (None, why gh has none) without one. Unclear when gh did not
    answer. The token never leaves this function but in its result."""
    for name in TOKEN_VARIABLES:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    unset = "neither GH_TOKEN nor GITHUB_TOKEN is set"
    if not login:
        return None, f"{unset}, and the agent's gh sees no login of this machine (its config dir is its own)"
    gh = shutil.which("gh", path=env.get("PATH") or os.defpath)
    if gh is None:
        return None, f"{unset}, and gh is not on the run's PATH"
    try:
        process = await asyncio.create_subprocess_exec(
            gh,
            "auth",
            "token",
            "--hostname",
            "github.com",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**env, "GH_PROMPT_DISABLED": "1"},
        )
    except OSError as exc:
        raise Unclear(f"gh did not start: {exc}") from None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), GH_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise Unclear(f"`gh auth token` did not answer within {GH_TIMEOUT:g}s") from None
    value = out.decode("utf-8", errors="replace").strip()
    if process.returncode == 0 and value:
        return value, "gh"
    return None, f"{unset}, and gh on this worker is not signed in to github.com"


async def pull_refusal(owner: str, repo: str, token: str) -> str | None:
    """What keeps ``token`` from opening a pull request in ``owner/repo`` on GitHub, as a message goes on after the
    token's name; None when it can. Unclear when GitHub gave no clear answer. An installation token of a GitHub App is
    asked to open one with an empty body, which GitHub refuses with 403 for want of pull_requests: write and with 422
    when the token has it; any other token is asked for the repo, read only (see the module's docstring)."""
    where = f"{owner}/{repo}"
    if token.startswith(INSTALLATION_TOKEN):
        status, headers, body = await github_call("POST", f"/repos/{owner}/{repo}/pulls", token, {})
        if status == 422:
            return None
        if status == 403 and not _rate_limited(headers, body):
            needed = headers.get("x-accepted-github-permissions") or "pull_requests=write"
            return f"lacks {PULL_PERMISSION} on {where} (GitHub answered 403: {_said(body)}; it accepts {needed})"
        return _refusal(status, headers, body, where)
    status, headers, body = await github_call("GET", f"/repos/{owner}/{repo}", token)
    if status != 200:
        return _refusal(status, headers, body, where)
    if not isinstance(body, dict):
        raise Unclear(f"GitHub's answer for {where} is not a repository")
    scopes = headers.get("x-oauth-scopes")
    if scopes is not None:  # a classic or OAuth token, gh's login among them; a fine-grained one has no scopes
        held = {scope.strip() for scope in scopes.split(",") if scope.strip()}
        needed = "repo" if body.get("private") is not False else "public_repo"
        if "repo" not in held and needed not in held:
            return f"lacks the scope {needed} (it has {', '.join(sorted(held)) or 'none'})"
    permissions = body.get("permissions")
    if not isinstance(permissions, dict):
        raise Unclear(f"GitHub did not say what the token's user may do in {where}")
    if permissions.get("push") is not True:
        return f"belongs to a user who may not push to {where} (GitHub's permissions.push is false)"
    return None


def _refusal(status: int, headers: Mapping[str, str], body, where: str) -> str:
    """What a refusal of GitHub says the token lacks; Unclear for any answer that is not one."""
    if status == 401:
        return f"is not valid (GitHub answered 401: {_said(body)})"
    if status == 404:
        return f"does not see {where} (GitHub answered 404: {_said(body)})"
    if status == 403 and not _rate_limited(headers, body):
        return f"is refused {where} (GitHub answered 403: {_said(body)})"
    raise Unclear(f"GitHub answered {status} for {where}: {_said(body)}")


def _rate_limited(headers: Mapping[str, str], body) -> bool:
    return headers.get("x-ratelimit-remaining") == "0" or "rate limit" in _said(body).lower()


def _said(body) -> str:
    message = body.get("message") if isinstance(body, dict) else None
    return _tail(message, 200) if isinstance(message, str) and message.strip() else "no message"


async def github_call(
    method: str, path: str, token: str, body: dict | None = None
) -> tuple[int, dict[str, str], object]:
    """GitHub's answer to one call of its API at ``path`` with ``token``: the status, the headers (names in lower case)
    and the body read as JSON (None when it is not); Unclear when GitHub did not answer. The token goes only into the
    Authorization header of a request to GITHUB_API_URL, and a redirect is not followed."""
    return await asyncio.to_thread(_github_call, method, path, token, body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the Authorization header must not follow a redirect to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


def _github_call(method: str, path: str, token: str, body: dict | None) -> tuple[int, dict[str, str], object]:
    request = urllib.request.Request(
        f"{GITHUB_API_URL}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"evo-agents/{__version__}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with _OPENER.open(request, timeout=GITHUB_TIMEOUT) as response:
            status, headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, exc.headers
        try:
            raw = exc.read()
        except OSError:
            raw = b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise Unclear(f"GitHub did not answer: {reason}") from None
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        payload = None
    return status, {name.lower(): value for name, value in (headers or {}).items()}, payload


def missing_programs(commands: Iterable[str], path: str | None) -> list[tuple[str, str]]:
    """(program, command) for each program ``commands`` call (``judge.programs``) that is not on ``path`` (the run's
    PATH; the shell's default one when the run has none), in order, each program once; an absolute path counts when it
    is an executable file."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    search = path if path is not None else os.defpath
    for command in commands:
        for program in programs(command):
            if program in seen:
                continue
            seen.add(program)
            if program.startswith("/"):
                present = os.path.isfile(program) and os.access(program, os.X_OK)
            else:
                present = shutil.which(program, path=search) is not None
            if not present:
                found.append((program, command))
    return found


def verify_problems(
    commands: Iterable[str | tuple[str | None, str]], path: str | None, *, what: str = "verify command"
) -> list[Problem]:
    """A ``missing_tool`` problem for each verify of ``commands``, a command or a (step key, command) pair, that is not
    runnable as a shell command (``shell_problem``), naming the words that are no programs and any program it also
    calls that ``path`` lacks; then one for each program of the others not on ``path``, each program once. Each names
    the command, after "step <key>: " when its step is known."""
    found: list[Problem] = []
    seen: set[str] = set()
    for item in commands:
        key, command = item if isinstance(item, tuple) else (None, item)
        where = f"step {key}: " if key not in (None, "") else ""
        said = f"{where}{what} `{_cut(command, 200)}`"
        missing = [program for program, _ in missing_programs([command], path)]
        unrunnable = shell_problem(command)
        if unrunnable is not None:
            message = f"{said} is not runnable as a shell command: it {unrunnable}"
            absent = [program for program in missing if COMMAND_NAME.fullmatch(program.rsplit("/", 1)[-1])]
            if absent:
                named = ", ".join(f"`{program}`" for program in absent)
                message += f"; it also runs {named}, which this worker does not have"
            found.append(Problem("missing_tool", f"{message}: rewrite the verify as a command"))
            continue
        for program in missing:
            if program not in seen:
                seen.add(program)
                found.append(Problem("missing_tool", f"{said} calls {program}, which is not on this worker's PATH"))
    return found


def hidden_problems(checks: list[str], path: str | None) -> list[Problem]:
    """A ``missing_tool`` problem for each hidden check that calls a program not on ``path``, naming the check by its
    place alone: neither its command nor its program leaves the daemon's memory."""
    return [
        Problem(
            "missing_tool",
            f"hidden check {index} of {len(checks)} calls a program that is not on this worker's PATH",
        )
        for index, check in enumerate(checks, start=1)
        if missing_programs([check], path)
    ]


def summary(problems: list[Problem]) -> str:
    """The error of a run its preflight failed: each problem, the agent not started."""
    said = "; ".join(problem.message for problem in problems)
    return f"preflight: {said}; the agent did not start"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tail(text: str, limit: int = 300) -> str:
    """git's message on one line, its end when it is long."""
    text = " ".join(text.split())
    return text if len(text) <= limit else "..." + text[-limit:]
