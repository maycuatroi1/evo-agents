"""The base URL of a provider account, checked: the hub stores and calls only an https root on the public internet.

``base_url`` normalizes and checks the text a member gave: https, a host, an optional port and path, no user,
password, query or fragment, and a host that is not a name of a private network: a single label (``postgres``,
``hub``, the names of the hub's own compose network), ``localhost`` or a name under one of PRIVATE_SUFFIXES.
``public_addresses`` resolves the host and refuses it unless every address it resolves to is public
(``address_refusal``): no loopback, private (RFC 1918), link-local and the metadata address 169.254.169.254, CGNAT
(100.64.0.0/10), multicast, reserved or unspecified, IPv6 unique-local (fc00::/7) or an IPv4 address mapped into IPv6
that is any of these. The hub checks both when a member writes the URL and again before each call, as DNS may change
after the write; the call goes to the address checked (``evo_agents.hub.server.provider_probe``), so a second answer
of the resolver cannot send it elsewhere.

No message repeats more of the URL than its host, which the member typed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from evo_agents.hub.provider_accounts import MAX_BASE_URL_CHARS

PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".private")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
CGNAT = ipaddress.ip_network("100.64.0.0/10")
Resolver = Callable[[str, int], Awaitable[list[str]]]


class EndpointRefused(ValueError):
    """A base URL the hub does not store or call; the message says why, without the URL's path."""


def base_url(text: str) -> str:
    """``text`` as https://host[:port][/path], host in lower case, no trailing slash; EndpointRefused otherwise."""
    text = text.strip()
    if len(text) > MAX_BASE_URL_CHARS:
        raise EndpointRefused(f"base_url is at most {MAX_BASE_URL_CHARS} characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise EndpointRefused(
            "base_url is not a URL: give https://host/path, such as https://api.openai.com/v1"
        ) from None
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise EndpointRefused("base_url must be an https URL, such as https://api.openai.com/v1")
    if parts.username is not None or parts.password is not None:
        raise EndpointRefused("base_url may not hold a user or a password: the API key goes in the key field")
    if parts.query or parts.fragment:
        raise EndpointRefused("base_url is a host and a path, without a query or a fragment")
    if any(char.isspace() for char in parts.path):
        raise EndpointRefused("base_url may not hold spaces")
    host = parts.hostname.lower().rstrip(".")
    refusal = host_refusal(host)
    if refusal is not None:
        raise EndpointRefused(refusal)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(("https", netloc, parts.path.rstrip("/"), "", ""))


def host_refusal(host: str) -> str | None:
    """Why the hub does not call ``host`` by its name or literal address, or None; its addresses are checked apart."""
    try:
        return address_refusal(ipaddress.ip_address(host), host)
    except ValueError:
        pass
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        return f"{host} is a name of a private network: the hub calls only endpoints on the public internet"
    if not HOST.fullmatch(host):
        return (
            f"{host} is not a public host name: the hub calls only a name with a dot, such as api.openai.com, "
            "never one of its own network"
        )
    return None


def address_refusal(address: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> str | None:
    """Why the hub does not call ``address`` (an address of ``host``), or None for a public one."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_global and not address.is_multicast and address not in CGNAT:
        return None
    if address.is_loopback:
        what = "a loopback address"
    elif address.is_link_local:
        what = "a link-local address (the metadata service of a cloud among them)"
    elif address.is_multicast:
        what = "a multicast address"
    elif address in CGNAT:
        what = "a shared address of carrier-grade NAT"
    else:
        what = "a private or reserved address"
    return f"{host} resolves to {what}: the hub calls only endpoints whose every address is public"


async def system_resolver(host: str, port: int) -> list[str]:
    """The addresses of ``host``, as the hub's resolver answers now."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def public_addresses(url: str, resolver: Resolver | None = None) -> list[str]:
    """The addresses of the host of ``url`` (a ``base_url``), every one public; EndpointRefused when the host does not
    resolve or one of its addresses is not public."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        found = [str(literal)]
    else:
        try:
            found = await (resolver or system_resolver)(host, parts.port or 443)
        except (OSError, UnicodeError):
            raise EndpointRefused(f"{host} does not resolve: check the base URL") from None
    if not found:
        raise EndpointRefused(f"{host} does not resolve: check the base URL")
    for text in found:
        refusal = address_refusal(ipaddress.ip_address(text.split("%")[0]), host)
        if refusal is not None:
            raise EndpointRefused(refusal)
    return found
