"""Seats that end, in chats (plan org-chart, criterion a5): a person whose primary seat ended is at A0, and the @ of an
agent in their message hands no work: the hub answers in the thread why, naming the unit and the day; their message
is still stored and read, and a person whose seat is live gets their run as before
(``evo_agents.hub.server.chat_runs.hand_work``)."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401 (app_key, client and hub are fixtures)
    OTHER,
    OWNER,
    app_key,
    chat_runs_of,
    chat_worker,
    client,
    created,
    hub,
    sent,
    thread,
)


def test_orgchart_expiry_a_mention_by_a_person_whose_primary_seat_ended_hands_no_work(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio")
    chat_worker(client, hub["other"], "theirs")
    chat = created(client, hub["owner"], people=[OTHER])
    (seat,) = [
        s for s in client.get("/v1/org", headers=hub["admin"]).json()["seats"] if s["login"] == OWNER and s["primary"]
    ]
    assert client.post(f"/v1/org/seats/{seat['id']}/end", headers=hub["admin"]).status_code == 200

    mention = sent(client, hub["owner"], chat["id"], "@reviewer please fix the flaky wait test")
    assert chat_runs_of(hub_db) == []
    day = sql(hub_db, select(func.current_date()))[0][0]
    (refusal,) = [m["refusal"] for m in mention["mentions"]]
    assert refusal == (
        f"@reviewer: the primary seat of {OWNER} in unit root ended on {day}, which leaves them at level A0: a chat "
        "run is refused"
    )
    answer = thread(client, hub["owner"], chat["id"], mention["id"])[-1]
    assert (answer["author"]["kind"], answer["body"]) == ("hub", refusal)

    # someone-else, whose seat is live, gets their run
    theirs = sent(client, hub["other"], chat["id"], "@reviewer then you take it")
    ((run_id, state, _, _, _),) = chat_runs_of(hub_db)
    assert [(m["run_id"], m["refusal"]) for m in theirs["mentions"]] == [(run_id, None)] and state == "queued"
