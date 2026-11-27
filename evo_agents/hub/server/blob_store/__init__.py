"""Blob uploads on the hub, the code behind the routes of ``evo_agents.hub.server.blobs``, which describes them.

The package has four layers, which import-linter keeps in order (contract ``hub-blob-store-layers``): ``routes``
(empty: the routes stay in ``evo_agents.hub.server.blobs``), then ``service`` (who may upload and commit, what the blob
store is asked, what a route answers: ``uploads``), ``repository`` (every SQL statement on blob_uploads and blobs) and
``models`` (the bodies, the holder, the refusals and the limits). Other modules import ``service`` and ``models``, or
the names ``evo_agents.hub.server.blobs`` re-exports.
"""
