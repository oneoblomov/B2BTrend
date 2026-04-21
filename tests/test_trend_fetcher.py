import os
from unittest.mock import Mock

import pandas as pd
import pytest

from src import trend_fetcher
from src.trend_fetcher import _build_client, _score_col, FetchConfig, fetch_country_cities, fetch_timeline, fetch_hourly_data, fetch_related_queries, fetch_related_topics, fetch_trends_dataset


def test_score_col_returns_first_numeric_column():
    df = pd.DataFrame({"city": ["A", "B"], "score": [10, 20], "isPartial": [False, False]})
    assert _score_col(df) == "score"


def test_fetch_country_cities_with_mocked_client():
    cfg = FetchConfig(use_cache=False)
    client = Mock()
    # interest_by_region returns a DataFrame with one numeric column
    client.interest_by_region.return_value = pd.DataFrame(
        {"geoCode": ["US-CA"], "value": [83]},
        index=pd.Index(["Los Angeles"], name="geoName"),
    )
    client.build_payload = Mock()

    result = fetch_country_cities(client, "US", cfg)

    assert not result.empty
    assert result.iloc[0]["city"] == "Los Angeles"
    assert result.iloc[0]["score"] == 83


def test_fetch_timeline_with_mocked_client():
    cfg = FetchConfig(use_cache=False)
    client = Mock()
    client.build_payload = Mock()
    client.interest_over_time.return_value = pd.DataFrame(
        {"us_score": [42], "isPartial": [False]},
        index=pd.to_datetime(["2026-03-01"]),
    )

    result = fetch_timeline(client, "US", cfg, timeframe="today 1-d")

    assert not result.empty
    assert "score" in result.columns
    assert result.iloc[0]["score"] == 42


def test_fetch_related_queries_handles_build_payload_error():
    cfg = FetchConfig(use_cache=False)
    client = Mock()
    client.build_payload.side_effect = Exception("Network unavailable")

    result = fetch_related_queries(client, "US", cfg)

    assert "top" in result and "rising" in result
    assert result["top"].empty
    assert result["rising"].empty


def test_fetch_related_topics_handles_build_payload_error():
    cfg = FetchConfig(use_cache=False)
    client = Mock()
    client.build_payload.side_effect = Exception("Network unavailable")

    result = fetch_related_topics(client, "US", cfg)

    assert "top" in result and "rising" in result
    assert result["top"].empty
    assert result["rising"].empty


@pytest.mark.integration
def test_fetch_timeline_integration_one_geo():
    # Çok yavaş olabilir, sadece tek bir veri çağrısı için.
    if os.getenv("RUN_INTEGRATION", "0") != "1":
        pytest.skip("Integration testi çevrimiçi istek gerektiriyor")

    cfg = FetchConfig(use_cache=False)
    client = _build_client(cfg)
    result = fetch_timeline(client, "US", cfg, timeframe="today 1-d")

    assert result is not None
    assert not result.empty
    assert "date" in result.columns
    assert "score" in result.columns


def test_fetch_hourly_data_handles_build_payload_error(monkeypatch):
    cfg = FetchConfig(use_cache=False)
    client = Mock()
    client.build_payload.side_effect = Exception("429 too many requests")
    monkeypatch.setattr(trend_fetcher, "_build_client", lambda *_args, **_kwargs: client)

    result = fetch_hourly_data("US", config=cfg)

    assert result.empty


def test_fetch_trends_dataset_country_first_pipeline(monkeypatch):
    cfg = FetchConfig(use_cache=False, max_attempt_per_country=1, top_cities_per_country=1)
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(trend_fetcher, "_build_client", lambda *_args, **_kwargs: object())

    def fake_fetch_country_cities(_client, country, _cfg, **_kwargs):
        calls.append(("city_list", country))
        return pd.DataFrame([
            {"country": country, "city": f"{country}-city", "geo_code": f"{country}-geo", "score": 77},
        ])

    def fake_fetch_timeline(_client, geo, _cfg, timeframe=None, **kwargs):
        phase = str(kwargs.get("phase") or "city_timeline")
        calls.append((phase, geo))
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-01"]),
                "score": [55],
            }
        )

    monkeypatch.setattr(trend_fetcher, "fetch_country_cities", fake_fetch_country_cities)
    monkeypatch.setattr(trend_fetcher, "fetch_timeline", fake_fetch_timeline)

    cities, timeline = fetch_trends_dataset(countries=["US", "TR"], config=cfg)

    assert not cities.empty
    assert not timeline.empty

    # Country timelines should be fetched before any city list request.
    assert calls[0] == ("country_timeline", "US")
    assert calls[1] == ("country_timeline", "TR")
    assert calls[2] == ("city_list", "US")
    assert calls[3] == ("city_list", "TR")
    assert all(phase != "city_timeline" for phase, _ in calls)


def test_fetch_trends_dataset_resume_skips_completed_steps(monkeypatch):
    cfg = FetchConfig(use_cache=False, max_attempt_per_country=1, top_cities_per_country=1)
    calls: list[tuple[str, str]] = []

    seed_cities = pd.DataFrame([
        {"country": "US", "city": "US-city", "geo_code": "US-geo", "score": 88},
    ])
    seed_timeline = pd.DataFrame([
        {"country": "US", "city": "", "geo_code": "US", "date": "2026-01-01", "score": 66},
        {"country": "US", "city": "US-city", "geo_code": "US-geo", "date": "2026-01-01", "score": 44},
    ])
    resume_state = {
        "completed_country_timelines": ["US"],
        "completed_city_lists": ["US"],
        "completed_city_timelines": {"US": ["US-geo"]},
    }

    monkeypatch.setattr(trend_fetcher, "_build_client", lambda *_args, **_kwargs: object())

    def fake_fetch_country_cities(_client, country, _cfg, **_kwargs):
        calls.append(("city_list", country))
        return pd.DataFrame([
            {"country": country, "city": f"{country}-city", "geo_code": f"{country}-geo", "score": 70},
        ])

    def fake_fetch_timeline(_client, geo, _cfg, timeframe=None, **kwargs):
        phase = str(kwargs.get("phase") or "city_timeline")
        calls.append((phase, geo))
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-01"]),
                "score": [42],
            }
        )

    monkeypatch.setattr(trend_fetcher, "fetch_country_cities", fake_fetch_country_cities)
    monkeypatch.setattr(trend_fetcher, "fetch_timeline", fake_fetch_timeline)

    cities, timeline = fetch_trends_dataset(
        countries=["US", "TR"],
        config=cfg,
        seed_cities=seed_cities,
        seed_timeline=seed_timeline,
        resume_state=resume_state,
    )

    # US should be skipped from network calls because resume state + seed data already has it.
    assert ("country_timeline", "US") not in calls
    assert ("city_list", "US") not in calls
    assert ("city_timeline", "US-geo") not in calls

    # TR should still be fetched in the automatic phases.
    assert ("country_timeline", "TR") in calls
    assert ("city_list", "TR") in calls
    assert all(phase != "city_timeline" for phase, _ in calls)

    assert set(cities["country"].astype(str).tolist()) == {"US", "TR"}
    assert {"US", "TR"}.issubset(set(timeline["country"].astype(str).tolist()))


def test_save_workspace_dataset_deduplicates_rows(tmp_path, monkeypatch):
    from src import workspace_store

    monkeypatch.setattr(workspace_store, "WORKSPACES_DIR", tmp_path / "workspaces")

    cities = pd.DataFrame(
        [
            {"country": "US", "city": "New York", "geo_code": "US-NY", "score": 95},
            {"country": "US", "city": "New York", "geo_code": "US-NY", "score": 95},
        ]
    )
    timeline = pd.DataFrame(
        [
            {"country": "US", "city": "New York", "geo_code": "US-NY", "date": "2026-01-01", "score": 40},
            {"country": "US", "city": "New York", "geo_code": "US-NY", "date": "2026-01-01", "score": 40},
        ]
    )

    path = workspace_store.save_workspace_dataset("ws-1", cities, timeline)
    saved = pd.read_csv(path)

    assert len(saved) == 2
    assert saved[saved["row_type"] == "city"].shape[0] == 1
    assert saved[saved["row_type"] == "timeline"].shape[0] == 1


def test_save_workspace_dataset_preserves_existing_city_timelines(tmp_path, monkeypatch):
    from src import workspace_store

    monkeypatch.setattr(workspace_store, "WORKSPACES_DIR", tmp_path / "workspaces")

    initial_cities = pd.DataFrame(
        [
            {"country": "US", "city": "New York", "geo_code": "US-NY", "score": 95},
        ]
    )
    initial_timeline = pd.DataFrame(
        [
            {"country": "US", "city": "New York", "geo_code": "US-NY", "date": "2026-01-01", "score": 40},
        ]
    )
    workspace_store.save_workspace_dataset("ws-1", initial_cities, initial_timeline)

    auto_cities = pd.DataFrame(
        [
            {"country": "US", "city": "New York", "geo_code": "US-NY", "score": 98},
        ]
    )
    auto_timeline = pd.DataFrame(
        [
            {"country": "US", "city": "", "geo_code": "US", "date": "2026-01-08", "score": 52},
        ]
    )
    path = workspace_store.save_workspace_dataset("ws-1", auto_cities, auto_timeline)
    saved = pd.read_csv(path)

    city_timeline_rows = saved[(saved["row_type"] == "timeline") & (saved["city"] == "New York")]
    assert city_timeline_rows.shape[0] == 1
    assert saved[saved["row_type"] == "timeline"]["city"].fillna("").isin(["", "New York"]).all()


def test_manual_city_timeline_fetch_skips_existing(monkeypatch, tmp_path):
    from app import _download_city_timeline_for_workspace
    from src import workspace_store

    monkeypatch.setattr(workspace_store, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr("app.fetch_job_store.snapshot", lambda: {"active": None})
    monkeypatch.setattr("app._build_client", lambda cfg: object())

    workspace_store.create_workspace(name="ws-1", keyword="/m/02vqb5x", countries=["US"])
    workspace_store.update_workspace(
        "ws-1",
        name="ws-1",
        language="tr",
        keyword="/m/02vqb5x",
        countries=["US"],
        use_topic_mode=False,
        country_keywords={},
    )
    workspace_store.save_workspace_dataset(
        "ws-1",
        pd.DataFrame([{"country": "US", "city": "New York", "geo_code": "US-NY", "score": 95}]),
        pd.DataFrame(),
    )

    def fake_fetch_timeline(_client, geo, _cfg, timeframe=None, **kwargs):
        assert geo == "US-NY"
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-01"]),
                "score": [44],
            }
        )

    monkeypatch.setattr("app.fetch_timeline", fake_fetch_timeline)

    result_first = _download_city_timeline_for_workspace("ws-1", "US", "New York", "US-NY")
    result_second = _download_city_timeline_for_workspace("ws-1", "US", "New York", "US-NY")

    assert result_first["status"] == "completed"
    assert result_second["status"] == "skipped"


def test_render_city_analysis_uses_geo_code_when_city_name_differs():
    from app import _render_city_analysis

    cities = pd.DataFrame([
        {"country": "TR", "city": "Izmir", "geo_code": "TR-35", "score": 88},
    ])
    timeline = pd.DataFrame([
        {"country": "TR", "city": "İzmir", "geo_code": "TR-35", "date": pd.to_datetime(["2026-01-01"])[0], "score": 55},
    ])

    result = _render_city_analysis(timeline, cities, "TR", "Izmir", "TR-35")

    assert result["timeline_ready"] is True
    assert result["geo_code"] == "TR-35"
    assert result["city"] == "Izmir"
