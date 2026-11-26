"""``evo-agents hub srs list|show|import|export|submit|approve|reviews|withdraw|retire|archive|unarchive|findings|waive``: the
BA documents of a project that enabled the core plugin srs, through its routes under /v1/x/srs/<project>/ (``docs/plugins.md``, Core
plugins). The plugin's code is ``evo_agents.hub.server.srs``, which this client never imports: it speaks HTTP only.

``list`` prints the documents with their latest and approved versions, ``--all`` the archived ones too. ``show SLUG``
prints a document's versions and the items of its latest version (``--version N`` another), ``--item CODE`` one item
with its fields and children. ``import FILE`` reads Markdown (``-`` for stdin) whose headings follow the template, as
a new document (``--slug``, ``--title``) or as the next version of ``--doc``; ``--dry-run`` prints the preview only.
``comments list|add|reply|resolve|reopen SLUG`` reads and writes threads of comments, one on a passage of an item with
``--item`` and ``--quote``; ``@ba`` in a comment asks @ba to answer in its thread. ``queue`` prints what waits for review in the project, use case by use case, with whether the caller may review each
and the link of its review. ``export SLUG`` prints a version as Markdown (``--no-anchors`` leaves out the screens' code comments, ``--output``
writes a file). ``submit SLUG N`` sends a draft to review; ``submit SLUG --item CODE`` sends one use case to review on
its own, the revision it has in the latest version. ``approve SLUG N`` approves a version, ``--request-changes`` sends
it back with ``--comment``; with ``--item CODE`` (repeatable) the review is of those use cases one by one, in order, and
stops at the first the hub refuses (N left out: the latest version). ``reviews SLUG N`` prints the version's state and
the use cases it needs reviewed (N left out: the latest), each with its revision (``UC ORD-1 r3``), the reviews that
count and the link of its review. ``withdraw SLUG N`` takes a version in review out of review for good, as its author or an
admin, with the reason as a comment on it (``--comment``). ``retire SLUG CODE...``, an admin's, saves the next version
without the items holding those codes or aliases and their subtrees (POST .../documents/{slug}/retire), a draft, and
prints its number and every item dropped; ``--dry-run`` prints the items only and writes nothing. ``archive SLUG``, an
admin's, takes a document out of the list, @ba's search and the knowledge graph and closes it to changes;
``unarchive SLUG`` opens it again. ``findings SLUG`` prints the conflicts @ba found in a document, with their sources
and how each was settled (``--status`` keeps one status); an open one of level decide holds approval. ``waive SLUG
FINDING``, an admin's, waives an open one with ``--reason``. ``--json`` prints what the hub answered. A refusal prints the hub's reason and each
problem it listed.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), listed by hand in
:data:`SRS`: ``documents`` (list, show, import, export, retire, archive, unarchive), ``review`` (submit, approve,
queue, reviews, withdraw), ``findings`` (findings, waive) and ``comments``;
:class:`~evo_agents.hub.cli.srs.resources.Documents` is the gateway to the plugin's routes. Standard library only,
like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Group
from evo_agents.hub.cli.srs.comments import COMMENTS
from evo_agents.hub.cli.srs.documents import Archive, Retire, SrsExport, SrsImport, SrsList, SrsShow, Unarchive
from evo_agents.hub.cli.srs.findings import Findings, Waive
from evo_agents.hub.cli.srs.review import Approve, Queue, Reviews, Submit, Withdraw

SRS = Group(
    "srs",
    "BA documents of a project: versions, coded items, approval (plugin srs)",
    [
        SrsList(),
        SrsShow(),
        SrsImport(),
        SrsExport(),
        Submit(),
        Approve(),
        Queue(),
        Reviews(),
        Withdraw(),
        Retire(),
        Archive(),
        Unarchive(),
        Findings(),
        Waive(),
        COMMENTS,
    ],
)
