from __future__ import annotations

import re
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import DEFAULT_KEYWORD, TOP_20_COUNTRIES

FILE_LOCK = threading.RLock()
WORKSPACES_DIR = None

_WORKSPACES: dict[str, dict[str, Any]] = {}
_DATASETS: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
_SETTINGS: dict[str, Any] = {}


class WorkspaceValidationError(ValueError):
    pass


def _slugify(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return clean or "workspace"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_countries(countries: list[str] | None, fallback: list[str] | None = None) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for item in countries or []:
        code = str(item).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            continue
        if code in seen:
            continue
        seen.add(code)
        clean.append(code)
    return clean or list(fallback or [])


def _default_meta(workspace_id: str, name: str | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": workspace_id,
        "name": name or workspace_id,
        "language": "tr",
        "keyword": DEFAULT_KEYWORD,
        "countries": list(TOP_20_COUNTRIES),
        "use_topic_mode": False,
        "country_keywords": {},
        "created_at": now,
        "updated_at": now,
        "dataset_rows": 0,
    }


def _normalize_meta(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _default_meta(workspace_id, name=payload.get("name"))
    base["language"] = str(payload.get("language") or "tr").strip() or "tr"
    base["keyword"] = str(payload.get("keyword") or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
    base["countries"] = _normalize_countries(payload.get("countries"), fallback=list(TOP_20_COUNTRIES))
    base["use_topic_mode"] = bool(payload.get("use_topic_mode", False))

    raw_country_keywords = payload.get("country_keywords") or {}
    clean_keywords: dict[str, str] = {}
    if isinstance(raw_country_keywords, dict):
        for key, value in raw_country_keywords.items():
            country = str(key).strip().upper()
            keyword = str(value).strip()
            if country and keyword:
                clean_keywords[country] = keyword
    base["country_keywords"] = clean_keywords

    base["created_at"] = str(payload.get("created_at") or base["created_at"])
    base["updated_at"] = str(payload.get("updated_at") or base["updated_at"])
    try:
        base["dataset_rows"] = max(0, int(payload.get("dataset_rows", 0)))
    except Exception:
        base["dataset_rows"] = 0

    base["id"] = workspace_id
    base["name"] = str(payload.get("name") or workspace_id).strip() or workspace_id
    return base


def _empty_cities() -> pd.DataFrame:
    return pd.DataFrame(columns=["country", "city", "geo_code", "score"])


def _empty_timeline() -> pd.DataFrame:
    return pd.DataFrame(columns=["country", "city", "geo_code", "date", "score"])


def reset_memory_state() -> None:
    with FILE_LOCK:
        _WORKSPACES.clear()
        _DATASETS.clear()
        _SETTINGS.clear()


def export_memory_state() -> dict[str, Any]:
    with FILE_LOCK:
        return {
            "workspaces": [deepcopy(item) for item in _WORKSPACES.values()],
            "default_workspace_id": _SETTINGS.get("default_workspace_id"),
            "datasets": {
                workspace_id: {
                    "cities": cities_df.to_dict(orient="records"),
                    "timeline": timeline_df.to_dict(orient="records"),
                }
                for workspace_id, (cities_df, timeline_df) in _DATASETS.items()
            },
        }


def import_memory_state(payload: dict[str, Any] | None) -> None:
    if not isinstance(payload, dict):
        return

    with FILE_LOCK:
        _WORKSPACES.clear()
        _DATASETS.clear()
        _SETTINGS.clear()

        for item in payload.get("workspaces") or []:
            if not isinstance(item, dict):
                continue
            workspace_id = str(item.get("id") or item.get("workspace_id") or "").strip()
            if not workspace_id:
                continue
            _WORKSPACES[workspace_id] = _normalize_meta(workspace_id, item)

        default_workspace_id = str(payload.get("default_workspace_id") or "").strip()
        if default_workspace_id:
            _SETTINGS["default_workspace_id"] = default_workspace_id

        datasets = payload.get("datasets") or {}
        if isinstance(datasets, dict):
            for workspace_id, dataset in datasets.items():
                if not isinstance(dataset, dict):
                    continue
                cities_df = pd.DataFrame(dataset.get("cities") or [])
                timeline_df = pd.DataFrame(dataset.get("timeline") or [])
                _DATASETS[str(workspace_id).strip()] = (cities_df, timeline_df)


def get_default_workspace_id() -> str | None:
    value = _SETTINGS.get("default_workspace_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_default_workspace(workspace_id: str) -> None:
    _SETTINGS["default_workspace_id"] = workspace_id


def list_workspaces() -> list[dict[str, Any]]:
    return sorted([deepcopy(item) for item in _WORKSPACES.values()], key=lambda item: item.get("updated_at", ""), reverse=True)


def load_workspace_meta(workspace_id: str) -> dict[str, Any]:
    meta = _WORKSPACES.get(workspace_id)
    if meta is None:
        meta = _default_meta(workspace_id)
    cities_df, timeline_df = _DATASETS.get(workspace_id, (_empty_cities(), _empty_timeline()))
    meta = _normalize_meta(workspace_id, meta)
    meta["dataset_rows"] = int(len(cities_df) + len(timeline_df))
    _WORKSPACES[workspace_id] = meta
    return deepcopy(meta)


def create_workspace(name: str, keyword: str, countries: list[str]) -> dict[str, Any]:
    stem = _slugify(name)
    workspace_id = stem
    index = 1
    while workspace_id in _WORKSPACES:
        workspace_id = f"{stem}_{index:02d}"
        index += 1

    meta = _default_meta(workspace_id, name=name.strip() or workspace_id)
    meta["keyword"] = keyword.strip() or DEFAULT_KEYWORD
    normalized_countries = _normalize_countries(countries)
    if not normalized_countries:
        raise WorkspaceValidationError("En az bir gecerli ulke kodu secilmeli.")
    meta["countries"] = normalized_countries
    meta["updated_at"] = _now_iso()
    _WORKSPACES[workspace_id] = meta
    return deepcopy(meta)


def update_workspace(
    workspace_id: str,
    *,
    name: str,
    language: str,
    keyword: str,
    countries: list[str],
    use_topic_mode: bool,
    country_keywords: dict[str, str],
) -> dict[str, Any]:
    meta = load_workspace_meta(workspace_id)
    meta["name"] = name.strip() or workspace_id
    meta["language"] = language.strip() or "tr"
    meta["keyword"] = keyword.strip() or DEFAULT_KEYWORD
    normalized_countries = _normalize_countries(countries)
    if not normalized_countries:
        raise WorkspaceValidationError("En az bir gecerli ulke kodu secilmeli.")
    meta["countries"] = normalized_countries
    meta["use_topic_mode"] = bool(use_topic_mode)

    clean_keywords: dict[str, str] = {}
    for key, value in (country_keywords or {}).items():
        country = str(key).strip().upper()
        keyword_value = str(value).strip()
        if country and keyword_value:
            clean_keywords[country] = keyword_value
    meta["country_keywords"] = clean_keywords
    meta["updated_at"] = _now_iso()
    _WORKSPACES[workspace_id] = meta
    return deepcopy(meta)


def delete_workspace(workspace_id: str) -> None:
    _WORKSPACES.pop(workspace_id, None)
    _DATASETS.pop(workspace_id, None)
    if get_default_workspace_id() == workspace_id:
        _SETTINGS.pop("default_workspace_id", None)


def load_workspace_dataset(workspace_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    cities_df, timeline_df = _DATASETS.get(workspace_id, (_empty_cities(), _empty_timeline()))
    cities = cities_df.copy()
    timeline = timeline_df.copy()

    for col in ["country", "city", "geo_code", "score"]:
        if col not in cities.columns:
            cities[col] = pd.NA
    for col in ["country", "city", "geo_code", "date", "score"]:
        if col not in timeline.columns:
            timeline[col] = pd.NA

    cities = cities[["country", "city", "geo_code", "score"]].reset_index(drop=True)
    timeline = timeline[["country", "city", "geo_code", "date", "score"]].reset_index(drop=True)
    cities["score"] = pd.to_numeric(cities["score"], errors="coerce").fillna(0)
    timeline["score"] = pd.to_numeric(timeline["score"], errors="coerce").fillna(0)
    timeline["date"] = pd.to_datetime(timeline["date"], errors="coerce")
    timeline = timeline.dropna(subset=["date"]).reset_index(drop=True)
    return cities, timeline


def set_workspace_dataset(workspace_id: str, cities_df: pd.DataFrame, timeline_df: pd.DataFrame) -> None:
    _DATASETS[workspace_id] = (cities_df.copy(), timeline_df.copy())
    meta = load_workspace_meta(workspace_id)
    meta["dataset_rows"] = int(len(cities_df) + len(timeline_df))
    meta["updated_at"] = _now_iso()
    _WORKSPACES[workspace_id] = meta


def save_workspace_dataset(
    workspace_id: str,
    cities_df: pd.DataFrame,
    timeline_df: pd.DataFrame,
) -> Path:
    existing_cities, existing_timeline = load_workspace_dataset(workspace_id)

    city_rows = cities_df.copy()
    timeline_rows = timeline_df.copy()

    for col in ["country", "city", "geo_code", "score"]:
        if col not in city_rows.columns:
            city_rows[col] = pd.NA
    for col in ["country", "city", "geo_code", "date", "score"]:
        if col not in timeline_rows.columns:
            timeline_rows[col] = pd.NA

    city_rows = city_rows[["country", "city", "geo_code", "score"]]
    city_rows["date"] = pd.NA
    city_rows["row_type"] = "city"

    timeline_rows = timeline_rows[["country", "city", "geo_code", "date", "score"]]
    timeline_rows["date"] = pd.to_datetime(timeline_rows["date"], errors="coerce")
    timeline_rows["date"] = timeline_rows["date"].dt.strftime("%Y-%m-%d")
    timeline_rows["row_type"] = "timeline"

    if not existing_cities.empty:
        existing_city_rows = existing_cities.copy()
        existing_city_rows["date"] = pd.NA
        existing_city_rows["row_type"] = "city"
        city_rows = pd.concat([existing_city_rows[["country", "city", "geo_code", "score", "date", "row_type"]], city_rows], ignore_index=True)
    if not existing_timeline.empty:
        existing_timeline_rows = existing_timeline.copy()
        existing_timeline_rows = existing_timeline_rows[["country", "city", "geo_code", "date", "score"]]
        existing_timeline_rows["date"] = pd.to_datetime(existing_timeline_rows["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        existing_timeline_rows["row_type"] = "timeline"
        timeline_rows = pd.concat([existing_timeline_rows[["country", "city", "geo_code", "date", "score", "row_type"]], timeline_rows], ignore_index=True)

    combined = pd.concat([city_rows, timeline_rows], ignore_index=True)
    combined = combined[["row_type", "country", "city", "geo_code", "date", "score"]]
    combined = combined.drop_duplicates(subset=["row_type", "country", "city", "geo_code", "date"], keep="last").reset_index(drop=True)

    city_subset = combined[combined["row_type"] == "city"][["country", "city", "geo_code", "score"]].copy()
    timeline_subset = combined[combined["row_type"] == "timeline"][["country", "city", "geo_code", "date", "score"]].copy()
    _DATASETS[workspace_id] = (city_subset, timeline_subset)

    meta = load_workspace_meta(workspace_id)
    meta["dataset_rows"] = int(len(combined))
    meta["updated_at"] = _now_iso()
    _WORKSPACES[workspace_id] = meta

    return Path(f"browser://{workspace_id}/dataset.csv")


def workspace_summary(workspace_id: str) -> dict[str, float | int]:
    cities, timeline = load_workspace_dataset(workspace_id)
    countries_count = int(cities["country"].nunique()) if not cities.empty else 0
    cities_count = int(cities["city"].nunique()) if not cities.empty else 0
    avg_score = 0.0
    if not timeline.empty:
        avg_score = float(pd.to_numeric(timeline["score"], errors="coerce").fillna(0).mean())
    return {
        "countries_count": countries_count,
        "cities_count": cities_count,
        "avg_score": avg_score,
    }


def ensure_default_workspace() -> dict[str, Any]:
    all_workspaces = list_workspaces()
    if all_workspaces:
        preferred = get_default_workspace_id()
        if preferred:
            for item in all_workspaces:
                if item["id"] == preferred:
                    return item
        return all_workspaces[0]

    created = create_workspace(
        name="Default Workspace",
        keyword=DEFAULT_KEYWORD,
        countries=list(TOP_20_COUNTRIES),
    )
    set_default_workspace(created["id"])
    return created
