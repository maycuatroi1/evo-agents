"""The files people attach to messages (``evo_agents.hub.attachments``; docs/hub.md, section "Attachments"):

- POST /v1/projects/{p}/runs/{id}/attachments?name=, the run's owner, while the run takes messages (queued, held by
  its worker, or a parked author or chat run, which a message resumes);
- POST /v1/chats/{id}/attachments?name=, a member of the chat;

  the request's body is the file's bytes, its Content-Type the file's media type (guessed from the name when it is
  missing, application/octet-stream or not one type/subtype). The hub streams the bytes into a temporary file while it
  counts and hashes them, refuses a file over MAX_FILE_BYTES with 413 (by Content-Length when it says so, else as the
  bytes pass the limit), puts them in the blob store, and answers 201 with the upload: its id goes in the message's
  ``attachment_ids``.
- DELETE /v1/projects/{p}/runs/{id}/attachments/{aid} and DELETE /v1/chats/{id}/attachments/{aid}: the uploader takes
  back an upload no message holds; 204.
- GET /v1/projects/{p}/runs/{id}/attachments/{aid}/content, to the run's readers (``views.readable_run``, by sink),
  for a file of a message to the run (or an upload of the caller's own not sent yet);
- GET /v1/chats/{id}/attachments/{aid}/content, to a member who clears the label of the message that carries it (or
  the uploader, for an upload not sent yet);
- GET /v1/worker/runs/{id}/attachments/{aid}/content, to the worker that claimed the run: a file of a message the run's
  inbox took, or of a chat message the run's agent may read (``chat_read``).

The bytes are served as an output's are (``outputs.service.serving.serve``): nosniff, a safe Content-Disposition, one
Range, and a sandbox Content-Security-Policy for HTML and SVG. Anyone else gets the 404 of a file that does not exist.

A message takes the uploads it names (``service.messages.take``): with each row locked, every id must be an upload of
its sender, for that very run or chat, that no message holds yet; ``repository.hold_in_inbox`` and
``repository.hold_in_chat`` then set the message that holds them, in the transaction that writes the message.
``repository.link_inbox`` ties files to a run_inbox row: the owner's own files, and those of a chat message that a
chat run takes in its inbox (``chat_runs._to_inbox``).

The package has four layers, which lint-imports keeps in this order (contract hub-attachments-layers in
pyproject.toml): ``routes`` hands the request's parts to ``service`` (``uploads`` receives and takes back a file,
``content`` checks who reads its bytes, ``messages`` checks the uploads a message names), ``repository`` holds the SQL
on attachments and run_inbox_attachments, ``models`` the path and query parameters and the refusals of the API; the
model of an upload is ``Attachment`` of the run API (``runs.models.attachment_parts``). Other modules import
``service``, ``repository`` and ``models``, never ``routes``.
"""
