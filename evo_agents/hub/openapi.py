"""``evo-agents hub openapi``: print the hub API's OpenAPI document, the contract the web client is generated from.

The document comes from the same FastAPI app ``hub serve`` runs, built over a placeholder configuration: nothing
connects to Postgres or GitHub, no ``EVO_HUB_*`` variable is read, and the output is the same on every machine for
one checkout. ``web/`` runs it through ``pnpm gen:api`` to regenerate ``src/lib/api/schema.d.ts``, and CI fails when
the committed file differs from what this prints.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("evo_agents.hub")

# Never connected to: the app's lifespan does not run while the document is built.
PLACEHOLDER_DSN = "postgresql://openapi.invalid/evo_hub"


def document() -> dict:
    """The OpenAPI document of the hub API as a dict."""
    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.server.app import create_app

    config = HubConfig(dsn=PLACEHOLDER_DSN, data_dir=Path(tempfile.gettempdir()) / "evo-hub-openapi")
    return create_app(config).openapi()


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def cmd_openapi(args) -> int:
    from evo_agents.hub.cli import _missing_extra

    try:
        text = render(document())
    except ImportError as exc:
        return _missing_extra(exc, args)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        log.info("openapi document written", extra={"path": args.output})
    else:
        sys.stdout.write(text)
    return 0


def register(hsub) -> None:
    from evo_agents.hub.cli import _server_command

    parser = hsub.add_parser("openapi", help="print the hub API's OpenAPI document (needs the hub-server extra)")
    parser.add_argument("--output", "-o", help="write the document to this file instead of stdout")
    parser.set_defaults(func=_server_command(cmd_openapi))
