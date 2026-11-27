"""The names and limits of the release of a global skill from the Curator's merges (``service.skill_release``)."""

from __future__ import annotations

ACTION = "skill_release"  # the ledger's line of a release
PUBLISH = "skill.publish"  # evo_agents.hub.server.skills.PUBLISH: the target is skill:global/<name>
MAX_RELEASES = 10  # merges one pass of the job releases a skill of
NIGHT_SHIFT = "night_shift"
