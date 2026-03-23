import os
from unittest.mock import Mock

import pandas as pd
import pytest

from src.trend_fetcher import _build_client, _score_col, FetchConfig, fetch_country_cities, fetch_timeline


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
