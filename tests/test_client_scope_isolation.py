from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from app import app
from src import fetch_resume_store, workspace_store
from src.fetch_job_store import fetch_job_store


def _client_headers(client_id: str) -> dict[str, str]:
    return {"Cookie": f"b2btrend-client-id={client_id}"}


def _reset_state() -> None:
    workspace_store.reset_memory_state()
    fetch_resume_store.import_state({}, client_id=None)
    fetch_job_store.import_state({}, client_id=None)


def test_browser_state_and_workspaces_are_scoped_by_client_id(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "WORKSPACES_DIR", tmp_path / "workspaces")
    _reset_state()

    client = TestClient(app)
    headers_a = _client_headers("client-a")
    headers_b = _client_headers("client-b")

    response = client.post(
        "/api/workspaces",
        headers=headers_a,
        json={
            "name": "Client A Workspace",
            "keyword": "/m/02vqb5x",
            "countries": ["TR", "US"],
            "language": "tr",
            "use_topic_mode": False,
            "country_keywords": {},
        },
    )
    assert response.status_code == 200
    workspace_id = response.json()["item"]["id"]

    workspace_store.save_workspace_dataset(
        workspace_id,
        pd.DataFrame([
            {"country": "US", "city": "New York", "geo_code": "US-NY", "score": 95},
        ]),
        pd.DataFrame(),
        client_id="client-a",
    )

    scoped_a = client.get("/api/workspaces", headers=headers_a)
    scoped_b = client.get("/api/workspaces", headers=headers_b)
    export_a = client.get("/api/browser-state", headers=headers_a)
    export_b = client.get("/api/browser-state", headers=headers_b)

    assert scoped_a.status_code == 200
    assert scoped_b.status_code == 200
    assert len(scoped_a.json()["items"]) == 1
    assert scoped_b.json()["items"] == []

    payload_a = export_a.json()["workspace"]
    payload_b = export_b.json()["workspace"]

    assert workspace_id in payload_a["datasets"]
    assert payload_a["datasets"][workspace_id]["cities"][0]["geo_code"] == "US-NY"
    assert payload_b["workspaces"] == []
    assert payload_b["datasets"] == {}


def test_fetch_and_resume_state_do_not_cross_clients():
    _reset_state()

    checkpoint = fetch_resume_store.save_checkpoint(
        "ws-1",
        {"job_id": "job-a", "fingerprint": "fp-a", "status": "running", "resume_state": {"step": 1}},
        client_id="client-a",
    )
    assert checkpoint["workspace_id"] == "ws-1"
    assert fetch_resume_store.load_checkpoint("ws-1", client_id="client-a") is not None
    assert fetch_resume_store.load_checkpoint("ws-1", client_id="client-b") is None

    job_a = fetch_job_store.start_job(
        workspace_id="ws-1",
        keyword="/m/02vqb5x",
        countries=["TR"],
        use_topic_mode=False,
        language="tr",
        country_keywords={},
        client_id="client-a",
    )
    job_b = fetch_job_store.start_job(
        workspace_id="ws-1",
        keyword="/m/02vqb5x",
        countries=["TR"],
        use_topic_mode=False,
        language="tr",
        country_keywords={},
        client_id="client-b",
    )

    snapshot_a = fetch_job_store.snapshot(client_id="client-a")
    snapshot_b = fetch_job_store.snapshot(client_id="client-b")

    assert snapshot_a["active"]["job_id"] == job_a["job_id"]
    assert snapshot_b["active"]["job_id"] == job_b["job_id"]
    assert snapshot_a["active"]["job_id"] != snapshot_b["active"]["job_id"]


def test_workspace_dataset_survives_memory_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_store, "WORKSPACES_DIR", tmp_path / "workspaces")
    workspace_store.reset_memory_state()

    workspace = workspace_store.create_workspace(
        name="Persisted Workspace",
        keyword="/m/02vqb5x",
        countries=["TR", "US"],
        client_id="client-persist",
    )
    workspace_store.save_workspace_dataset(
        workspace["id"],
        pd.DataFrame([
            {"country": "TR", "city": "Istanbul", "geo_code": "TR-34", "score": 91},
        ]),
        pd.DataFrame([
            {"country": "TR", "city": "Istanbul", "geo_code": "TR-34", "date": "2026-01-01", "score": 77},
        ]),
        client_id="client-persist",
    )

    workspace_store.reset_memory_state("client-persist")

    meta = workspace_store.load_workspace_meta(workspace["id"], client_id="client-persist")
    cities, timeline = workspace_store.load_workspace_dataset(workspace["id"], client_id="client-persist")

    assert meta["id"] == workspace["id"]
    assert meta["name"] == "Persisted Workspace"
    assert not cities.empty
    assert not timeline.empty
    assert cities.iloc[0]["geo_code"] == "TR-34"
    assert timeline.iloc[0]["geo_code"] == "TR-34"