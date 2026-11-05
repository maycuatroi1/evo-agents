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
# Python 3.13 renamed two HTTP status phrases FastAPI copies into the document: back to the names of 3.11 and 3.12, so
# a schema.d.ts generated on any supported Python is the one CI generates (PRs #418 and #420 had flipped them by hand).
PHRASES = {"Unprocessable Content": "Unprocessable Entity", "Content Too Large": "Request Entity Too Large"}


def _old_phrases(node):
    if isinstance(node, dict):
        return {key: _old_phrases(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_old_phrases(value) for value in node]
    return PHRASES.get(node, node) if isinstance(node, str) else node


def document() -> dict:
    """The OpenAPI document of the hub API as a dict, the same on every supported Python."""
    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.server.app import create_app

    config = HubConfig(dsn=PLACEHOLDER_DSN, data_dir=Path(tempfile.gettempdir()) / "evo-hub-openapi")
    return _old_phrases(create_app(config).openapi())


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def cmd_openapi(args) -> int:
    from evo_agents.hub.cli import missing_extra

    try:
        text = render(document())
    except ImportError as exc:
        return missing_extra(exc, args)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        log.info("openapi document written", extra={"path": args.output})
    else:
        sys.stdout.write(text)
    return 0


def register(hsub) -> None:
    from evo_agents.hub.cli import server_command

    parser = hsub.add_parser("openapi", help="print the hub API's OpenAPI document (needs the hub-server extra)")
    parser.add_argument("--output", "-o", help="write the document to this file instead of stdout")
    parser.set_defaults(func=server_command(cmd_openapi))
