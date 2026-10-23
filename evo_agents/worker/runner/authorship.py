"""Who a run's commits name: the agent as author, the person at the origin of the run as co-author.

A chat run, under any profile, and a run under a profile that is not its project's default (the claim's
``own_home``) are the agent's work: the commits of such a run name the profile as their author, its name with the email
``<handle>@<host of the hub>`` (``agent_author``). The committer stays this machine's git identity. The commit the
daemon makes of what the agent left (``push.commit``) carries the trailer ``Co-authored-by: <login>
<<github_id>+<login>@users.noreply.github.com>`` of the person who dispatched the run (the claim's ``origin``;
``co_author_trailer``), and the agent's own commits in the run take the same author from GIT_AUTHOR_NAME and
GIT_AUTHOR_EMAIL in its environment (``author_env``). Any other run under the project's default profile, a run without
a profile and a run whose claim has none of this commit as before: no author, no trailer, no variable. The daemon never
rewrites a commit that is already there.

Every commit of a run with a principal or an agent (plan autonomy-levels, criterion a10) also carries the trailers
``Evo-Principal: <login>`` (the principal of the claim's autonomy card) and ``Evo-Agent: <handle>`` (the claim's
profile), never a runtime or a model (``run_trailers``): the daemon's commit in its message, the agent's own commits
through the ``commit-msg`` hook of a directory of hooks the run's git configuration names (``trailer_hooks``). That hook
adds them only to a commit of a checkout under the run's work tree, and every hook of the directory then runs the
checkout's own one, as git would have.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

NOREPLY = "users.noreply.github.com"
PRINCIPAL_TRAILER, AGENT_TRAILER = "Evo-Principal", "Evo-Agent"
_UNSAFE = re.compile(r"[^A-Za-z0-9._@-]")  # a login or a handle keeps to these; anything else is dropped
# The client hooks git runs; each one of the run's directory runs the checkout's own hook of that name.
HOOK_NAMES = (
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "pre-auto-gc",
    "post-rewrite",
    "reference-transaction",
    "push-to-checkout",
    "post-index-change",
)


@dataclass(frozen=True)
class Author:
    """The git identity a run's commits name as their author."""

    name: str
    email: str

    @property
    def flag(self) -> str:
        """The value of ``git commit --author``."""
        return f"{self.name} <{self.email}>"


def hub_url(run) -> str:
    """The URL of the hub the daemon of ``run`` works for; empty when it has none."""
    return str(getattr(getattr(run.daemon, "hub", None), "url", "") or "")


def agent_authored(spec: dict) -> bool:
    """Whether the run of claim ``spec`` is the agent's work: a chat run, or one under a profile that is not its
    project's default, with a profile in its claim."""
    return isinstance(spec.get("profile"), dict) and (spec.get("kind") == "chat" or bool(spec.get("own_home")))


def _host(hub_url: str) -> str:
    return (urlsplit(hub_url).hostname or "").lower()


def agent_author(spec: dict, hub_url: str) -> Author | None:
    """The author of the commits of the run of claim ``spec``, under the hub at ``hub_url``: its profile, named as it
    is with the email ``<handle>@<host of the hub>``; None for a run that is not the agent's work (``agent_authored``)
    or a hub URL without a host."""
    if not agent_authored(spec):
        return None
    profile = spec["profile"]
    handle = str(profile.get("handle") or "").strip()
    host = _host(hub_url)
    if not handle or not host:
        return None
    name = " ".join(str(profile.get("name") or "").split()) or handle
    return Author(name=name, email=f"{handle}@{host}")


def co_author_trailer(spec: dict) -> str | None:
    """The ``Co-authored-by`` trailer that names the person at the origin of the run of claim ``spec``, with their
    GitHub noreply email (``<github_id>+<login>@`` NOREPLY, or ``<login>@`` NOREPLY without an id); None for a run that
    is not the agent's work or a claim without an origin."""
    origin = spec.get("origin")
    if not agent_authored(spec) or not isinstance(origin, dict):
        return None
    login = str(origin.get("login") or "").strip()
    if not login:
        return None
    github_id = origin.get("github_id")
    local = f"{github_id}+{login}" if isinstance(github_id, int) and not isinstance(github_id, bool) else login
    return f"Co-authored-by: {login} <{local}@{NOREPLY}>"


def _clean(value) -> str:
    return _UNSAFE.sub("", str(value or ""))


def run_trailers(spec: dict) -> list[str]:
    """``Evo-Principal: <login>`` and ``Evo-Agent: <handle>`` of the run of claim ``spec``, each when the claim names
    it: the principal of its autonomy card, the handle of its profile."""
    card = spec.get("autonomy") if isinstance(spec.get("autonomy"), dict) else {}
    profile = spec.get("profile") if isinstance(spec.get("profile"), dict) else {}
    principal, agent = _clean(card.get("principal")), _clean(profile.get("handle"))
    found = [f"{PRINCIPAL_TRAILER}: {principal}"] if principal else []
    return found + ([f"{AGENT_TRAILER}: {agent}"] if agent else [])


def commit_message(spec: dict, subject: str) -> str:
    """The message of the daemon's commit of the run of claim ``spec``: ``subject``, and after a blank line the
    co-author trailer when the run has one and its trailers ``Evo-Principal`` and ``Evo-Agent``."""
    trailer = co_author_trailer(spec)
    trailers = ([trailer] if trailer else []) + run_trailers(spec)
    return subject + ("\n\n" + "\n".join(trailers) if trailers else "")


def hook_script(trailers: list[str], root: Path) -> str:
    """The text of each hook of the run's directory: in ``commit-msg`` of a checkout under ``root``, add ``trailers``
    (once each); then run the checkout's own hook of the same name, from its own configuration."""
    added = " ".join(f"--trailer {shlex.quote(line)}" for line in trailers)
    under = shlex.quote(str(root)) + "/*"
    return f"""#!/bin/sh
# The git hooks of an evo-agents run: a commit of its own checkouts names its principal and agent.
name=${{0##*/}}
top=$(git rev-parse --show-toplevel 2>/dev/null) || top=
if [ "$name" = commit-msg ] && [ -n "$top" ]; then
  case "$(cd "$top" && pwd -P)/" in
    {under}) git interpret-trailers --in-place --if-exists addIfDifferent {added} "$1" || : ;;
  esac
fi
own=$(GIT_CONFIG_COUNT=0 git rev-parse --git-path hooks 2>/dev/null) || exit 0
here=$(cd "${{0%/*}}" && pwd -P)
case "$own" in /*) ;; *) own="$(pwd -P)/$own" ;; esac
[ "$(cd "$own" 2>/dev/null && pwd -P)" = "$here" ] && exit 0
[ -x "$own/$name" ] && exec "$own/$name" "$@"
exit 0
"""


def trailer_hooks(spec: dict, directory: Path, root: Path) -> Path | None:
    """Write the directory of hooks of the run of claim ``spec`` at ``directory`` for the checkouts under ``root``,
    its work tree; the directory, for ``core.hooksPath`` of the agent's git, or None for a run without trailers."""
    trailers = run_trailers(spec)
    if not trailers:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    text = hook_script(trailers, Path(root).resolve())
    for name in HOOK_NAMES:
        path = directory / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
        path.chmod(0o755)
    return directory


def author_env(spec: dict, hub_url: str) -> dict[str, str]:
    """GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL of the agent's author for the environment of its agent; empty for a run
    that is not the agent's work, whose environment stays as it was."""
    author = agent_author(spec, hub_url)
    if author is None:
        return {}
    return {"GIT_AUTHOR_NAME": author.name, "GIT_AUTHOR_EMAIL": author.email}
