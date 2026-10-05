"""End-to-end tests for time-limited license suspensions."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

START = "2026-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
}
# covers START; left-closed, right-open
SUSP = {"start_at": "2026-06-01T00:00:00Z", "end_at": "2026-06-02T00:00:00Z"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 100,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def suspend(client, license_id, **overrides):
    body = {**SUSP, "reason": "版权方临时下架整改"}
    body.update(overrides)
    return client.post(f"/licenses/{license_id}/suspensions", json=body)


def verify(client, key, start=START, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


# ----------------------------------------------------------------------
# registration: validation & persistence
# ----------------------------------------------------------------------
def test_create_suspension_returns_id_window_and_reason(client):
    lic = make_license(client)
    resp = suspend(client, lic["id"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] == 1
    assert data["license_id"] == lic["id"]
    assert data["start_at"] == "2026-06-01T00:00:00.000000Z"
    assert data["end_at"] == "2026-06-02T00:00:00.000000Z"
    assert data["reason"] == "版权方临时下架整改"
    assert data["created_at"]


def test_suspension_normalises_timezone_offsets_to_utc(client):
    lic = make_license(client)
    resp = suspend(
        client,
        lic["id"],
        start_at="2026-06-01T08:00:00+08:00",
        end_at="2026-06-02T08:00:00+08:00",
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["start_at"] == "2026-06-01T00:00:00.000000Z"
    assert data["end_at"] == "2026-06-02T00:00:00.000000Z"


def test_inverted_or_empty_window_rejected_without_record(client):
    lic = make_license(client)
    for bad in (
        {"start_at": "2026-06-02T00:00:00Z", "end_at": "2026-06-01T00:00:00Z"},
        {"start_at": "2026-06-01T00:00:00Z", "end_at": "2026-06-01T00:00:00Z"},
    ):
        resp = suspend(client, lic["id"], **bad)
        assert resp.status_code == 422, resp.text
    assert client.get(f"/licenses/{lic['id']}/suspensions").json() == []


def test_unknown_license_rejected_without_record(client):
    resp = suspend(client, 999)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert client.get("/suspensions/1").status_code == 404


def test_empty_reason_and_invalid_timestamps_rejected(client):
    lic = make_license(client)
    assert suspend(client, lic["id"], reason="").status_code == 422
    assert suspend(client, lic["id"], start_at="not-a-time").status_code == 422
    assert suspend(client, lic["id"], end_at="2026-13-01T00:00:00Z").status_code == 422
    assert client.get(f"/licenses/{lic['id']}/suspensions").json() == []


def test_suspensions_are_queryable_and_immutable(client):
    lic = make_license(client)
    first = suspend(client, lic["id"], reason="第一段").json()
    second = suspend(
        client,
        lic["id"],
        start_at="2026-07-01T00:00:00Z",
        end_at="2026-07-02T00:00:00Z",
        reason="第二段",
    ).json()

    listing = client.get(f"/licenses/{lic['id']}/suspensions").json()
    assert [s["id"] for s in listing] == [first["id"], second["id"]]
    assert client.get(f"/suspensions/{second['id']}").json()["reason"] == "第二段"
    assert client.get("/suspensions/999").status_code == 404
    assert client.get("/licenses/999/suspensions").status_code == 404

    # registered records cannot be modified or deleted
    url = f"/suspensions/{first['id']}"
    assert client.put(url, json={"reason": "改"}).status_code == 405
    assert client.patch(url, json={"reason": "改"}).status_code == 405
    assert client.delete(url).status_code == 405
    assert client.get(url).json()["reason"] == "第一段"


# ----------------------------------------------------------------------
# verification semantics
# ----------------------------------------------------------------------
def test_suspended_license_rejects_with_license_suspended(client):
    lic = make_license(client)
    suspend(client, lic["id"])
    data = verify(client, "s-1").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "LICENSE_SUSPENDED"
    assert data["license_id"] is None
    assert data["remaining_quota"] is None
    # the rejection consumed nothing
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 0


def test_suspension_window_is_left_closed_right_open(client):
    lic = make_license(client)
    suspend(client, lic["id"])

    at_start = verify(client, "edge-1", start="2026-06-01T00:00:00Z").json()
    assert at_start["reject_reason"] == "LICENSE_SUSPENDED"

    inside = verify(client, "edge-2", start="2026-06-01T23:59:59Z").json()
    assert inside["reject_reason"] == "LICENSE_SUSPENDED"

    at_end = verify(client, "edge-3", start="2026-06-02T00:00:00Z").json()
    assert at_end["decision"] == "approved"

    before = verify(client, "edge-4", start="2026-05-31T23:59:59Z").json()
    assert before["decision"] == "approved"


def test_overlapping_suspensions_apply_as_union(client):
    lic = make_license(client)
    suspend(client, lic["id"], start_at="2026-06-01T00:00:00Z",
            end_at="2026-06-10T00:00:00Z", reason="第一段")
    suspend(client, lic["id"], start_at="2026-06-05T00:00:00Z",
            end_at="2026-06-15T00:00:00Z", reason="第二段（与第一段重叠）")

    # union covers 06-01 .. 06-15, including the gap-free overlap region
    for day in (2, 7, 12):
        data = verify(client, f"u-{day}", start=f"2026-06-{day:02d}T12:00:00Z").json()
        assert data["reject_reason"] == "LICENSE_SUSPENDED", day
    after = verify(client, "u-16", start="2026-06-16T12:00:00Z").json()
    assert after["decision"] == "approved"


def test_verification_skips_suspended_and_picks_other_valid_license(client):
    early = make_license(client, valid_to="2026-09-01T00:00:00Z", max_sessions=5)
    late = make_license(client, valid_to="2027-06-01T00:00:00Z", max_sessions=5)
    suspend(client, early["id"])

    data = verify(client, "skip-1").json()
    assert data["decision"] == "approved"
    assert data["license_id"] == late["id"]
    assert data["remaining_quota"] == 4
    # the suspended license was skipped, not consumed
    assert client.get(f"/licenses/{early['id']}").json()["used_sessions"] == 0


def test_partial_suspension_keeps_legacy_reasons_for_the_rest(client):
    suspended = make_license(client, valid_to="2026-09-01T00:00:00Z",
                             max_sessions=5)
    exhausted = make_license(client, valid_to="2027-06-01T00:00:00Z",
                             max_sessions=1)
    suspend(client, suspended["id"])
    assert verify(client, "pp-1").json()["decision"] == "approved"  # drains the other

    data = verify(client, "pp-2").json()
    # suspended licenses are skipped entirely: the only remaining usable
    # license is out of its own quota -> classic reason, not SUSPENDED
    assert data["reject_reason"] == "QUOTA_EXHAUSTED"


def test_suspension_beats_pool_exhausted_when_all_suspended(client):
    pool = client.post("/pools", json={"film_id": "film-1", "max_sessions": 1}).json()
    bound = make_license(client, pool_id=pool["id"], max_sessions=5)
    other = make_license(client, venue_id="hall-b", pool_id=pool["id"],
                         max_sessions=5)
    assert verify(client, "pool-1").json()["decision"] == "approved"  # pool now dry

    suspend(client, bound["id"])
    data = verify(client, "pool-2").json()
    # the only in-window active license for hall-a is suspended
    assert data["reject_reason"] == "LICENSE_SUSPENDED"
    # hall-b's license is not suspended and is blocked only by the dry pool
    data_b = verify(client, "pool-3", venue="hall-b").json()
    assert data_b["reject_reason"] == "POOL_EXHAUSTED"
    assert other["id"] != bound["id"]


def test_revocation_takes_precedence_over_suspension(client):
    lic = make_license(client)
    suspend(client, lic["id"])
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "永久撤片"})
    data = verify(client, "rev-1").json()
    assert data["reject_reason"] == "LICENSE_REVOKED"


def test_suspension_only_affects_verifications_registered_afterwards(client):
    lic = make_license(client, max_sessions=5)
    approved = verify(client, "pre-1").json()
    assert approved["decision"] == "approved"

    suspend(client, lic["id"])

    # history and quota consumption are untouched
    history = client.get("/verifications/pre-1").json()
    assert history["decision"] == "approved"
    assert history["license_id"] == lic["id"]
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 1
    assert lic_after["remaining_sessions"] == 4

    # idempotent replay of the pre-suspension key still returns the original
    replay = verify(client, "pre-1").json()
    assert replay["replay"] is True
    assert replay["decision"] == "approved"
    assert replay["id"] == approved["id"]
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1

    # brand-new verifications inside the window are rejected
    fresh = verify(client, "post-1").json()
    assert fresh["reject_reason"] == "LICENSE_SUSPENDED"


def test_sessions_outside_the_window_are_unaffected(client):
    lic = make_license(client)
    suspend(client, lic["id"])
    data = verify(client, "out-1", start="2026-08-01T20:00:00Z").json()
    assert data["decision"] == "approved"
    assert data["license_id"] == lic["id"]


def test_suspension_of_other_license_does_not_leak(client):
    make_license(client, venue_id="hall-a")
    other = make_license(client, venue_id="hall-b")
    suspend(client, other["id"])
    assert verify(client, "iso-1", venue="hall-a").json()["decision"] == "approved"
    assert verify(client, "iso-2", venue="hall-b").json()[
        "reject_reason"
    ] == "LICENSE_SUSPENDED"


# ----------------------------------------------------------------------
# concurrency: registration vs verification must be linearisable
# ----------------------------------------------------------------------
def test_concurrent_registration_and_verification_linearisable(tmp_path):
    store = Store(str(tmp_path / "susp-concurrent.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=100,  # quota never binds: only the suspension decides
    )

    results, errors = [], []

    def verifier(i: int) -> None:
        try:
            results.append(
                store.verify(
                    film_id="film-x",
                    venue_id="hall-1",
                    start_time=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
                    idempotency_key=f"sk-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def registrar() -> None:
        try:
            store.create_suspension(
                license_id=lic["id"],
                start_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                end_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
                reason="并发登记",
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=verifier, args=(i,)) for i in range(20)]
    threads.append(threading.Thread(target=registrar))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 20
    # every decision corresponds to the complete state either before the
    # registration (approved, quota permitting) or after it (suspended)
    approved = [r for r in results if r["decision"] == "approved"]
    suspended = [
        r for r in results
        if r["decision"] == "rejected" and r["reject_reason"] == "LICENSE_SUSPENDED"
    ]
    assert len(approved) + len(suspended) == 20
    # quota accounting stays exact regardless of interleaving
    final = store.get_license(lic["id"])
    assert final["used_sessions"] == len(approved)
    assert len(store.list_suspensions(license_id=lic["id"])) == 1


def test_concurrent_registrations_all_persist(tmp_path):
    store = Store(str(tmp_path / "susp-multi.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )

    results, errors = [], []

    def registrar(i: int) -> None:
        try:
            results.append(
                store.create_suspension(
                    license_id=lic["id"],
                    start_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    end_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
                    reason=f"第{i}段",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=registrar, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len({r["id"] for r in results}) == 10
    assert len(store.list_suspensions(license_id=lic["id"])) == 10
