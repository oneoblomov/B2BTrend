from __future__ import annotations

from src import fetch_resume_store


def test_checkpoint_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_resume_store, "WORKSPACES_DIR", tmp_path / "workspaces")

    payload = {
        "job_id": "job-1",
        "fingerprint": "abc123",
        "status": "running",
        "resume_state": {"phase": "city_timeline", "completed_steps": 12},
    }

    saved = fetch_resume_store.save_checkpoint("ws_01", payload)
    loaded = fetch_resume_store.load_checkpoint("ws_01")

    assert saved["workspace_id"] == "ws_01"
    assert loaded is not None
    assert loaded["workspace_id"] == "ws_01"
    assert loaded["fingerprint"] == "abc123"
    assert loaded["resume_state"]["phase"] == "city_timeline"


def test_fingerprint_changes_when_fetch_shape_changes():
    fp1 = fetch_resume_store.build_fetch_fingerprint(
        keyword="/m/02vqb5x",
        countries=["TR", "US"],
        use_topic_mode=True,
        language="tr",
        country_keywords={},
    )
    fp2 = fetch_resume_store.build_fetch_fingerprint(
        keyword="/m/02vqb5x",
        countries=["TR", "US", "DE"],
        use_topic_mode=True,
        language="tr",
        country_keywords={},
    )

    assert fp1 != fp2


def test_checkpoint_compatibility_check():
    checkpoint = {"fingerprint": "same-fp"}
    assert fetch_resume_store.is_checkpoint_compatible(checkpoint, "same-fp") is True
    assert fetch_resume_store.is_checkpoint_compatible(checkpoint, "other-fp") is False
    assert fetch_resume_store.is_checkpoint_compatible(None, "same-fp") is False
