"""The feature docs: a project's documents on the hub, Markdown with images, each save a version (``docs/hub.md``,
section Docs).

A document is Markdown under a slug of the project. Each save is the next version of it, never changed after, with its
title, its body, what it changed and who wrote it; a save that changes nothing writes nothing. An image a document
shows is uploaded to the hub's own blob store first (``images``): a PNG, JPEG, WebP or GIF image of at most
MAX_IMAGE_BYTES, checked by its first bytes and kept once per project by its SHA-256. A version names the images it
shows by the names its Markdown gives them (``![Board](shots/board.png "Caption")``), and the hub serves each image to
the readers of the document from its own origin; the web never loads an image from anywhere else.

The routes are under /v1/x/docs/<project>/ (``server``) for members with a grant on a project the feature is on in:
readers read, writers create documents, save versions and upload images. A document carries a label, the project's by
default or one an admin gives it when creating it, and every version and image of it is read under that label through
the sink X-Evo-Sink names, else the project's hub sink, as plans are read. The hub's web shows them on the feature's
native pages (``PAGES``): the documents, a document read at any version with its images, its history, and the editor.
``evo-agents hub docs list|show|put|history`` is their client: ``put`` uploads the images a Markdown file names beside
it and saves the file as the next version. The tool ``docs__read`` of the hub's /mcp reads them for the agent of a run.

The hub imports this package for the manifest only: ``store`` reads and writes the documents, ``images`` keeps and
serves their images, ``models`` and ``server`` are the API, ``agent`` is the tool.
"""

from evo_agents.hub.tables_docs import MAX_IMAGE_BYTES, SLUG

SERVER = "evo_agents.hub.server.docs.server"
AGENT = "evo_agents.hub.server.docs.agent"

READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document": {
            "type": "string",
            "pattern": SLUG,
            "description": "the document's slug; left out: the documents of the project",
        },
        "version": {"type": "integer", "minimum": 1, "description": "a version's number; the latest when left out"},
    },
}
TOOLS = (
    (
        "read",
        "Read the project's docs",
        "Read a document of the project: without document, the list of documents; with it, a version as Markdown (the "
        "latest unless version names one) with its title, author and the images it shows, each by its name.",
        READ_SCHEMA,
    ),
)

TABLES = (
    ("docs_documents", "A project's documents, the latest version of each and the label each carries"),
    ("docs_versions", "Each save of a document, never changed after"),
    ("docs_images", "The images uploaded for a project's documents, once each by SHA-256"),
    ("docs_version_images", "The images a version shows, by the name its Markdown gives each"),
)

# The native pages of the hub's web (web/src/app/(app)/p/[project]/x/docs, web/src/components/docs): the documents, with
# a document's pages under it, in the project's menu.
PAGES = {"documents": {"title": "Docs", "icon": "book-open", "description": "The project's documents"}}

MANIFEST = {
    "id": "docs",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Docs",
    "description": "A project's documents: Markdown with images, each save a version, read and written by its members.",
    "engines": {"evo-agents": ">=0.20"},
    "plugin_api": 1,
    "contributes": {
        "mcp_tools": [
            {"name": name, "title": title, "description": text, "python": f"{AGENT}:{name}", "input_schema": schema}
            for name, title, text, schema in TOOLS
        ],
        "routes": [{"name": "documents", "title": "The documents of a project", "python": f"{SERVER}:router"}],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
        "pages": [{"name": name, **page} for name, page in PAGES.items()],
    },
}

__all__ = ["MANIFEST", "MAX_IMAGE_BYTES"]
