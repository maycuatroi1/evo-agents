"""The handles only the API makes (``Plugin``, ``Session``, ``Member``), which its functions take back and check they
made: a plugin cannot hand the API a member, a session or another plugin's handle it built itself through the API's
own names, and ``refusing``, which turns the hub's refusals into ``Refused``."""

from __future__ import annotations

import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from fastapi import HTTPException
from pydantic import ValidationError

from evo_agents.hub.server.core_plugin_api.models.types import Clearance, EgressSink, Member, Plugin, Refused, Session
from evo_agents.hub.server.security import Principal

_PLUGINS: dict[str, Plugin] = {}  # plugin id: the handle plugin() gave
_SESSIONS: weakref.WeakSet[Session] = weakref.WeakSet()
_ADMINS: weakref.WeakSet[Session] = weakref.WeakSet()
_MEMBERS: weakref.WeakSet[Member] = weakref.WeakSet()
_PRINCIPALS: weakref.WeakKeyDictionary[Session, Principal] = weakref.WeakKeyDictionary()  # as the hub checks them


def issue_plugin(plugin_id: str, kinds: tuple[str, ...], egress: tuple[EgressSink, ...] = ()) -> Plugin:
    """The handle of core plugin ``plugin_id``: the one given before while its kinds and sinks are the same."""
    known = _PLUGINS.get(plugin_id)
    if known is not None and known.kinds == kinds and known.egress == egress:
        return known
    made = _PLUGINS[plugin_id] = Plugin(plugin_id, kinds, egress)
    return made


def issue_session(
    user_id: int, login: str, token_id: int, web: bool, *, admin: bool = False, principal: Principal | None = None
) -> Session:
    machine = principal is not None and principal.kind == "machine"
    made = Session(user_id, login, token_id if token_id is not None else 0, web, machine)
    _SESSIONS.add(made)
    if admin:
        _ADMINS.add(made)
    if principal is not None:
        _PRINCIPALS[made] = principal
    return made


def session_principal(session: Session) -> Principal:
    """The session as the hub's checks take it: the credential it came from, or, for a session of before 1.3 the API
    made without one, a member without a token who is no hub admin."""
    found = _PRINCIPALS.get(session)
    if found is not None:
        return found
    return Principal(
        user_id=session.user_id,
        login=session.login,
        admin=session in _ADMINS,
        token_id=session.token_id or None,  # pyright: ignore[reportArgumentType]
        kind="web" if session.web else "machine",
        token_hash="",
    )


def issue_member(plugin: str, kind: str, user_id: int, login: str, channel_id: int, config: Mapping) -> Member:
    made = Member(user_id, login, plugin, kind, channel_id, MappingProxyType(dict(config)))
    _MEMBERS.add(made)
    return made


def own_plugin(plugin) -> Plugin:
    if not isinstance(plugin, Plugin) or _PLUGINS.get(plugin.id) is not plugin:
        raise ValueError("a Plugin comes from plugin(__name__) only")
    return plugin


def own_kind(plugin: Plugin, kind: str) -> str:
    if kind not in plugin.kinds:
        raise ValueError(
            f"{kind!r} is not a kind of channel plugin {plugin.id} contributes ({', '.join(plugin.kinds)})"
        )
    return kind


def own_session(session) -> Session:
    if not isinstance(session, Session) or session not in _SESSIONS:
        raise ValueError("a Session comes from WebSession, HubAdmin or redeem_link_code only")
    return session


def admin_session(session) -> Session:
    if not isinstance(session, Session) or session not in _ADMINS:
        raise ValueError("an unlink by an admin names the Session HubAdmin gave")
    return session


def own_member(member) -> Member:
    if not isinstance(member, Member) or member not in _MEMBERS:
        raise ValueError("a Member comes from find_member, link_channel or direct_chat_member only")
    return member


def own_clearance(cleared) -> Clearance:
    if not isinstance(cleared, Clearance) or not isinstance(cleared.below, bool):
        raise ValueError("a clearance is a Clearance(level, location, below=False)")
    return cleared


def plugin_kinds(plugin_id: str) -> tuple[str, ...]:
    known = _PLUGINS.get(plugin_id)
    return () if known is None else known.kinds


def principal_of(member: Member) -> Principal:
    """The member as the hub's checks take them: no token, of the channel's kind, and never a hub admin."""
    return Principal(
        user_id=member.user_id,
        login=member.login,
        admin=False,
        token_id=None,
        kind=member.kind,
        token_hash="",  # pyright: ignore[reportArgumentType]
    )


def _validation_text(exc: ValidationError) -> str:
    found = exc.errors()
    if not found:
        return str(exc)
    first = found[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg", "invalid"))


@contextmanager
def refusing() -> Iterator[None]:
    """The hub's refusals as ``Refused``: an HTTPException of a route's code with its status and words, and a body
    the route's model refuses as 422."""
    try:
        yield
    except Refused:
        raise
    except HTTPException as exc:
        raise Refused(exc.status_code, exc.detail) from None
    except ValidationError as exc:
        raise Refused(422, _validation_text(exc)) from None
