"""What a PUT and a PATCH of a member's provider account take. They hold a project's name (``admin``) and a worker's
(``workers.models.workers``), modules of the hub that reach this service, so they are here and not in the models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from evo_agents.hub.accounts import MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.credentials import MAX_SECRET_BYTES, SECRET_NAME
from evo_agents.hub.provider_accounts import API_FORMATS, MAX_MODELS, PROVIDER_ID
from evo_agents.hub.server.accounts.models.provider_accounts import MAX_BINDINGS, Models, ModelsFrom
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.workers.models.workers import WORKER_NAME

Projects = list[Annotated[str, Field(pattern=PROJECT_NAME)]]
Workers = list[Annotated[str, Field(pattern=WORKER_NAME)]]


class ProviderAccountWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=PROVIDER_ID.pattern, description="the id of an entry of the catalog")
    base_url: str | None = Field(None, description="the https root of its API; the catalog's when left out")
    api_format: Literal[API_FORMATS] | None = Field(None, description="the catalog's when left out")  # pyright: ignore[reportInvalidTypeForm]
    value: SecretStr | None = Field(
        None, min_length=1, max_length=MAX_SECRET_BYTES, description="the API key; written once, never shown again"
    )
    from_secret: str | None = Field(
        None, pattern=f"^{SECRET_NAME.pattern}$", description="or one of your env secrets whose value is the key"
    )
    models: Models = Field(default_factory=list, max_length=MAX_MODELS, description="model names, as typed")
    models_from: ModelsFrom | None = Field(None, description="add the catalog's default model, or the endpoint's")
    projects: Projects = Field(min_length=1, max_length=MAX_BINDINGS, description="the writer role on each")
    workers: Workers = Field(default_factory=list, max_length=MAX_BINDINGS, description="your own; empty for any")
    expires_at: AwareDatetime | None = Field(None, description="after this no run takes it; in the future")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 0 leaves it")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner in a fill-first pool")
    paused: bool | None = Field(None, description="true: no new run takes it")


class ProviderAccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(None, description="the https root of its API")
    api_format: Literal[API_FORMATS] | None = None  # pyright: ignore[reportInvalidTypeForm]
    value: SecretStr | None = Field(None, min_length=1, max_length=MAX_SECRET_BYTES, description="a new API key")
    from_secret: str | None = Field(None, pattern=f"^{SECRET_NAME.pattern}$", description="or an env secret's value")
    models: Models | None = Field(None, max_length=MAX_MODELS, description="the model names, replaced")
    models_from: ModelsFrom | None = Field(None, description="add the catalog's default model, or the endpoint's")
    projects: Projects | None = Field(None, min_length=1, max_length=MAX_BINDINGS)
    workers: Workers | None = Field(None, max_length=MAX_BINDINGS)
    expires_at: AwareDatetime | None = Field(None, description="null drops the end")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = Field(None, description="true pauses it, false resumes it")
