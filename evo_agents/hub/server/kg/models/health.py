"""The limits of the health of a project's knowledge graph (``evo_agents.hub.server.kg_health`` says what it measures),
the signals its eval pushes, and the shape GET /v1/projects/{p}/curator/kg-health answers with."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from evo_agents.hub import kg_health_eval

NIGHT_SHIFT = "night_shift"
HEALTH_EVERY = timedelta(hours=6)  # a project's graph is measured again this long after its last measure
RETRY_AFTER = timedelta(hours=1)  # and this long after a measure that failed
ERROR_CHARS = 2000
GRAPHS_TRIED = 3  # kept graphs the evaluation may find its snapshot among
MAX_SERIES = 10  # builds of the span the figures list
HISTORY = (1, 200, 30)  # the fewest, most and default measures and builds the route lists
NO_APP = "the hub has no GitHub App to read default branches with (EVO_HUB_GITHUB_APP_*)"
OFF_GITHUB = "the hub reads the default branches of repos on GitHub only"
NO_REPO = "the source reads a path, not a repo of the project with an origin"

EVAL_SIGNALS = (
    (
        "kg.retrieval.hit_at_5",
        "percent",
        "Of the holdout questions of the project's question set the hub asks its latest graph (all the questions it "
        "asks, the done plan steps among them, when the set has none), the percent whose answer kg_search puts in its "
        "first 5 results (kg_health.eval).",
        kg_health_eval.hit_percent,
    ),
    (
        "kg.retrieval.tokens_per_file",
        "tokens",
        "The tokens kg_search spends per answer file it finds in its first 5 results (its mean tokens over Recall@5) "
        "on the holdout questions of the project's question set the hub asks its latest graph (all the questions it "
        "asks when the set has none) (kg_health.eval).",
        kg_health_eval.tokens_per_file,
    ),
)


class KgMeasure(BaseModel):
    measured_at: datetime
    build_id: int | None = Field(description="the build whose graph the measure read")
    error: str | None = Field(description="why the measure failed; null when it did not")
    build: dict | None = Field(description="that build: id, content_hash, finished_at")
    sources: list[dict] = Field(
        description="each git source: the commit it read, its default branch and head, the commits behind and the hours "
        "the oldest waited, the files missing, stale and deleted with samples and evidence, and why for what was not read"
    )
    eval: dict | None = Field(description="the retrieval evaluation of the build's graph, or why there is none")


class KgBuildCoverage(BaseModel):
    id: int
    finished_at: datetime
    changed: bool = Field(description="false when an earlier build had the same content")
    coverage: dict | None = Field(description="what the build covers (kg_builds.coverage); null before schema 0055")


class KgHealthView(BaseModel):
    project: str
    measures: list[KgMeasure] = Field(description="newest first")
    builds: list[KgBuildCoverage] = Field(description="the successful builds, newest first")
