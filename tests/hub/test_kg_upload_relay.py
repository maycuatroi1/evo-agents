"""The parts a browser sends through the hub (plan kg-upload-archive, step 5): PUT
/v1/kg/{project}/uploads/{id}/parts/{n} relays the body to the store as that part of the multipart upload, for the
sender of an upload in flight only, at exactly the part's size; complete then joins them as for the CLI."""

import hashlib

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub import live
from tests.hub.test_kg_uploads import PART, PROJECT, archive, world  # noqa: F401 (the fixture)


def start(world, data: bytes, name: str = "handbook.zip") -> dict:
    body = {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    answer = world.client.post(f"/v1/kg/{PROJECT}/uploads", json=body, headers=world.writer)
    assert answer.status_code == 200, answer.text
    return answer.json()


def put(world, upload: int, number: int, data: bytes, headers=None):
    return world.client.put(
        f"/v1/kg/{PROJECT}/uploads/{upload}/parts/{number}",
        content=data,
        headers={**(headers or world.writer), "content-type": "application/octet-stream"},
    )


def test_parts_sent_through_the_hub_are_joined_into_the_archive(world):
    data = archive(world, 2 * PART + 300).read_bytes()
    started = start(world, data)
    chunks = [data[i : i + PART] for i in range(0, len(data), PART)]
    assert started["parts"] == len(chunks) == 3
    for number, chunk in enumerate(chunks, start=1):
        answer = put(world, started["id"], number, chunk)
        assert answer.status_code == 200 and answer.json() == {"number": number, "size": len(chunk)}
    again = put(world, started["id"], 2, chunks[1])
    assert again.status_code == 200, "a part sent again replaces itself"
    resumed = start(world, data)
    assert resumed["resumed"] and resumed["done"] == [1, 2, 3], "a reload learns which parts the store holds"
    done = world.client.post(f"/v1/kg/{PROJECT}/uploads/{started['id']}/complete", headers=world.writer)
    assert done.status_code == 200 and done.json()["state"] == "extracting"
    ((key,),) = live.sql(world.db, select(tables.kg_uploads.c.object_key))
    assert world.s3.get(key) == data


def test_a_part_of_the_wrong_size_or_number_or_sender_is_refused(world):
    data = archive(world, 2 * PART).read_bytes()
    started = start(world, data)
    upload = started["id"]
    assert put(world, upload, 1, data[: PART - 1]).status_code == 422
    assert put(world, upload, 1, data[: PART + 1]).status_code == 413
    assert put(world, upload, 3, data[:PART]).status_code == 422
    assert put(world, upload, 1, data[:PART], world.reader).status_code in (403, 404)
    assert put(world, upload + 1000, 1, data[:PART]).status_code == 404
    assert world.store.list_parts(*_multipart(world)) == {}, "nothing refused reached the store"


def test_a_part_of_an_upload_no_longer_in_flight_is_refused(world):
    data = archive(world, PART).read_bytes()
    started = start(world, data)
    assert put(world, started["id"], 1, data).status_code == 200
    world.client.post(f"/v1/kg/{PROJECT}/uploads/{started['id']}/complete", headers=world.writer)
    late = put(world, started["id"], 1, data)
    assert late.status_code == 409 and "extracting" in late.text


def _multipart(world) -> tuple[str, str]:
    u = tables.kg_uploads
    ((key, multipart),) = live.sql(world.db, select(u.c.object_key, u.c.multipart_id))
    return key, multipart
