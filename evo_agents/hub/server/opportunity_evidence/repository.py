"""The evidence of an opportunity in the database: table opportunity_evidence (schema 0143), its lines numbered within
the opportunity they show, read and written by the ``EvidenceStore`` of a problem's evidence."""

from __future__ import annotations

from evo_agents.hub import tables
from evo_agents.hub.server.problem_evidence.repository import EvidenceStore

STORE = EvidenceStore(tables.opportunity_evidence, tables.opportunities, "opportunity")
