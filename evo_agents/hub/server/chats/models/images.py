"""The image of a group chat (``chats.service.images``): the kind its old blobs are recorded with and its refusals."""

from __future__ import annotations

from typing import Any

from evo_agents.hub.server.errors import ErrorBody

IMAGE_KIND = "chat-image"  # the kind blob_deletions records an image no chat refers to any longer with
UNAVAILABLE = "the blob store did not answer; the chat's image was not changed, try again shortly"
SET_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}
DROP_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 503)}
