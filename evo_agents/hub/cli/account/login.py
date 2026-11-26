"""``evo-agents hub account login`` and ``oauth-login``: a Claude account added, or its token renewed, without the token
ever on the screen (``docs/credentials.md``, section "Accounts").

``account login NAME`` adds a Claude account, or renews the token of one: it runs ``claude setup-token`` unchanged on
the member's machine (``evo_agents.hub.setup_token``), where the member approves in the browser, takes the token it
prints off its screen, and writes it with PUT /v1/secrets/{name} as an env secret for CLAUDE_CODE_OAUTH_TOKEN, ending a
year from today less a day unless ``--expires`` says otherwise. With ``--paste``, when stdin is not a terminal, or
when the screen held no token, the token is read as ``secret set`` reads a value. An API key of the Anthropic Console
is refused, pointing at ANTHROPIC_API_KEY. Renewing an account sends its projects and workers again unless a flag
gives others; PUT keeps the id of the secret, so its weight, priority, pause, cooldown and runs stay. Every check that
needs no token (the name, the day, the projects' writer role, the workers) comes before ``claude setup-token`` runs,
so a token it makes is not lost to a refusal.

``account oauth-login NAME`` does the same for an account of Claude's OAuth sign-in (``evo_agents.hub.claude_oauth``):
the member approves on claude.ai, the command trades the code for an access token, a refresh token and the end of the
access token, in memory alone, and writes them with PUT /v1/secrets/{name}: the access token as the value, the rest as
``oauth``, which the hub seals with it. ``account list`` and ``show`` tell such an account by ``auth``: oauth, where an
account of a pasted or ``claude setup-token`` token is token. Running it again for a NAME that exists replaces the
tokens alone, as ``login`` does, with the same secret id.

The commands are :class:`AccountLogin` and :class:`AccountOAuthLogin`, classes of ``evo_agents.clikit``. They talk to
the member while they run, on stderr (what they do, the address to open), before the token or the code is read, so
those lines are written as they come; what they found is their :class:`evo_agents.clikit.Result`. Standard library
only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import webbrowser
from datetime import UTC, datetime

from evo_agents.clikit import Arg, Result, Text, UsageError, Views, returns_object
from evo_agents.hub import claude_oauth, setup_token
from evo_agents.hub.access import has_role
from evo_agents.hub.cli.account.views import ACCOUNT_KEYS, account_lines
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext, when
from evo_agents.hub.cli.secret.bindings import bound_to, pool_arg, where_text
from evo_agents.hub.cli.secret.resources import Accounts, Principal, Secrets
from evo_agents.hub.cli.secret.values import Refused, check_name, check_value, expires_at, read_value
from evo_agents.hub.client import Hub

NO_CLAUDE = (
    "there is no `claude` on PATH to run `claude setup-token` with. Install Claude Code "
    "(https://code.claude.com/docs/en/setup), or run `claude setup-token` where it is installed and give the token "
    "it prints to `evo-agents hub account login {name} --paste`, which asks for it without echo, or on stdin, such "
    "as `pbpaste | evo-agents hub account login {name}`"
)
NO_TOKEN = (
    "no token: run the command in a terminal, where it runs `claude setup-token` for you or asks for the token, or "
    "pipe the token on stdin, such as `pbpaste | evo-agents hub account login NAME`"
)
API_KEY = (
    "that is an API key of the Anthropic Console (sk-ant-api...), not a token of `claude setup-token`: an API key "
    "goes in ANTHROPIC_API_KEY, with `evo-agents hub secret set NAME --kind env --env-var ANTHROPIC_API_KEY "
    "--project PROJECT`"
)
OAUTH_PASTE = (
    "no code: pipe the code claude.ai shows after you approve, such as `pbpaste | evo-agents hub account oauth-login "
    "NAME`, or run the command in a terminal"
)


class NoClaude(Exception):
    """There is no ``claude`` on PATH to run ``claude setup-token`` with; ``str`` says what to do instead."""


def say(text: str) -> None:
    """A line for the member at the terminal, on stderr, so stdout holds the account alone."""
    print(text, file=sys.stderr)


def at_terminal(args: argparse.Namespace) -> bool:
    """Whether the member is at a terminal and did not ask to paste."""
    return not args.paste and sys.stdin is not None and sys.stdin.isatty()


def login_ends(day: str | None, now: datetime) -> str:
    """When the account ends, in ISO 8601: 00:00 UTC of ``day`` (YYYY-MM-DD), which is to come, or of a year from
    today (UTC) less a day when there is none. Refused for a day that is not one, or is past."""
    if day is None:
        return expires_at(setup_token.default_end(now.date()).isoformat())
    ends = expires_at(day)
    if datetime.fromisoformat(ends) <= now:
        raise Refused(f"--expires {day} is past already: an account ends on a day to come")
    return ends


def login_body(args, existing: dict | None, ends: str) -> dict:
    """What PUT /v1/secrets/{name} gets for the account, but its token: the projects and workers given, else those of
    the account when it is one already. Refused for a secret of that name that is not an account for
    CLAUDE_CODE_OAUTH_TOKEN, and for a new account without a project."""
    var = setup_token.ENV_VAR
    if existing is not None and (existing["kind"], existing["env_var"]) != ("env", var):
        what = f"sets {existing['env_var']}" if existing["kind"] == "env" else f"is of kind {existing['kind']}"
        raise Refused(f"your secret {args.name} {what}, not {var}: give the Claude account another name")
    projects = args.project or (existing["projects"] if existing else [])
    if not projects:
        raise Refused("a new account needs --project, a project whose runs take it; repeat it for several")
    try:
        bound = bound_to(args, existing)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    return {"kind": "env", "env_var": var, "projects": projects, **bound, "expires_at": ends}


def check_bindings(hub: Hub, body: dict) -> None:
    """Refused for a project of ``body`` the caller does not hold the writer role on, or a worker that is not one of
    theirs, as PUT /v1/secrets would refuse them: asked before ``claude setup-token`` makes a token, so none is made
    to be lost."""
    principal = Principal(hub)
    me = principal.whoami()
    roles = {grant["project"]: grant["role"] for grant in me["grants"]}
    for project in body["projects"]:
        if not has_role(roles.get(project), "writer"):
            held = roles.get(project) or "no role"
            raise Refused(f"an account for project {project} needs the writer role on it; you hold {held}")
    if body["workers"]:
        mine = {w["name"].lower() for w in principal.workers() if w["owner"] == me["login"]}
        for worker in body["workers"]:
            if worker.lower() not in mine:
                raise Refused(f"{worker} is not a worker of yours that is not revoked: see GET /v1/workers")
    if body["pools"]:
        pools = {p["name"].lower() for p in principal.pools()}
        for pool in body["pools"]:
            if pool.lower() not in pools:
                raise Refused(f"{pool} is not a worker pool of yours: see GET /v1/worker-pools")


def login_token(value: str) -> str:
    """The token as given, without the spaces and line breaks around it; Refused for an API key, or for what cannot
    be one token."""
    token = value.strip()
    if not token:
        raise Refused(NO_TOKEN)
    if setup_token.is_api_key(token):
        raise Refused(API_KEY)
    if any(char.isspace() for char in token):
        raise Refused("the token holds a space or a line break: give it as one line")
    check_value("env", token)
    return token


def login_intro(name: str, existing: dict | None, body: dict) -> str:
    projects = ", ".join(body["projects"])
    where = where_text(body)
    ends = f"{when(body['expires_at'])} UTC"
    if existing is None:
        return f"Adding account {name} ({body['env_var']}) for project(s) {projects}, {where}; it ends {ends}."
    return (
        f"{name} is an account of yours already: this replaces its token, which ends {ends}, for project(s) "
        f"{projects}, {where}. Its weight, priority, pause, cooldown and runs stay as they are."
    )


def login_value(name: str, claude: str | None) -> str:
    """The token: what ``claude setup-token`` printed when ``claude`` runs it and its screen held one, else read
    from stdin or asked for without echo."""
    if claude is not None:
        say(
            "Running `claude setup-token`: sign in to Claude in the browser it opens. The token it prints goes to the "
            "hub sealed, and is not shown here."
        )
        token = setup_token.run(claude)
        if token is not None:
            say("Took the token `claude setup-token` printed.")
            return token
        say("Found no token in what `claude setup-token` printed: paste it, or press Enter to stop.")
    prompt = f"Token of `claude setup-token` for account {name} (not shown): "
    return read_value(name, prompt=prompt, missing=NO_TOKEN)


def oauth_code(name: str, pkce: claude_oauth.Pkce, args: argparse.Namespace) -> str:
    """The authorization code: from the browser's answer on localhost when a terminal and a browser are here, else
    pasted (a code, ``code#state``, or the address the browser went to)."""
    url = claude_oauth.authorize_url(pkce)
    if at_terminal(args):
        say(f"Sign in to Claude in the browser, then come back here. If it does not open, go to:\n\n{url}\n")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                pass
        try:
            return claude_oauth.wait_for_callback(pkce.state)
        except claude_oauth.ListenError as exc:
            say(f"{exc}: paste the code claude.ai shows, or the address the browser went to.")
    else:
        say(f"Open this address, approve, and give the code claude.ai shows:\n\n{url}\n")
    pasted = read_value(name, prompt=f"Code for account {name} (not shown): ", missing=OAUTH_PASTE)
    return claude_oauth.parse_code(pasted, pkce.state)


def oauth_fields(tokens: claude_oauth.Tokens) -> dict:
    """The ``oauth`` of PUT /v1/secrets/{name}: what the hub seals with the access token."""
    return {
        "refresh_token": tokens.refresh_token,
        "access_expires_at": tokens.expires_at.isoformat(),
        "account_uuid": tokens.account_uuid,
        "organization_uuid": tokens.organization_uuid,
        "email": tokens.email,
    }


NAME = Arg("name", metavar="NAME", help="1 to 64 of a-z, 0-9, '.', '_' and '-'; an account's to renew it")
PROJECTS = Arg(
    "--project",
    action="append",
    metavar="PROJECT",
    help="a project whose runs take it, on which you hold writer; repeat it for several (a new account needs one; "
    "renewing keeps the account's projects)",
)
WORKERS = Arg(
    "--worker",
    action="append",
    metavar="NAME",
    help="only runs on this worker of yours take it; repeat it for several (default: any worker of yours; "
    "renewing keeps the account's workers)",
)
ACCOUNT_OUTPUT = returns_object(*ACCOUNT_KEYS, schema="Account")


class AccountLogin(HubCommand):
    name = "login"
    help = (
        "add a Claude account, or renew its token: runs `claude setup-token` and puts the token it prints on the "
        "hub, never on the screen"
    )
    arguments = (
        NAME,
        PROJECTS,
        WORKERS,
        pool_arg("take"),
        Arg(
            "--expires",
            metavar="YYYY-MM-DD",
            help="no run takes it from 00:00 UTC of this day on (default: a year from today, less a day)",
        ),
        Arg(
            "--paste",
            action="store_true",
            help="do not run `claude setup-token`: ask for the token without echo, or read it from stdin",
        ),
    )
    output = ACCOUNT_OUTPUT

    def check(self, args: argparse.Namespace) -> None:
        check_name(args.name)
        login_ends(args.expires, datetime.now(UTC))

    def run(self, ctx: HubContext) -> Result:
        name, ends = ctx.args.name, login_ends(ctx.args.expires, datetime.now(UTC))
        secrets = Secrets.of(ctx)
        claude = None
        if at_terminal(ctx.args):
            claude = shutil.which("claude")
            if claude is None:
                raise NoClaude(NO_CLAUDE.format(name=name))
        existing = secrets.find(name)
        body = login_body(ctx.args, existing, ends)
        check_bindings(ctx.hub, body)
        say(login_intro(name, existing, body))
        token = login_token(login_value(name, claude))
        written = secrets.put(name, {**body, "value": token})
        account = Accounts.of(ctx).get(name)
        if ctx.args.json:
            return Result(account)
        if written["created"]:
            said = f"Created account {name}. The hub keeps its token sealed and never shows it again."
        else:
            said = (
                f"Replaced the token of account {name}; its weight, priority, pause, cooldown and runs stay. The hub "
                "keeps the token sealed and never shows it again."
            )
        return Result(account, Views(Text(said), Text(*account_lines(account))))

    def failed(self, exc: Exception) -> int | None:
        if isinstance(exc, NoClaude):
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED
        return super().failed(exc)


class AccountOAuthLogin(HubCommand):
    name = "oauth-login"
    help = (
        "add a Claude account, or renew its tokens, by Claude's OAuth sign-in in your browser: the tokens go to "
        "the hub sealed, never on the screen"
    )
    arguments = (
        NAME,
        PROJECTS,
        WORKERS,
        pool_arg("take"),
        Arg("--no-browser", action="store_true", help="print the address to open instead of opening it"),
        Arg(
            "--paste",
            action="store_true",
            help="do not listen for the browser: ask for the code claude.ai shows without echo, or read it from stdin",
        ),
    )
    output = ACCOUNT_OUTPUT

    def check(self, args: argparse.Namespace) -> None:
        check_name(args.name)

    def run(self, ctx: HubContext) -> Result:
        name, secrets = ctx.args.name, Secrets.of(ctx)
        existing = secrets.find(name)
        body = login_body(ctx.args, existing, "")
        body.pop("expires_at")  # the end of the access token, which the tokens carry
        check_bindings(ctx.hub, body)
        projects = ", ".join(body["projects"])
        verb = "Adding" if existing is None else "Replacing the tokens of"
        say(f"{verb} account {name} ({body['env_var']}, OAuth) for project(s) {projects}, {where_text(body)}.")
        try:
            pkce = claude_oauth.new_pkce()
            tokens = claude_oauth.exchange(oauth_code(name, pkce, ctx.args), pkce)
        except claude_oauth.OAuthError as exc:
            raise UsageError(str(exc)) from None
        written = secrets.put(name, {**body, "value": tokens.access_token, "oauth": oauth_fields(tokens)})
        account = Accounts.of(ctx).get(name)
        if ctx.args.json:
            return Result(account)
        if written["created"]:
            said = f"Created account {name} (OAuth). The hub keeps its tokens sealed and never shows them again."
        else:
            said = (
                f"Replaced the tokens of account {name}; its weight, priority, pause, cooldown and runs stay. The hub "
                "keeps the tokens sealed and never shows them again."
            )
        return Result(account, Views(Text(said), Text(*account_lines(account))))


ACCOUNT_LOGIN = AccountLogin()
ACCOUNT_OAUTH_LOGIN = AccountOAuthLogin()
