"""A producer added while the API runs (ADR-023 phase 2: an installed
plug-in's work), against the real database: Processing lists it, the queue
hands out its clips, its failures are kept, and the repair summary counts
what it still needs; taken away, it's none of those.
"""

from __future__ import annotations

import pytest

from tests.clips import make_clip
from tests.producer_fakes import due
from tests.test_added_producers import NAME, _spec, use_added
from tests.test_scans_api import env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow


@pytest.fixture
def library(env):  # noqa: F811
    r = env.client.post("/v1/libraries", json={"name": "Added", "root_path": "/Volumes/added"}, headers=env.admin)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    yield library_id
    use_added(())


def _summary(env, library_id: str) -> dict:  # noqa: F811
    r = env.client.get("/v1/assets/repair-summary", params={"library_id": library_id}, headers=env.admin)
    assert r.status_code == 200, r.text
    return r.json()


def _failure(env, asset_id: str):  # noqa: F811
    return env.client.post("/v1/producers/failures", headers=env.admin, json={
        "items": [{"asset_id": asset_id, "artifact": NAME, "error": "the plug-in said no"}]})


def test_a_producer_added_while_the_api_runs_is_listed_handed_out_and_counted(env, library):  # noqa: F811
    asset_id = make_clip(env.tenant_url, library, "added.jpg")
    assert f"missing_{NAME}" not in _summary(env, library)
    assert _failure(env, asset_id).status_code == 422  # no producer makes it yet

    use_added([_spec()])
    r = env.client.get("/v1/producers", headers=env.admin)
    assert r.status_code == 200, r.text
    rows = {p["artifact"]: p for p in r.json()["producers"]}
    assert rows[NAME]["title"] == "Example facts"
    assert [c["asset_id"] for c in due(env.tenant_url, NAME, [library])] == [asset_id]
    assert _summary(env, library)[f"missing_{NAME}"] == 1

    r = _failure(env, asset_id)
    assert r.status_code == 200 and r.json() == {"recorded": 1}, r.text
    assert due(env.tenant_url, NAME, [library]) == []  # waiting its turn
    assert _summary(env, library)[f"missing_{NAME}"] == 0 and _summary(env, library)["waiting_failures"] == 1

    use_added(())
    r = env.client.get("/v1/producers", headers=env.admin)
    assert NAME not in {p["artifact"] for p in r.json()["producers"]}
    assert f"missing_{NAME}" not in _summary(env, library)
