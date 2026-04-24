from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from src.config import DATA_DIR, DEFAULT_KEYWORD, TOP_20_COUNTRIES

FILE_LOCK = threading.RLock()
WORKSPACES_DIR = DATA_DIR / "workspaces"
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_SCOPE = "anonymous"
_STATE_BY_SCOPE: dict[str, dict[str, Any]] = {}


class WorkspaceValidationError(ValueError):
    pass


def _slugify(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return clean or "workspace"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _scope_key(client_id: str | None) -> str:
    key = str(client_id or "").strip()
    return key or _DEFAULT_SCOPE


def _state_scope_key(client_id: str | None) -> str:
    scope = _scope_key(client_id)
    if WORKSPACES_DIR is None:
        return scope
    try:
        storage_root = str(Path(WORKSPACES_DIR).resolve())
    except Exception:
        storage_root = str(WORKSPACES_DIR)
    return f"{scope}::{storage_root}"


def _scope_state(client_id: str | None) -> dict[str, Any]:
    scope = _state_scope_key(client_id)
    state = _STATE_BY_SCOPE.get(scope)
    if state is None:
        state = {"workspaces": {}, "datasets": {}, "settings": {}}
        _STATE_BY_SCOPE[scope] = state
        _load_scope_state_from_disk(client_id, state)
    return state


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


def _path_component(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return clean or "workspace"


def _scope_dir(client_id: str | None) -> Path:
    return Path(WORKSPACES_DIR) / _path_component(_scope_key(client_id))


def _scope_state_path(client_id: str | None) -> Path:
    return _scope_dir(client_id) / "workspace_state.json"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _persist_scope_state(client_id: str | None) -> None:
    if WORKSPACES_DIR is None:
        return

    state = _scope_state(client_id)
    payload = {
        "scope": _scope_key(client_id),
        "updated_at": _now_iso(),
        "default_workspace_id": state["settings"].get("default_workspace_id"),
        "workspaces": [deepcopy(item) for item in state["workspaces"].values()],
    }
    _atomic_write_text(_scope_state_path(client_id), json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _load_scope_state_from_disk(client_id: str | None, state: dict[str, Any]) -> None:
    payload = _read_json_file(_scope_state_path(client_id))
    if not payload:
        return

    state["workspaces"].clear()
    state["datasets"].clear()
    state["settings"].clear()

    for item in payload.get("workspaces") or []:
        if not isinstance(item, dict):
            continue
        workspace_id = str(item.get("id") or item.get("workspace_id") or "").strip()
        if not workspace_id:
            continue
        state["workspaces"][workspace_id] = _normalize_meta(workspace_id, item)

    default_workspace_id = str(payload.get("default_workspace_id") or "").strip()
    if default_workspace_id:
        state["settings"]["default_workspace_id"] = default_workspace_id


def _path_component(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return clean or "workspace"


def _workspace_dataset_path(workspace_id: str, client_id: str | None = None) -> Path | None:
    if WORKSPACES_DIR is None:
        return None
    return Path(WORKSPACES_DIR) / _path_component(_scope_key(client_id)) / _path_component(workspace_id) / "dataset.csv"


def _load_dataset_frame_from_disk(workspace_id: str, client_id: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    dataset_path = _workspace_dataset_path(workspace_id, client_id=client_id)
    if dataset_path is None or not dataset_path.exists():
        return None

    try:
        raw = pd.read_csv(dataset_path)
    except Exception:
        return None

    if "row_type" not in raw.columns:
        return None

    city_cols = ["country", "city", "geo_code", "score"]
    timeline_cols = ["country", "city", "geo_code", "date", "score"]

    city_rows = raw[raw["row_type"].astype(str) == "city"].copy()
    timeline_rows = raw[raw["row_type"].astype(str) == "timeline"].copy()

    for col in city_cols:
        if col not in city_rows.columns:
            city_rows[col] = pd.NA
    for col in timeline_cols:
        if col not in timeline_rows.columns:
            timeline_rows[col] = pd.NA

    city_rows = city_rows[city_cols].reset_index(drop=True)
    timeline_rows = timeline_rows[timeline_cols].reset_index(drop=True)
    return city_rows, timeline_rows


def reset_memory_state(client_id: str | None = None) -> None:
    with FILE_LOCK:
        if client_id is None:
            _STATE_BY_SCOPE.clear()
            return
        _STATE_BY_SCOPE.pop(_scope_key(client_id), None)


def export_memory_state(client_id: str | None = None) -> dict[str, Any]:
    with FILE_LOCK:
        if client_id is not None:
            state = _scope_state(client_id)
            for workspace_id in list(state["workspaces"].keys()):
                load_workspace_dataset(workspace_id, client_id=client_id)
            return {
                "workspaces": [deepcopy(item) for item in state["workspaces"].values()],
                "default_workspace_id": state["settings"].get("default_workspace_id"),
                "datasets": {
                    workspace_id: {
                        "cities": cities_df.to_dict(orient="records"),
                        "timeline": timeline_df.to_dict(orient="records"),
                    }
                    for workspace_id, (cities_df, timeline_df) in state["datasets"].items()
                },
            }

        return {
            scope: {
                "workspaces": [deepcopy(item) for item in state["workspaces"].values()],
                "default_workspace_id": state["settings"].get("default_workspace_id"),
                "datasets": {
                    workspace_id: {
                        "cities": cities_df.to_dict(orient="records"),
                        "timeline": timeline_df.to_dict(orient="records"),
                    }
                    for workspace_id, (cities_df, timeline_df) in state["datasets"].items()
                },
            }
            for scope, state in _STATE_BY_SCOPE.items()
        }


def import_memory_state(payload: dict[str, Any] | None, client_id: str | None = None) -> None:
    if not isinstance(payload, dict):
        return

    with FILE_LOCK:
        if client_id is None:
            _STATE_BY_SCOPE.clear()
        state = _scope_state(client_id)
        state["workspaces"].clear()
        state["datasets"].clear()
        state["settings"].clear()

        for item in payload.get("workspaces") or []:
            if not isinstance(item, dict):
                continue
            workspace_id = str(item.get("id") or item.get("workspace_id") or "").strip()
            if not workspace_id:
                continue
            state["workspaces"][workspace_id] = _normalize_meta(workspace_id, item)

        default_workspace_id = str(payload.get("default_workspace_id") or "").strip()
        if default_workspace_id:
            state["settings"]["default_workspace_id"] = default_workspace_id

        datasets = payload.get("datasets") or {}
        if isinstance(datasets, dict):
            for workspace_id, dataset in datasets.items():
                if not isinstance(dataset, dict):
                    continue
                cities_df = pd.DataFrame(dataset.get("cities") or [])
                timeline_df = pd.DataFrame(dataset.get("timeline") or [])
                state["datasets"][str(workspace_id).strip()] = (cities_df, timeline_df)

        _persist_scope_state(client_id)


def get_default_workspace_id(client_id: str | None = None) -> str | None:
    value = _scope_state(client_id)["settings"].get("default_workspace_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_default_workspace(workspace_id: str, client_id: str | None = None) -> None:
    _scope_state(client_id)["settings"]["default_workspace_id"] = workspace_id
    _persist_scope_state(client_id)


def list_workspaces(client_id: str | None = None) -> list[dict[str, Any]]:
    state = _scope_state(client_id)
    return sorted([deepcopy(item) for item in state["workspaces"].values()], key=lambda item: item.get("updated_at", ""), reverse=True)


def load_workspace_meta(workspace_id: str, client_id: str | None = None) -> dict[str, Any]:
    state = _scope_state(client_id)
    meta = state["workspaces"].get(workspace_id)
    if meta is None:
        meta = _default_meta(workspace_id)
    cities_df, timeline_df = state["datasets"].get(workspace_id, (_empty_cities(), _empty_timeline()))
    meta = _normalize_meta(workspace_id, meta)
    meta["dataset_rows"] = int(len(cities_df) + len(timeline_df))
    state["workspaces"][workspace_id] = meta
    return deepcopy(meta)


def create_workspace(name: str, keyword: str, countries: list[str], client_id: str | None = None) -> dict[str, Any]:
    state = _scope_state(client_id)
    stem = _slugify(name)
    workspace_id = stem
    index = 1
    while workspace_id in state["workspaces"]:
        workspace_id = f"{stem}_{index:02d}"
        index += 1

    meta = _default_meta(workspace_id, name=name.strip() or workspace_id)
    meta["keyword"] = keyword.strip() or DEFAULT_KEYWORD
    normalized_countries = _normalize_countries(countries)
    if not normalized_countries:
        raise WorkspaceValidationError("En az bir gecerli ulke kodu secilmeli.")
    meta["countries"] = normalized_countries
    meta["updated_at"] = _now_iso()
    state["workspaces"][workspace_id] = meta
    _persist_scope_state(client_id)
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
    client_id: str | None = None,
) -> dict[str, Any]:
    state = _scope_state(client_id)
    meta = load_workspace_meta(workspace_id, client_id=client_id)
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
    state["workspaces"][workspace_id] = meta
    _persist_scope_state(client_id)
    return deepcopy(meta)


def delete_workspace(workspace_id: str, client_id: str | None = None) -> None:
    state = _scope_state(client_id)
    state["workspaces"].pop(workspace_id, None)
    state["datasets"].pop(workspace_id, None)
    if get_default_workspace_id(client_id) == workspace_id:
        state["settings"].pop("default_workspace_id", None)

    dataset_path = _workspace_dataset_path(workspace_id, client_id=client_id)
    if dataset_path is not None:
        try:
            if dataset_path.exists():
                dataset_path.unlink()
            parent = dataset_path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass

    _persist_scope_state(client_id)


def load_workspace_dataset(workspace_id: str, client_id: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    state = _scope_state(client_id)
    cities_df, timeline_df = state["datasets"].get(workspace_id, (_empty_cities(), _empty_timeline()))
    if (cities_df.empty and timeline_df.empty) or workspace_id not in state["datasets"]:
        disk_dataset = _load_dataset_frame_from_disk(workspace_id, client_id=client_id)
        if disk_dataset is not None:
            cities_df, timeline_df = disk_dataset
            state["datasets"][workspace_id] = (cities_df.copy(), timeline_df.copy())
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


def set_workspace_dataset(workspace_id: str, cities_df: pd.DataFrame, timeline_df: pd.DataFrame, client_id: str | None = None) -> None:
    state = _scope_state(client_id)
    state["datasets"][workspace_id] = (cities_df.copy(), timeline_df.copy())
    meta = load_workspace_meta(workspace_id, client_id=client_id)
    meta["dataset_rows"] = int(len(cities_df) + len(timeline_df))
    meta["updated_at"] = _now_iso()
    state["workspaces"][workspace_id] = meta
    _persist_scope_state(client_id)


def save_workspace_dataset(
    workspace_id: str,
    cities_df: pd.DataFrame,
    timeline_df: pd.DataFrame,
    client_id: str | None = None,
) -> Path:
    existing_cities, existing_timeline = load_workspace_dataset(workspace_id, client_id=client_id)

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
    state = _scope_state(client_id)
    state["datasets"][workspace_id] = (city_subset, timeline_subset)

    meta = load_workspace_meta(workspace_id, client_id=client_id)
    meta["dataset_rows"] = int(len(combined))
    meta["updated_at"] = _now_iso()
    state["workspaces"][workspace_id] = meta

    dataset_path = _workspace_dataset_path(workspace_id, client_id=client_id)
    if dataset_path is not None:
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_csv(dataset_path, index=False)
        _persist_scope_state(client_id)
        return dataset_path

    _persist_scope_state(client_id)

    return Path(f"browser://{workspace_id}/dataset.csv")


def workspace_summary(workspace_id: str, client_id: str | None = None) -> dict[str, float | int]:
    cities, timeline = load_workspace_dataset(workspace_id, client_id=client_id)
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


def ensure_default_workspace(client_id: str | None = None) -> dict[str, Any]:
    all_workspaces = list_workspaces(client_id)
    if all_workspaces:
        preferred = get_default_workspace_id(client_id)
        if preferred:
            for item in all_workspaces:
                if item["id"] == preferred:
                    return item
        return all_workspaces[0]

    created = create_workspace(
        name="Default Workspace",
        keyword=DEFAULT_KEYWORD,
        countries=list(TOP_20_COUNTRIES),
        client_id=client_id,
    )
    set_default_workspace(created["id"], client_id=client_id)
    return created
