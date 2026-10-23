"""The OAuth 1.0a user-context credential of an X account and the header that signs one request with it.

X's developer console gives a member four values for their own account: the app's API key and secret (the consumer)
and the account's access token and secret. They are four sealed fields of the plugin's config (``settings``); this
class holds them for one call, hides all four from its repr, and signs with HMAC-SHA1 as X's documentation says
(docs.x.com, "Authorizing a request" and "Creating a signature"): the oauth_* parameters and the query of the URL,
percent-encoded and sorted, under the key ``consumer_secret&token_secret``. A JSON body is not signed; only a
form-encoded one would be, and no X request of the plugin sends one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


def _encode(value: str) -> str:
    return quote(value, safe="-._~")


@dataclass(frozen=True)
class OAuth1:
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)
    token: str = field(repr=False)
    token_secret: str = field(repr=False)

    def header(self, method: str, url: str, *, nonce: str | None = None, timestamp: int | None = None) -> str:
        """The ``Authorization`` value of ``method url``; ``nonce`` and ``timestamp`` are fixed by tests only."""
        parts = urlsplit(url)
        oauth = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": nonce or secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
            "oauth_token": self.token,
            "oauth_version": "1.0",
        }
        signed = sorted((_encode(k), _encode(v)) for k, v in [*parse_qsl(parts.query, True), *oauth.items()])
        base_url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
        text = "&".join([method.upper(), _encode(base_url), _encode("&".join(f"{k}={v}" for k, v in signed))])
        key = f"{_encode(self.consumer_secret)}&{_encode(self.token_secret)}"
        digest = hmac.new(key.encode(), text.encode(), hashlib.sha1).digest()
        oauth["oauth_signature"] = base64.b64encode(digest).decode()
        return "OAuth " + ", ".join(f'{_encode(k)}="{_encode(v)}"' for k, v in sorted(oauth.items()))
