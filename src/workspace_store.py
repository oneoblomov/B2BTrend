from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import DATA_DIR, DEFAULT_KEYWORD, TOP_20_COUNTRIES

WORKSPACES_DIR = DATA_DIR / "workspaces"
DATASET_FILE = "dataset.csv"
META_FILE = "metadata.json"
SETTINGS_FILE = WORKSPACES_DIR / "settings.json"


def _slugify(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return clean or "workspace"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _workspace_dir(workspace_id: str) -> Path:
    return WORKSPACES_DIR / workspace_id


def workspace_dataset_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / DATASET_FILE


def workspace_meta_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / META_FILE


def _normalize_countries(countries: list[str] | None) -> list[str]:
    values = [str(item).strip().upper() for item in (countries or []) if str(item).strip()]
    unique_sorted = sorted(set(values))
    return unique_sorted or list(TOP_20_COUNTRIES)


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


def _read_settings() -> dict[str, Any]:
    payload = _read_json(SETTINGS_FILE)
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_settings(payload: dict[str, Any]) -> None:
    _write_json(SETTINGS_FILE, payload)


def get_default_workspace_id() -> str | None:
    payload = _read_settings()
    value = payload.get("default_workspace_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_default_workspace(workspace_id: str) -> None:
    payload = _read_settings()
    payload["default_workspace_id"] = workspace_id
    _write_settings(payload)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _normalize_meta(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _default_meta(workspace_id, name=payload.get("name"))
    base["language"] = str(payload.get("language") or "tr").strip() or "tr"
    base["keyword"] = str(payload.get("keyword") or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
    base["countries"] = _normalize_countries(payload.get("countries"))
    base["use_topic_mode"] = bool(payload.get("use_topic_mode", False))

    raw_country_keywords = payload.get("country_keywords") or {}
    clean_keywords: dict[str, str] = {}
    if isinstance(raw_country_keywords, dict):
        for k, v in raw_country_keywords.items():
            key = str(k).strip().upper()
            value = str(v).strip()
            if key and value:
                clean_keywords[key] = value
    base["country_keywords"] = clean_keywords

    created_at = payload.get("created_at") or base["created_at"]
    updated_at = payload.get("updated_at") or base["updated_at"]
    base["created_at"] = str(created_at)
    base["updated_at"] = str(updated_at)

    dataset_rows = payload.get("dataset_rows", 0)
    try:
        base["dataset_rows"] = max(0, int(dataset_rows))
    except Exception:
        base["dataset_rows"] = 0

    base["id"] = workspace_id
    base["name"] = str(payload.get("name") or workspace_id).strip() or workspace_id
    return base


def _migrate_legacy_workspace(workspace_id: str) -> None:
    workspace_dir = _workspace_dir(workspace_id)
    dataset_path = workspace_dataset_path(workspace_id)

    if dataset_path.exists():
        return

    legacy_cities = workspace_dir / "cities.csv"
    legacy_timeline = workspace_dir / "timeline.csv"
    if not legacy_cities.exists() and not legacy_timeline.exists():
        return

    cities_df = pd.DataFrame(columns=["country", "city", "geo_code", "score"])
    timeline_df = pd.DataFrame(columns=["country", "city", "geo_code", "date", "score"])

    if legacy_cities.exists():
        try:
            cities_df = pd.read_csv(legacy_cities)
        except Exception:
            pass
    if legacy_timeline.exists():
        try:
            timeline_df = pd.read_csv(legacy_timeline)
        except Exception:
            pass

    save_workspace_dataset(workspace_id, cities_df, timeline_df)


def list_workspaces() -> list[dict[str, Any]]:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for child in sorted([item for item in WORKSPACES_DIR.iterdir() if item.is_dir()]):
        ws_id = child.name
        _migrate_legacy_workspace(ws_id)
        meta = load_workspace_meta(ws_id)
        records.append(meta)
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def load_workspace_meta(workspace_id: str) -> dict[str, Any]:
    raw = _read_json(workspace_meta_path(workspace_id))
    normalized = _normalize_meta(workspace_id, raw)

    data_path = workspace_dataset_path(workspace_id)
    if data_path.exists() and normalized["dataset_rows"] == 0:
        try:
            normalized["dataset_rows"] = int(pd.read_csv(data_path).shape[0])
        except Exception:
            normalized["dataset_rows"] = 0

    _write_json(workspace_meta_path(workspace_id), normalized)
    return normalized


def create_workspace(name: str, keyword: str, countries: list[str]) -> dict[str, Any]:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

    stem = _slugify(name)
    workspace_id = stem
    index = 1
    while _workspace_dir(workspace_id).exists():
        workspace_id = f"{stem}_{index:02d}"
        index += 1

    meta = _default_meta(workspace_id, name=name.strip() or workspace_id)
    meta["keyword"] = keyword.strip() or DEFAULT_KEYWORD
    meta["countries"] = _normalize_countries(countries)
    meta["updated_at"] = _now_iso()

    _workspace_dir(workspace_id).mkdir(parents=True, exist_ok=True)
    _write_json(workspace_meta_path(workspace_id), meta)
    return meta


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
    meta["countries"] = _normalize_countries(countries)
    meta["use_topic_mode"] = bool(use_topic_mode)

    clean_keywords: dict[str, str] = {}
    for k, v in (country_keywords or {}).items():
        key = str(k).strip().upper()
        value = str(v).strip()
        if key and value:
            clean_keywords[key] = value
    meta["country_keywords"] = clean_keywords

    meta["updated_at"] = _now_iso()
    _write_json(workspace_meta_path(workspace_id), meta)
    return meta


def delete_workspace(workspace_id: str) -> None:
    ws_dir = _workspace_dir(workspace_id)
    if ws_dir.exists() and ws_dir.is_dir():
        for child in sorted(ws_dir.glob("**/*"), key=lambda p: len(p.parts), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
        try:
            ws_dir.rmdir()
        except OSError:
            pass

    if get_default_workspace_id() == workspace_id:
        payload = _read_settings()
        payload.pop("default_workspace_id", None)
        _write_settings(payload)


def _empty_cities() -> pd.DataFrame:
    return pd.DataFrame(columns=["country", "city", "geo_code", "score"])


def _empty_timeline() -> pd.DataFrame:
    return pd.DataFrame(columns=["country", "city", "geo_code", "date", "score"])


def load_workspace_dataset(workspace_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    data_path = workspace_dataset_path(workspace_id)
    if not data_path.exists():
        return _empty_cities(), _empty_timeline()

    try:
        df = pd.read_csv(data_path)
    except Exception:
        return _empty_cities(), _empty_timeline()

    if df.empty:
        return _empty_cities(), _empty_timeline()

    if "row_type" not in df.columns:
        return _empty_cities(), _empty_timeline()

    cities = df[df["row_type"] == "city"].copy()
    timeline = df[df["row_type"] == "timeline"].copy()

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


def save_workspace_dataset(
    workspace_id: str,
    cities_df: pd.DataFrame,
    timeline_df: pd.DataFrame,
) -> Path:
    workspace_dir = _workspace_dir(workspace_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)

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

    combined = pd.concat([city_rows, timeline_rows], ignore_index=True)
    combined = combined[["row_type", "country", "city", "geo_code", "date", "score"]]
    dataset_path = workspace_dataset_path(workspace_id)
    combined.to_csv(dataset_path, index=False)

    meta = load_workspace_meta(workspace_id)
    meta["dataset_rows"] = int(len(combined))
    meta["updated_at"] = _now_iso()
    _write_json(workspace_meta_path(workspace_id), meta)

    return dataset_path


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
