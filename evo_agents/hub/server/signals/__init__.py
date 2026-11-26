"""Signals: a service or a member sends a batch, members read them by key and time (``evo_agents.hub.signals``).

POST /v1/signals/{project} takes a batch of at most 500 items in at most 1 MiB of body, from an ingest key of the
project (``Authorization: Bearer evi_...``, which the middleware lets reach this route alone) or from a member with the
writer role, with their machine token or web session. It answers 202 with the batch's id, how many signals it stored,
how many repeated an ``external_id`` stored before, and each item refused with its index and why; the others are
stored. A body over the limit is a 413, read no further; a key past its rate or a project past its day is a 429 with
Retry-After; an ingest key revoked or unknown is a 401, and one of another project a 403. A project whose knowledge.yaml
declares no hub sink takes no signal (422), as it takes no memory.

GET /v1/projects/{project}/signals reads them for a member with a grant: the newest first, of one key or all, in a range
of measured times, of one tenant, at most ``limit``. A signal is read under its label through the sink X-Evo-Sink
names, else the project's hub sink, as plans are; one above what the member may read is left out. The value of a
signal never goes into a log line.

The registry of a project's signal keys and its ingest keys:

GET /v1/projects/{project}/signal-keys lists the keys for a member with a grant, the archived ones too with
``archived``. An admin of the project adds a key (POST, 201; 409 when the name is taken, archived or not) with its kind,
``number`` or ``feedback``, its unit, description, source and the level its signals get when they name none, a level of
the project's ladder; and archives one (POST .../{name}/archive), after which it takes no new signal and its signals
stay readable.

GET /v1/projects/{project}/signal-ingest-keys lists the ingest keys for an admin of the project, the revoked ones too
with ``revoked``. An admin creates one for a service (POST, 201) with a name unique among the project's live keys and
the highest level it writes (the project's default level when left out); the answer holds the token, ``evi_...``, the
one time the hub shows it: the hub keeps only its SHA-256. Revoking one (POST .../{id}/revoke) takes effect on the next
batch sent with it. Each change writes an audit row naming the project and the key, never a token.

The package has four layers, which lint-imports keeps in this order (contract hub-signals-layers in pyproject.toml):
``routes`` reads the auth of a push itself (an ingest key or a member) and passes a ``Scope``
(``evo_agents.hub.server.base.scope``) of every other route to ``service``, which checks and decides; ``repository``
reads and writes the registry of keys and the ingest keys, and ``evo_agents.hub.signals`` the signals; ``models`` holds
the request and response models. Other modules import ``service`` and ``models``, never ``routes``.
"""
