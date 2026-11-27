"""The shapes of a login a run's agent asks for (``routes.asks``): its body, its answer and the decision it opens."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import browser_login
from evo_agents.hub.server.decisions.models import DecisionAsk

MAX_REASON_CHARS = 1000


class LoginAsk(DecisionAsk):
    """The decision a login asks: DecisionAsk, in the category the agent cannot name with `worker ask`."""

    category: Literal["login"]  # browser_login.CATEGORY


class BrowserLoginBody(BaseModel):
    identity: str = Field(
        min_length=1, max_length=200, description="the id or the name of one of the run's browser identities"
    )
    url: str = Field(min_length=1, max_length=browser_login.MAX_URL_CHARS, description="the page to sign in on")
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, description="why the agent needs the login")


class BrowserLoginAsked(BaseModel):
    outcome: Annotated[Literal["asked", "failed"], Field(description="asked: a decision is open; failed: the run ends")]
    decision_id: int | None = Field(description="the login decision, for outcome asked")
    identity_id: int = Field(description="the browser identity the login is for")
    message: str = Field(description="what the agent is told")
