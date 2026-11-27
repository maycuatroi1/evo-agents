"""The versions of a document in a chat or a run (docs/hub.md, section "Versions of a document"): every file of one
name that its messages carry, oldest first, so the web's document panel shows what each version changed even when the
messages of the older ones are not on the page it loaded.

- GET /v1/chats/{id}/doc-versions?name=, to a member of the chat (``chats.service.access``): the files people attached to its
  messages and the outputs its agents' messages carry, threads included, of the messages whose label the caller
  clears (``chats.service.access.label_reader``), in the order of the messages, a message's attachments before its outputs;
- GET /v1/projects/{p}/runs/{id}/doc-versions?name=, to the run's readers (``views.readable_run``, by sink): the files
  of the messages its inbox took and the outputs of the run, in the order they came.

A document is named as the web names it (``docKey`` of web/src/components/chats/doc-model.ts): the last part of a
file's name after a slash, without case, so an output kept under a path is a version of the file attached under its
own name. Each version carries the URL of its bytes on the route that serves it to the same reader
(``attachments.routes``, ``outputs.routes``). Anyone else gets the 404 of a chat or run that does not exist. Nothing here
is stored: the versions are read from ``attachments``, ``run_inbox_attachments``, ``run_outputs`` and
``run_output_links``.

The package goes from its routes to its service to its repository to its models (contract hub-doc-versions-layers):
``routes`` hands the request's parts and the app's engine to ``service``, which opens the transaction (``UnitOfWork``),
checks the reader and keeps the versions of the document; ``repository`` (``VersionStore``) reads the files; ``models``
holds the answers and ``doc_key``. Other modules import the names below, never the routes.
"""

from evo_agents.hub.server.doc_versions.models import doc_key as doc_key
