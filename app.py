from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
from datetime import date
from pathlib import Path

import jinja2
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import pycountry
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from geopy.geocoders import Nominatim
from plotly.subplots import make_subplots
from pydantic import BaseModel, Field

from src.analytics import (
    advanced_forecast,
    best_ad_hours_text,
    clean_city_outliers,
    compute_correlation_matrix,
    compute_moving_averages,
    compute_trend_scores,
    country_ranking,
    detect_change_points,
    detect_local_extrema,
    detect_spikes,
    hourly_analysis,
    location_trend_ranking,
    recommendation_from_signal,
    robust_trend_signal,
    rolling_volatility,
    stl_decompose,
    trend_strength_meter,
)
from src.config import ALL_COUNTRIES, DEFAULT_KEYWORD, GEOCACHE_FILE, TOP_20_COUNTRIES, USER_AGENT
from src.fetch_job_store import FetchJobStore, JobConflictError
from src.fetch_resume_store import (
    build_fetch_fingerprint,
    clear_checkpoint,
    is_checkpoint_compatible,
    load_checkpoint,
    save_checkpoint,
)
from src.reports import export_csv
from src.trend_fetcher import (
    FetchConfig,
    FetchCancelledError,
    _build_client,
    clear_cache,
    fetch_hourly_data,
    fetch_related_queries,
    fetch_related_topics,
    fetch_timeline,
    fetch_trends_dataset,
    fetch_trends_dataset_country_keywords,
    save_snapshot,
)
from src.workspace_store import (
    WorkspaceValidationError,
    create_workspace,
    delete_workspace,
    ensure_default_workspace,
    get_default_workspace_id,
    list_workspaces,
    load_workspace_dataset,
    load_workspace_meta,
    save_workspace_dataset,
    set_default_workspace,
    update_workspace,
    workspace_summary,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="B2BTrend", version="4.2.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
    cache_size=0,
)
templates = Jinja2Templates(env=jinja_env)
logger = logging.getLogger(__name__)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


class WorkspaceCreate(BaseModel):
    name: str
    keyword: str = DEFAULT_KEYWORD
    countries: list[str] = Field(default_factory=lambda: list(TOP_20_COUNTRIES))
    language: str = "tr"
    use_topic_mode: bool = False
    country_keywords: dict[str, str] = Field(default_factory=dict)


class WorkspaceUpdate(BaseModel):
    name: str | None = None
    keyword: str | None = None
    countries: list[str] | None = None
    language: str | None = None
    use_topic_mode: bool | None = None
    country_keywords: dict[str, str] | None = None
    is_default: bool | None = None


class FetchRequest(BaseModel):
    workspace_id: str
    resume: bool = False


class FetchCancelRequest(BaseModel):
    workspace_id: str | None = None


class CityTimelineFetchRequest(BaseModel):
    workspace_id: str
    country: str
    city: str
    geo_code: str | None = None


class WsHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        stale: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


hub = WsHub()
fetch_job_store = FetchJobStore()


def _schedule_broadcast(loop: asyncio.AbstractEventLoop, payload: dict) -> None:
    try:
        asyncio.run_coroutine_threadsafe(hub.broadcast(payload), loop)
    except Exception:
        logger.exception("Broadcast scheduling failed: %s", payload.get("type") if isinstance(payload, dict) else "unknown")


def _language_to_hl(language: str) -> str:
    code = str(language or "tr").strip().lower() or "tr"
    if code == "tr":
        return "tr-TR"
    if len(code) == 2:
        return f"{code}-{code.upper()}"
    return "en-US"


def _job_update(job_id: str, loop: asyncio.AbstractEventLoop, payload: dict) -> dict | None:
    snapshot = fetch_job_store.snapshot()
    active = snapshot.get("active") or {}
    incoming_status = str((payload or {}).get("status") or "")
    if active.get("job_id") == job_id and active.get("status") == "cancelling" and incoming_status == "running":
        return active
    updated = fetch_job_store.update_job(job_id, **payload)
    if updated:
        _schedule_broadcast(loop, {"type": "fetch_job_update", "state": updated})
    return updated


def _is_job_cancel_requested(job_id: str) -> bool:
    snapshot = fetch_job_store.snapshot()
    active = snapshot.get("active") or snapshot.get("latest") or {}
    return bool(
        active
        and active.get("job_id") == job_id
        and (active.get("cancel_requested") or active.get("status") in {"cancelling", "cancelled"})
    )


def _run_fetch_job(
    job_id: str,
    loop: asyncio.AbstractEventLoop,
    *,
    workspace_id: str,
    keyword: str,
    countries: list[str],
    language: str,
    use_topic_mode: bool,
    country_keywords: dict[str, str],
    resume_requested: bool,
) -> None:
    logger.info(
        "Fetch job started: workspace=%s countries=%s topic_mode=%s keyword=%s language=%s resume=%s",
        workspace_id,
        len(countries),
        use_topic_mode,
        keyword,
        language,
        resume_requested,
    )

    fetch_fingerprint = build_fetch_fingerprint(
        keyword=keyword,
        countries=countries,
        use_topic_mode=use_topic_mode,
        language=language,
        country_keywords=country_keywords,
    )

    resume_checkpoint = None
    resume_state: dict | None = None
    seed_cities = None
    seed_timeline = None

    if resume_requested:
        candidate = load_checkpoint(workspace_id)
        if is_checkpoint_compatible(candidate, fetch_fingerprint):
            resume_checkpoint = candidate
            state = candidate.get("resume_state")
            if isinstance(state, dict):
                resume_state = state
            seed_cities, seed_timeline = load_workspace_dataset(workspace_id)
        else:
            clear_checkpoint(workspace_id)
    else:
        clear_checkpoint(workspace_id)

    resume_enabled = bool(resume_checkpoint and isinstance(resume_state, dict))

    hl = _language_to_hl(language)
    cfg = FetchConfig(
        keyword=keyword,
        hl=hl,
        retries=3,
        backoff_factor=0.6,
        max_attempt_per_country=4,
        top_cities_per_country=10,
        min_sleep_sec=max(4.0, 4.0),
        max_sleep_sec=max(11.0, 9.0),
        user_agent=USER_AGENT,  # Env'den sabit veya None (rastgele)
    )

    last_resume_state: dict = dict(resume_state or {})

    def persist_resume_checkpoint(status: str, phase: str, message: str = "", payload: dict | None = None) -> None:
        nonlocal last_resume_state
        if isinstance(payload, dict):
            last_resume_state = dict(payload)

        save_checkpoint(
            workspace_id,
            {
                "job_id": job_id,
                "fingerprint": fetch_fingerprint,
                "status": status,
                "phase": phase,
                "message": message,
                "resume_state": dict(last_resume_state),
                "keyword": keyword,
                "countries": list(countries),
                "language": language,
                "use_topic_mode": bool(use_topic_mode),
                "country_keywords": dict(country_keywords),
            },
        )

    start_message = "Veri cekimi arkaplanda basladi"
    if resume_requested and resume_enabled:
        start_message = "Veri cekimi kaldigi yerden devam ediyor"
    elif resume_requested and not resume_enabled:
        start_message = "Devam verisi bulunamadi, veri cekimi sifirdan basladi"

    _job_update(
        job_id,
        loop,
        {
            "status": "running",
            "phase": "prepare",
            "message": start_message,
            "progress": 0.02,
            "completed": 0,
            "total": max(1, len(countries) * 2),
            "resume_requested": resume_requested,
            "resume_enabled": resume_enabled,
        },
    )

    persist_resume_checkpoint(
        status="running",
        phase="prepare",
        message=start_message,
        payload=resume_state,
    )

    def progress_callback(payload: dict) -> None:
        data = dict(payload or {})
        data.setdefault("status", "running")
        data.setdefault("phase", "running")
        if data.get("phase") in {"city_list", "city_timeline", "country_timeline", "finalize"}:
            logger.info(
                "Fetch progress: job=%s phase=%s country=%s city=%s progress=%s message=%s",
                job_id,
                data.get("phase"),
                data.get("country"),
                data.get("city"),
                data.get("progress"),
                data.get("message"),
            )
        _job_update(job_id, loop, data)

    def checkpoint_callback(payload: dict) -> None:
        phase = str(payload.get("phase") or "running")
        message = str(payload.get("message") or "")
        persist_resume_checkpoint(status="running", phase=phase, message=message, payload=payload)

    def partial_save_callback(cities_df: pd.DataFrame, timeline_df: pd.DataFrame, payload: dict) -> None:
        save_workspace_dataset(workspace_id, cities_df, timeline_df)
        checkpoint_callback(payload)

    try:
        if use_topic_mode:
            cities, timeline = fetch_trends_dataset(
                countries,
                cfg,
                progress_callback=progress_callback,
                cancel_callback=lambda: _is_job_cancel_requested(job_id),
                seed_cities=seed_cities,
                seed_timeline=seed_timeline,
                resume_state=resume_state,
                checkpoint_callback=checkpoint_callback,
                partial_save_callback=partial_save_callback,
            )
        else:
            cities, timeline = fetch_trends_dataset_country_keywords(
                countries,
                country_keywords,
                keyword,
                cfg,
                progress_callback=progress_callback,
                cancel_callback=lambda: _is_job_cancel_requested(job_id),
                seed_cities=seed_cities,
                seed_timeline=seed_timeline,
                resume_state=resume_state,
                checkpoint_callback=checkpoint_callback,
                partial_save_callback=partial_save_callback,
            )

        if _is_job_cancel_requested(job_id):
            raise FetchCancelledError("Fetch cancelled")

        if cities.empty or timeline.empty:
            logger.warning(
                "Fetch completed with empty dataset: workspace=%s job=%s cities=%s timeline=%s keyword=%s countries=%s topic_mode=%s",
                workspace_id,
                job_id,
                len(cities),
                len(timeline),
                keyword,
                use_topic_mode,
            )
            save_workspace_dataset(workspace_id, cities, timeline)
            try:
                save_snapshot(cities, timeline, keyword=keyword)
            except Exception:
                logger.exception("Snapshot save failed for empty dataset: workspace=%s job=%s", workspace_id, job_id)

            clear_checkpoint(workspace_id)

            final = fetch_job_store.finish_job(
                job_id,
                status="completed",
                message="Veri cekimi tamamlandi (veri yok).",
                result={"cities": int(len(cities)), "timeline": int(len(timeline))},
            )
            if final:
                _schedule_broadcast(loop, {"type": "fetch_job_update", "state": final})
                _schedule_broadcast(loop, {"type": "fetch_done", "state": final})
            return

        save_workspace_dataset(workspace_id, cities, timeline)
        try:
            save_snapshot(cities, timeline, keyword=keyword)
        except Exception:
            logger.exception("Snapshot save failed: workspace=%s job=%s", workspace_id, job_id)

        clear_checkpoint(workspace_id)

        final = fetch_job_store.finish_job(
            job_id,
            status="completed",
            message=f"Veri cekimi tamamlandi: {len(cities)} sehir, {len(timeline)} satir",
            result={"cities": int(len(cities)), "timeline": int(len(timeline))},
        )
        if final:
            _schedule_broadcast(loop, {"type": "fetch_job_update", "state": final})
            _schedule_broadcast(loop, {"type": "fetch_done", "state": final})
    except FetchCancelledError:
        logger.info("Fetch job cancelled: workspace=%s job=%s", workspace_id, job_id)
        final = fetch_job_store.update_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            message="Veri cekimi iptal edildi",
        )
        persist_resume_checkpoint(
            status="cancelled",
            phase="cancelled",
            message="Veri cekimi iptal edildi",
            payload=last_resume_state,
        )
        if final:
            _schedule_broadcast(loop, {"type": "fetch_job_update", "state": final})
            _schedule_broadcast(loop, {"type": "fetch_cancelled", "state": final})
    except Exception as exc:
        logger.exception("Fetch job failed: workspace=%s job=%s", workspace_id, job_id)
        message = f"Veri cekimi basarisiz: {exc}"
        persist_resume_checkpoint(
            status="failed",
            phase="failed",
            message=message,
            payload=last_resume_state,
        )
        final = fetch_job_store.finish_job(
            job_id,
            status="failed",
            message=message,
            error=str(exc),
        )
        if final:
            _schedule_broadcast(loop, {"type": "fetch_job_update", "state": final})
            _schedule_broadcast(loop, {"type": "fetch_failed", "state": final})


def _download_city_timeline_for_workspace(
    workspace_id: str,
    country: str,
    city: str,
    geo_code: str | None = None,
) -> dict:
    snapshot = fetch_job_store.snapshot()
    active = snapshot.get("active") or {}
    if active and active.get("status") in {"queued", "running", "cancelling"}:
        raise HTTPException(status_code=409, detail="Arkaplanda calisan bir veri cekimi varken manuel sehir cekimi yapilamaz.")

    workspace_key = str(workspace_id or "").strip()
    country_key = str(country or "").strip().upper()
    city_key = str(city or "").strip()
    geo_key = str(geo_code or "").strip()

    if not workspace_key or not country_key or not city_key:
        raise HTTPException(status_code=422, detail="workspace_id, country ve city zorunludur.")

    meta = load_workspace_meta(workspace_key)
    cities_df, timeline_df = load_workspace_dataset(workspace_key)
    if cities_df.empty:
        raise HTTPException(status_code=409, detail="Once otomatik sehir skorlarini indirin.")

    city_rows = cities_df[cities_df["country"].astype(str).str.strip().str.upper() == country_key].copy()
    city_rows["city_norm"] = city_rows["city"].fillna("").astype(str).str.strip().str.casefold()
    city_rows["geo_norm"] = city_rows["geo_code"].fillna("").astype(str).str.strip()

    resolved_city_rows = city_rows[city_rows["city_norm"] == city_key.casefold()].copy()
    if geo_key:
        geo_rows = city_rows[city_rows["geo_norm"] == geo_key].copy()
        if not geo_rows.empty:
            resolved_city_rows = geo_rows

    if resolved_city_rows.empty:
        raise HTTPException(status_code=404, detail="Sehir bulunamadi.")

    resolved_row = resolved_city_rows.iloc[0]
    resolved_geo = str(resolved_row.get("geo_code", "")).strip() or geo_key
    resolved_city = str(resolved_row.get("city", city_key)).strip() or city_key
    if not resolved_geo:
        raise HTTPException(status_code=404, detail="Sehir icin geo_code bulunamadi.")

    existing = timeline_df[
        (timeline_df["country"].astype(str).str.strip().str.upper() == country_key)
        & (
            (timeline_df["geo_code"].astype(str).str.strip() == resolved_geo)
            | (timeline_df["city"].fillna("").astype(str).str.strip().str.casefold() == resolved_city.casefold())
        )
    ]
    if not existing.empty:
        return {
            "ok": True,
            "status": "skipped",
            "message": "Sehir zaman serisi zaten kayitli.",
            "workspace_id": workspace_key,
            "country": country_key,
            "city": resolved_city,
            "geo_code": resolved_geo,
            "timeline_rows": int(len(existing)),
        }

    keyword = str(meta.get("keyword") or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
    language = str(meta.get("language") or "tr").strip() or "tr"
    cfg = FetchConfig(keyword=keyword, hl=_language_to_hl(language))
    client = _build_client(cfg)
    city_timeline = fetch_timeline(client, resolved_geo, cfg, phase="city_timeline")
    if city_timeline.empty:
        return {
            "ok": False,
            "status": "empty",
            "message": "Sehir zaman serisi bulunamadi.",
            "workspace_id": workspace_key,
            "country": country_key,
            "city": resolved_city,
            "geo_code": resolved_geo,
        }

    city_timeline = city_timeline.copy()
    city_timeline["country"] = country_key
    city_timeline["city"] = resolved_city
    city_timeline["geo_code"] = resolved_geo
    merged_timeline = pd.concat([timeline_df, city_timeline[["country", "city", "geo_code", "date", "score"]]], ignore_index=True)

    save_workspace_dataset(workspace_key, cities_df, merged_timeline)
    try:
        save_snapshot(cities_df, merged_timeline, keyword=keyword)
    except Exception:
        logger.exception("Snapshot save failed for manual city timeline: workspace=%s country=%s city=%s", workspace_key, country_key, resolved_city)

    return {
        "ok": True,
        "status": "completed",
        "message": "Sehir zaman serisi kaydedildi.",
        "workspace_id": workspace_key,
        "country": country_key,
        "city": resolved_city,
        "geo_code": resolved_geo,
        "timeline_rows": int(len(city_timeline)),
    }


def _fig_to_dict(fig: go.Figure) -> dict:
    return json.loads(pio.to_json(fig))


def _country_iso3(iso2: str) -> str:
    country = pycountry.countries.get(alpha_2=str(iso2).upper())
    return country.alpha_3 if country else str(iso2).upper()


def _country_name(iso2: str) -> str:
    country = pycountry.countries.get(alpha_2=str(iso2).upper())
    return country.name if country else str(iso2).upper()


def _clean_country_keywords(mapping: dict[str, str], allowed: list[str]) -> dict[str, str]:
    allowed_set = {item.upper() for item in allowed}
    out: dict[str, str] = {}
    for k, v in mapping.items():
        ck = str(k).strip().upper()
        cv = str(v).strip()
        if ck in allowed_set and cv:
            out[ck] = cv
    return out


def _workspace_payload(meta: dict) -> dict:
    summary = workspace_summary(meta["id"])
    return {
        "id": meta["id"],
        "name": meta.get("name", meta["id"]),
        "keyword": meta.get("keyword", DEFAULT_KEYWORD),
        "language": meta.get("language", "tr"),
        "countries": meta.get("countries", []),
        "use_topic_mode": bool(meta.get("use_topic_mode", False)),
        "country_keywords": meta.get("country_keywords", {}),
        "dataset_rows": int(meta.get("dataset_rows", 0)),
        "updated_at": meta.get("updated_at", ""),
        "is_default": meta["id"] == get_default_workspace_id(),
        "stats": summary,
    }


def _filter_timeline(timeline: pd.DataFrame, date_range: str, start: date | None, end: date | None) -> pd.DataFrame:
    if timeline.empty:
        return timeline

    out = timeline.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"])
    if out.empty:
        return out

    max_date = out["date"].max()
    if date_range == "1m":
        out = out[out["date"] >= max_date - pd.Timedelta(days=30)]
    elif date_range == "3m":
        out = out[out["date"] >= max_date - pd.Timedelta(days=90)]
    elif date_range == "6m":
        out = out[out["date"] >= max_date - pd.Timedelta(days=180)]
    elif date_range == "custom" and start and end:
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]

    return out.sort_values("date").reset_index(drop=True)


def _normalize_geocode_key(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_geocache() -> pd.DataFrame:
    if GEOCACHE_FILE.exists():
        cache = pd.read_csv(GEOCACHE_FILE, dtype={"country": str, "city": str, "lat": str, "lon": str})
        for col in ["country", "city", "lat", "lon"]:
            if col not in cache.columns:
                cache[col] = pd.NA
        cache["country"] = cache["country"].fillna("").astype(str).map(_normalize_geocode_key)
        cache["city"] = cache["city"].fillna("").astype(str).map(_normalize_geocode_key)
        cache["lat"] = pd.to_numeric(cache["lat"], errors="coerce")
        cache["lon"] = pd.to_numeric(cache["lon"], errors="coerce")
        return cache[["country", "city", "lat", "lon"]]

    return pd.DataFrame({
        "country": pd.Series(dtype="string"),
        "city": pd.Series(dtype="string"),
        "lat": pd.Series(dtype="float64"),
        "lon": pd.Series(dtype="float64"),
    })


def _save_geocache(df: pd.DataFrame) -> None:
    cache = df.copy()
    cache["country"] = cache["country"].fillna("").astype(str).map(_normalize_geocode_key)
    cache["city"] = cache["city"].fillna("").astype(str).map(_normalize_geocode_key)
    cache["lat"] = pd.to_numeric(cache["lat"], errors="coerce")
    cache["lon"] = pd.to_numeric(cache["lon"], errors="coerce")
    GEOCACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache.to_csv(GEOCACHE_FILE, index=False)


def _geocode_cities(df: pd.DataFrame) -> pd.DataFrame:
    cache = _load_geocache()
    work = df[["country", "city"]].drop_duplicates().copy()
    work["country"] = work["country"].fillna("").astype(str).map(_normalize_geocode_key)
    work["city"] = work["city"].fillna("").astype(str).map(_normalize_geocode_key)

    cache_keys = cache[["country", "city"]].drop_duplicates()
    merged = work.merge(cache, on=["country", "city"], how="left")

    missing = work.merge(cache_keys, on=["country", "city"], how="left", indicator=True)
    missing = missing[missing["_merge"] == "left_only"][["country", "city"]].drop_duplicates()
    missing = missing[(missing["country"] != "") & (missing["city"] != "")]
    if missing.empty:
        return merged

    geolocator = Nominatim(user_agent="b2btrend-fastapi", timeout=8)
    new_rows: list[dict] = []
    for _, row in missing.iterrows():
        query = f"{row['city']}, {row['country']}"
        lat, lon = None, None
        try:
            loc = geolocator.geocode(query, timeout=8)
            if loc:
                lat, lon = loc.latitude, loc.longitude
        except Exception:
            pass
        new_rows.append({"country": row["country"], "city": row["city"], "lat": lat, "lon": lon})

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        new_df["country"] = new_df["country"].fillna("").astype(str).map(_normalize_geocode_key)
        new_df["city"] = new_df["city"].fillna("").astype(str).map(_normalize_geocode_key)
        new_df["lat"] = pd.to_numeric(new_df["lat"], errors="coerce")
        new_df["lon"] = pd.to_numeric(new_df["lon"], errors="coerce")
        cache = pd.concat([cache, new_df[["country", "city", "lat", "lon"]]], ignore_index=True)
        cache = cache.drop_duplicates(subset=["country", "city"], keep="last")
        _save_geocache(cache)

    return work.merge(cache, on=["country", "city"], how="left")


def _get_workspace_data(workspace_id: str, date_range: str, start: date | None, end: date | None) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    meta = load_workspace_meta(workspace_id)
    cities, timeline = load_workspace_dataset(workspace_id)
    if not timeline.empty:
        timeline = _filter_timeline(timeline, date_range, start, end)
    return meta, cities, timeline


def _country_summary(timeline: pd.DataFrame) -> pd.DataFrame:
    country_summary = (
        timeline[timeline["city"].fillna("") == ""]
        .groupby("country", as_index=False)
        .agg(avg_score=("score", "mean"))
        .sort_values("avg_score", ascending=False)
    )
    if country_summary.empty:
        country_summary = (
            timeline.groupby("country", as_index=False)
            .agg(avg_score=("score", "mean"))
            .sort_values("avg_score", ascending=False)
        )
    country_summary["iso3"] = country_summary["country"].map(_country_iso3)
    country_summary["country_name"] = country_summary["country"].map(_country_name)
    return country_summary


def _empty_dashboard(meta: dict, message: str) -> dict:
    return {
        "workspace": _workspace_payload(meta),
        "has_data": False,
        "message": message,
    }


def _render_world_charts(country_summary: pd.DataFrame, cities: pd.DataFrame, selected_country: str) -> dict:
    fig_world = px.choropleth(
        country_summary,
        locations="iso3",
        color="avg_score",
        hover_name="country_name",
        hover_data={"avg_score": ":.1f", "iso3": False},
        projection="natural earth",
        color_continuous_scale="YlOrRd",
    )
    fig_world.update_layout(height=520, margin=dict(l=0, r=0, t=8, b=0), paper_bgcolor="rgba(0,0,0,0)")

    city_map_data = cities[cities["country"] == selected_country].sort_values("score", ascending=False)
    fig_drill = go.Figure()
    if not city_map_data.empty:
        points = _geocode_cities(city_map_data)
        points = points.merge(city_map_data[["country", "city", "score"]], on=["country", "city"], how="left").dropna(subset=["lat", "lon"])
        if not points.empty:
            fig_drill = px.scatter_map(
                points,
                lat="lat",
                lon="lon",
                color="score",
                size="score",
                size_max=25,
                hover_name="city",
                hover_data={"score": True, "lat": False, "lon": False},
                zoom=4,
                map_style="carto-positron",
                color_continuous_scale="Viridis",
            )
            fig_drill.update_traces(marker={"opacity": 0.9})
            fig_drill.update_layout(height=520, margin=dict(l=0, r=0, t=8, b=0))

    city_world = go.Figure()
    city_map_all = cities.sort_values("score", ascending=False).head(100).copy()
    if not city_map_all.empty:
        points_all = _geocode_cities(city_map_all)
        points_all = points_all.merge(city_map_all[["country", "city", "score"]], on=["country", "city"], how="left").dropna(subset=["lat", "lon"])
        if not points_all.empty:
            city_world = px.scatter_map(
                points_all,
                lat="lat",
                lon="lon",
                color="score",
                size="score",
                size_max=18,
                hover_name="city",
                hover_data={"country": True, "score": True, "lat": False, "lon": False},
                zoom=1.2,
                map_style="carto-positron",
                color_continuous_scale="Plasma",
            )
            city_world.update_layout(height=580, margin=dict(l=0, r=0, t=8, b=0))

    return {
        "world": _fig_to_dict(fig_world),
        "drill_city": _fig_to_dict(fig_drill),
        "world_city": _fig_to_dict(city_world),
        "drill_table": city_map_data[["city", "score", "geo_code"]].head(40).to_dict(orient="records"),
    }


def _render_country_analysis(timeline: pd.DataFrame, country_code: str) -> dict:
    country_tl = timeline[(timeline["country"] == country_code) & (timeline["city"].fillna("") == "")].copy()
    if country_tl.empty:
        country_tl = timeline[timeline["country"] == country_code].copy()
    country_agg = country_tl.groupby("date", as_index=False).agg(score=("score", "mean")).sort_values("date")

    mas = compute_moving_averages(country_agg["score"])
    scores = compute_trend_scores(country_agg["score"])
    signal = robust_trend_signal(country_agg["score"])
    strength = trend_strength_meter(country_agg["score"])

    fig_ts = go.Figure()
    fig_ts.add_trace(go.Scatter(x=country_agg["date"], y=country_agg["score"], mode="lines", name="Raw", opacity=0.35, line=dict(color="#94a3b8", width=1)))
    fig_ts.add_trace(go.Scatter(x=country_agg["date"], y=mas["ma7"], mode="lines", name="MA7", line=dict(color="#0f766e", width=2.4)))
    fig_ts.add_trace(go.Scatter(x=country_agg["date"], y=mas["ma30"], mode="lines", name="MA30", line=dict(color="#ea580c", width=2, dash="dash")))

    spikes = detect_spikes(country_agg["score"], z_threshold=2.0)
    if not spikes.empty:
        spike_rows = spikes[spikes["is_spike"]]
        if not spike_rows.empty:
            peak_dates = country_agg.loc[spike_rows["index"], "date"]
            peak_scores = country_agg.loc[spike_rows["index"], "score"]
            fig_ts.add_trace(go.Scatter(x=peak_dates, y=peak_scores, mode="markers", name="Spike", marker=dict(color="#dc2626", size=9, symbol="diamond")))

    fig_ts.update_layout(height=430, margin=dict(l=20, r=20, t=45, b=22), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", hovermode="x unified")

    decomp = stl_decompose(country_agg["score"])
    fig_stl = go.Figure()
    if decomp is not None:
        fig_stl = make_subplots(rows=4, cols=1, shared_xaxes=True, subplot_titles=["Observed", "Trend", "Seasonal", "Residual"], vertical_spacing=0.05)
        dates = country_agg["date"]
        fig_stl.add_trace(go.Scatter(x=dates, y=decomp["observed"], mode="lines", line=dict(color="#0f766e")), row=1, col=1)
        fig_stl.add_trace(go.Scatter(x=dates, y=decomp["trend"], mode="lines", line=dict(color="#f97316")), row=2, col=1)
        fig_stl.add_trace(go.Scatter(x=dates, y=decomp["seasonal"], mode="lines", line=dict(color="#22c55e")), row=3, col=1)
        fig_stl.add_trace(go.Scatter(x=dates, y=decomp["residual"], mode="lines+markers", marker=dict(size=3), line=dict(color="#7c3aed")), row=4, col=1)
        fig_stl.update_layout(height=660, margin=dict(l=20, r=20, t=50, b=24), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=False)

    change_pts = detect_change_points(country_agg["score"], threshold=2.0)
    extrema = detect_local_extrema(country_agg["score"], order=3)

    fig_cp = go.Figure()
    fig_cp.add_trace(go.Scatter(x=country_agg["date"], y=country_agg["score"], mode="lines", line=dict(color="#0f766e", width=2), name="Score"))
    if change_pts:
        cp_dates = [country_agg["date"].iloc[i] for i in change_pts if i < len(country_agg)]
        cp_scores = [country_agg["score"].iloc[i] for i in change_pts if i < len(country_agg)]
        fig_cp.add_trace(go.Scatter(x=cp_dates, y=cp_scores, mode="markers", name="Change", marker=dict(color="#ef4444", size=12, symbol="x")))
    for idx in extrema.get("maxima", []):
        if idx < len(country_agg):
            fig_cp.add_trace(go.Scatter(x=[country_agg["date"].iloc[idx]], y=[country_agg["score"].iloc[idx]], mode="markers", marker=dict(color="#22c55e", size=8), name="Peak", showlegend=False))
    for idx in extrema.get("minima", []):
        if idx < len(country_agg):
            fig_cp.add_trace(go.Scatter(x=[country_agg["date"].iloc[idx]], y=[country_agg["score"].iloc[idx]], mode="markers", marker=dict(color="#a855f7", size=8), name="Valley", showlegend=False))
    fig_cp.update_layout(height=420, margin=dict(l=20, r=20, t=45, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")

    vol_df = rolling_volatility(country_agg["score"], window=7)
    fig_vol = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=["Bollinger", "Volatility %"], row_heights=[0.65, 0.35], vertical_spacing=0.08)
    fig_vol.add_trace(go.Scatter(x=country_agg["date"], y=vol_df["upper_band"], mode="lines", line=dict(width=0), showlegend=False), row=1, col=1)
    fig_vol.add_trace(go.Scatter(x=country_agg["date"], y=vol_df["lower_band"], mode="lines", line=dict(width=0), fill="tonexty", fillcolor="rgba(15,118,110,0.10)", name="Band"), row=1, col=1)
    fig_vol.add_trace(go.Scatter(x=country_agg["date"], y=vol_df["ma"], mode="lines", line=dict(color="#0f766e", width=2), name="MA"), row=1, col=1)
    fig_vol.add_trace(go.Scatter(x=country_agg["date"], y=vol_df["value"], mode="lines", line=dict(color="#334155", width=1), name="Score"), row=1, col=1)
    fig_vol.add_trace(go.Bar(x=country_agg["date"], y=vol_df["volatility_pct"], marker=dict(color=vol_df["volatility_pct"], colorscale="YlOrRd", showscale=False), name="Vol%"), row=2, col=1)
    fig_vol.update_layout(height=560, margin=dict(l=20, r=20, t=46, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")

    forecast_df = advanced_forecast(country_agg["score"], periods=12)
    fig_fc = go.Figure()
    if not forecast_df.empty:
        fig_fc.add_trace(go.Scatter(x=country_agg["date"], y=country_agg["score"], mode="lines", line=dict(color="#0f766e", width=2), name="History"))
        fig_fc.add_trace(go.Scatter(x=forecast_df["ds"], y=forecast_df["yhat_upper"], mode="lines", line=dict(width=0), showlegend=False))
        fig_fc.add_trace(go.Scatter(x=forecast_df["ds"], y=forecast_df["yhat_lower"], mode="lines", line=dict(width=0), fill="tonexty", fillcolor="rgba(249,115,22,0.12)", name="95% CI"))
        fig_fc.add_trace(go.Scatter(x=forecast_df["ds"], y=forecast_df["yhat"], mode="lines", line=dict(color="#f97316", width=2.3, dash="dash"), name="Forecast"))
    fig_fc.update_layout(height=420, margin=dict(l=20, r=20, t=45, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")

    corr_matrix = compute_correlation_matrix(timeline[timeline["city"].fillna("") == ""] if not timeline.empty else timeline, group_col="country")
    fig_corr = go.Figure()
    if not corr_matrix.empty and corr_matrix.shape[0] > 1:
        fig_corr = px.imshow(corr_matrix, color_continuous_scale="RdBu_r", zmin=-1, zmax=1)
        fig_corr.update_layout(height=500, margin=dict(l=20, r=20, t=45, b=20))

    return {
        "country": country_code,
        "country_name": _country_name(country_code),
        "stats": {
            "signal": signal,
            "scores": scores,
            "strength": strength,
            "change_points_count": len(change_pts),
        },
        "charts": {
            "overview": _fig_to_dict(fig_ts),
            "stl": _fig_to_dict(fig_stl),
            "change_points": _fig_to_dict(fig_cp),
            "volatility": _fig_to_dict(fig_vol),
            "forecast": _fig_to_dict(fig_fc),
            "correlation": _fig_to_dict(fig_corr),
        },
        "tables": {
            "forecast": forecast_df.to_dict(orient="records") if not forecast_df.empty else [],
            "spikes": spikes[spikes["is_spike"]].to_dict(orient="records") if not spikes.empty else [],
        },
    }


def _render_city_analysis(
    timeline: pd.DataFrame,
    cities: pd.DataFrame,
    country_code: str,
    city_name: str | None,
    selected_geo_code: str | None = None,
) -> dict:
    country_cities_df = cities[cities["country"] == country_code].sort_values("score", ascending=False).reset_index(drop=True)
    ranked = location_trend_ranking(country_cities_df)
    city_names = list(dict.fromkeys([str(item).strip() for item in country_cities_df["city"].dropna().tolist() if str(item).strip()]))
    requested_geo_code = str(selected_geo_code or "").strip()

    if not city_names:
        return {
            "country": country_code,
            "city": None,
            "city_options": [],
            "ranking": [],
            "timeline_ready": False,
            "message": "Sehir skoru verisi yok",
            "charts": {},
            "stats": {},
        }

    normalized_city_name = str(city_name or "").strip().casefold()
    selected_row = pd.DataFrame()
    if requested_geo_code:
        selected_row = ranked[ranked["geo_code"].fillna("").astype(str).str.strip() == requested_geo_code].head(1)
    if selected_row.empty:
        selected_row = ranked[ranked["city"].astype(str).str.strip().str.casefold() == normalized_city_name].head(1)

    selected_city = str(selected_row.iloc[0]["city"]) if not selected_row.empty else next((item for item in city_names if item.casefold() == normalized_city_name), city_names[0])
    selected_geo_code = requested_geo_code or (str(selected_row.iloc[0]["geo_code"]) if not selected_row.empty else "")
    selected_score = float(selected_row.iloc[0]["score"]) if not selected_row.empty else 0.0

    country_timeline = timeline[timeline["country"] == country_code].copy()
    city_ts = pd.DataFrame(columns=["date", "score"])
    if selected_geo_code:
        geo_mask = country_timeline["geo_code"].fillna("").astype(str).str.strip() == selected_geo_code
        city_ts = country_timeline[geo_mask][["date", "score"]].copy()
    if city_ts.empty:
        name_mask = country_timeline["city"].fillna("").astype(str).str.strip().str.casefold() == selected_city.casefold()
        city_ts = country_timeline[name_mask][["date", "score"]].copy()
    city_ts = city_ts.sort_values("date").reset_index(drop=True)

    timeline_ready = not city_ts.empty
    city_signal = {"direction": "flat", "label": "Zaman serisi bekleniyor", "slope": 0, "volatility": 0}
    city_scores = {"growth_rate": 0}
    city_mas = {"ma7": pd.Series(dtype=float)}
    city_strength = {"score": 0, "label": "Zaman serisi bekleniyor"}
    outlier_count = 0
    fig_city = go.Figure()
    city_forecast = pd.DataFrame()
    message = "Sehir zaman serisi henüz indirilmedi. Manuel indirme butonunu kullanin."

    if timeline_ready:
        city_ts["score_clean"], outlier_count = clean_city_outliers(city_ts["score"])
        city_signal = robust_trend_signal(city_ts["score_clean"])
        city_scores = compute_trend_scores(city_ts["score_clean"])
        city_mas = compute_moving_averages(city_ts["score_clean"])
        city_strength = trend_strength_meter(city_ts["score_clean"])

        fig_city.add_trace(go.Scatter(x=city_ts["date"], y=city_ts["score"], mode="lines", name="Raw", opacity=0.3, line=dict(color="#94a3b8", width=1)))
        fig_city.add_trace(go.Scatter(x=city_ts["date"], y=city_ts["score_clean"], mode="lines", name="Clean", line=dict(color="#0f766e", width=2), fill="tozeroy", fillcolor="rgba(15,118,110,0.08)"))
        fig_city.add_trace(go.Scatter(x=city_ts["date"], y=city_mas["ma7"], mode="lines", name="MA7", line=dict(color="#f97316", width=1.5, dash="dot")))

        city_forecast = advanced_forecast(city_ts["score_clean"], periods=12)
        if not city_forecast.empty:
            fig_city.add_trace(go.Scatter(x=city_forecast["ds"], y=city_forecast["yhat_upper"], mode="lines", line=dict(width=0), showlegend=False))
            fig_city.add_trace(go.Scatter(x=city_forecast["ds"], y=city_forecast["yhat_lower"], mode="lines", line=dict(width=0), fill="tonexty", fillcolor="rgba(249,115,22,0.1)", name="95% CI"))
            fig_city.add_trace(go.Scatter(x=city_forecast["ds"], y=city_forecast["yhat"], mode="lines", name="Forecast", line=dict(color="#f97316", width=2, dash="dash")))
        message = ""

    fig_city.update_layout(height=430, margin=dict(l=20, r=20, t=45, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", hovermode="x unified")

    return {
        "country": country_code,
        "city": selected_city,
        "city_options": city_names,
        "geo_code": selected_geo_code,
        "selected_score": selected_score,
        "timeline_ready": timeline_ready,
        "message": message,
        "ranking": ranked[["rank", "city", "score", "geo_code"]].head(30).to_dict(orient="records") if not ranked.empty else [],
        "stats": {
            "signal": city_signal,
            "scores": city_scores,
            "strength": city_strength,
            "outliers_removed": int(outlier_count),
            "recommendation": recommendation_from_signal(city_signal, lang="tr"),
        },
        "charts": {
            "city_trend": _fig_to_dict(fig_city),
        },
        "tables": {
            "forecast": city_forecast.to_dict(orient="records") if not city_forecast.empty else [],
        },
    }


def _render_hourly(keyword: str, country_code: str) -> dict:
    cfg = FetchConfig(keyword=keyword, hl="tr-TR")
    try:
        hourly_df = fetch_hourly_data(country_code, config=cfg)
    except Exception:
        return {"has_data": False, "message": "No hourly data"}

    if hourly_df.empty:
        return {"has_data": False, "message": "No hourly data"}

    analysis = hourly_analysis(hourly_df, lang="tr")

    fig_hourly = go.Figure()
    if not analysis["avg_by_hour"].empty:
        fig_hourly = px.bar(
            x=analysis["avg_by_hour"].index,
            y=analysis["avg_by_hour"].values,
            labels={"x": "Hour", "y": "Avg Score"},
            color=analysis["avg_by_hour"].values,
            color_continuous_scale="Viridis",
        )
        fig_hourly.update_layout(height=380, margin=dict(l=20, r=20, t=42, b=20), coloraxis_showscale=False)

    fig_heatmap = go.Figure()
    if not analysis["heatmap_matrix"].empty:
        fig_heatmap = px.imshow(
            analysis["heatmap_matrix"],
            labels=dict(x="Hour", y="Day", color="Score"),
            color_continuous_scale="Viridis",
            aspect="auto",
        )
        fig_heatmap.update_layout(height=380, margin=dict(l=20, r=20, t=42, b=20))

    return {
        "has_data": True,
        "country": country_code,
        "peak_hours": analysis["peak_hours"],
        "best_hours_text": best_ad_hours_text(analysis["peak_hours"], lang="tr"),
        "charts": {
            "avg_hour": _fig_to_dict(fig_hourly),
            "heatmap": _fig_to_dict(fig_heatmap),
        },
    }


def _render_ranking(timeline: pd.DataFrame, compare_countries: list[str] | None) -> dict:
    base = timeline[timeline["city"].fillna("") == ""]
    if base.empty:
        base = timeline
    rising, falling = country_ranking(base, top_n=10)

    fig_rising = go.Figure()
    if not rising.empty:
        fig_rising = px.bar(rising.head(10), x="change_pct", y="country", orientation="h", color="change_pct", color_continuous_scale="Greens")
        fig_rising.update_layout(height=370, margin=dict(l=20, r=20, t=20, b=20), yaxis=dict(autorange="reversed"), coloraxis_showscale=False)

    fig_falling = go.Figure()
    if not falling.empty:
        fig_falling = px.bar(falling.head(10), x="change_pct", y="country", orientation="h", color="change_pct", color_continuous_scale="Reds_r")
        fig_falling.update_layout(height=370, margin=dict(l=20, r=20, t=20, b=20), yaxis=dict(autorange="reversed"), coloraxis_showscale=False)

    compare = compare_countries or sorted(base["country"].dropna().unique().tolist()[:3])
    compare_data = base[base["country"].isin(compare)].copy()
    compare_agg = compare_data.groupby(["country", "date"], as_index=False)["score"].mean() if not compare_data.empty else pd.DataFrame(columns=["country", "date", "score"])

    fig_compare = go.Figure()
    if not compare_agg.empty:
        fig_compare = px.line(compare_agg, x="date", y="score", color="country")
        fig_compare.update_layout(height=410, margin=dict(l=20, r=20, t=44, b=20), hovermode="x unified")

    return {
        "rising": rising.to_dict(orient="records"),
        "falling": falling.to_dict(orient="records"),
        "compare_selected": compare,
        "charts": {
            "rising": _fig_to_dict(fig_rising),
            "falling": _fig_to_dict(fig_falling),
            "compare": _fig_to_dict(fig_compare),
        },
    }


def _render_raw(cities: pd.DataFrame, timeline: pd.DataFrame, search: str) -> dict:
    city_df = cities.copy()
    tl_df = timeline.copy()

    if search:
        city_df = city_df[
            city_df["country"].astype(str).str.contains(search, case=False, na=False)
            | city_df["city"].astype(str).str.contains(search, case=False, na=False)
        ]
        tl_df = tl_df[
            tl_df["country"].astype(str).str.contains(search, case=False, na=False)
            | tl_df["city"].astype(str).str.contains(search, case=False, na=False)
        ]

    tl_show = tl_df.sort_values(["country", "city", "date"]).copy() if not tl_df.empty else tl_df
    if not tl_show.empty:
        tl_show["date"] = pd.to_datetime(tl_show["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    return {
        "city": city_df.head(1500).to_dict(orient="records"),
        "timeline": tl_show.head(2500).to_dict(orient="records"),
        "counts": {"city": int(len(city_df)), "timeline": int(len(tl_show))},
    }


@app.get("/", response_class=HTMLResponse)
async def workspace_home(request: Request) -> HTMLResponse:
    default_workspace = ensure_default_workspace()
    records = [_workspace_payload(item) for item in list_workspaces()]
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "request": request,
            "default_workspace_id": get_default_workspace_id() or default_workspace["id"],
            "workspaces": records,
            "countries": ALL_COUNTRIES,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    default_workspace = ensure_default_workspace()
    records = [_workspace_payload(item) for item in list_workspaces()]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "default_workspace_id": get_default_workspace_id() or default_workspace["id"],
            "workspaces": records,
            "countries": ALL_COUNTRIES,
        },
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "B2BTrend API"}


@app.get("/api/workspaces")
async def api_workspaces() -> dict:
    records = [_workspace_payload(item) for item in list_workspaces()]
    return {"items": records, "default_workspace_id": get_default_workspace_id()}


@app.post("/api/workspaces")
async def api_create_workspace(payload: WorkspaceCreate) -> dict:
    try:
        created = create_workspace(name=payload.name, keyword=payload.keyword, countries=payload.countries)
        updated = update_workspace(
            created["id"],
            name=payload.name,
            language=payload.language,
            keyword=payload.keyword,
            countries=payload.countries,
            use_topic_mode=payload.use_topic_mode,
            country_keywords=_clean_country_keywords(payload.country_keywords, payload.countries),
        )
    except WorkspaceValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await hub.broadcast({"type": "workspace_created", "workspace_id": updated["id"]})
    return {"item": _workspace_payload(updated)}


@app.patch("/api/workspaces/{workspace_id}")
async def api_update_workspace(workspace_id: str, payload: WorkspaceUpdate) -> dict:
    meta = load_workspace_meta(workspace_id)
    if (
        payload.name is None
        and payload.keyword is None
        and payload.countries is None
        and payload.language is None
        and payload.use_topic_mode is None
        and payload.country_keywords is None
    ):
        if payload.is_default:
            set_default_workspace(workspace_id)
        await hub.broadcast({"type": "workspace_updated", "workspace_id": workspace_id})
        refreshed = load_workspace_meta(workspace_id)
        return {"item": _workspace_payload(refreshed)}

    countries = payload.countries if payload.countries is not None else list(meta.get("countries") or TOP_20_COUNTRIES)
    country_keywords = payload.country_keywords if payload.country_keywords is not None else dict(meta.get("country_keywords") or {})
    country_keywords = _clean_country_keywords(country_keywords, countries)

    try:
        updated = update_workspace(
            workspace_id,
            name=payload.name if payload.name is not None else str(meta.get("name") or workspace_id),
            language=payload.language if payload.language is not None else str(meta.get("language") or "tr"),
            keyword=payload.keyword if payload.keyword is not None else str(meta.get("keyword") or DEFAULT_KEYWORD),
            countries=countries,
            use_topic_mode=payload.use_topic_mode if payload.use_topic_mode is not None else bool(meta.get("use_topic_mode", False)),
            country_keywords=country_keywords,
        )
    except WorkspaceValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.is_default:
        set_default_workspace(workspace_id)
    await hub.broadcast({"type": "workspace_updated", "workspace_id": workspace_id})
    return {"item": _workspace_payload(updated)}


@app.delete("/api/workspaces/{workspace_id}")
async def api_delete_workspace(workspace_id: str) -> dict:
    delete_workspace(workspace_id)
    await hub.broadcast({"type": "workspace_deleted", "workspace_id": workspace_id})
    return {"ok": True}


@app.post("/api/cache/clear")
async def api_clear_cache() -> dict:
    count = clear_cache()
    await hub.broadcast({"type": "cache_cleared", "count": count})
    return {"ok": True, "deleted": count}


@app.post("/api/fetch")
async def api_fetch(payload: FetchRequest) -> JSONResponse:
    meta = load_workspace_meta(payload.workspace_id)
    resume_requested = bool(payload.resume)
    keyword = str(meta.get("keyword") or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
    countries = list(meta.get("countries") or TOP_20_COUNTRIES)
    use_topic_mode = bool(meta.get("use_topic_mode", False))
    country_keywords = dict(meta.get("country_keywords") or {})
    language = str(meta.get("language") or "tr").strip() or "tr"
    clean_country_keywords = _clean_country_keywords(country_keywords, countries)

    try:
        job = fetch_job_store.start_job(
            workspace_id=payload.workspace_id,
            keyword=keyword,
            countries=countries,
            use_topic_mode=use_topic_mode,
            language=language,
            country_keywords=clean_country_keywords,
            resume_requested=resume_requested,
        )
    except JobConflictError as exc:
        return JSONResponse(status_code=409, content={"ok": False, "detail": "Bu anda baska bir veri cekimi calisiyor", "job": exc.active_job})

    await hub.broadcast({"type": "fetch_job_update", "state": job})
    await hub.broadcast({"type": "fetch_started", "state": job})

    loop = asyncio.get_running_loop()
    worker = threading.Thread(
        target=_run_fetch_job,
        kwargs={
            "job_id": job["job_id"],
            "loop": loop,
            "workspace_id": payload.workspace_id,
            "keyword": keyword,
            "countries": countries,
            "language": language,
            "use_topic_mode": use_topic_mode,
            "country_keywords": clean_country_keywords,
            "resume_requested": resume_requested,
        },
        daemon=True,
    )
    worker.start()

    return JSONResponse(status_code=202, content={"ok": True, "job": job})


@app.post("/api/fetch/cancel")
async def api_fetch_cancel(payload: FetchCancelRequest) -> JSONResponse:
    snapshot = fetch_job_store.snapshot()
    active = snapshot.get("active") or {}
    if not active or active.get("status") not in {"queued", "running", "cancelling"}:
        return JSONResponse(status_code=409, content={"ok": False, "detail": "Iptal edilecek aktif bir cekim yok"})

    workspace_id = str(payload.workspace_id or "").strip()
    if workspace_id and active.get("workspace_id") != workspace_id:
        return JSONResponse(status_code=409, content={"ok": False, "detail": "Bu workspace icin aktif cekim yok", "job": active})

    updated = fetch_job_store.request_cancel(str(active.get("job_id") or ""))
    if not updated:
        return JSONResponse(status_code=409, content={"ok": False, "detail": "Iptal istegi uygulanamadi"})

    await hub.broadcast({"type": "fetch_job_update", "state": updated})
    await hub.broadcast({"type": "fetch_cancel_requested", "state": updated})
    return JSONResponse(status_code=200, content={"ok": True, "job": updated})


@app.get("/api/fetch/status")
async def api_fetch_status() -> dict:
    return fetch_job_store.snapshot()


@app.get("/api/dashboard/overview")
async def api_dashboard_overview(
    workspace_id: str = Query(...),
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    country: str | None = Query(None),
) -> dict:
    meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)
    if cities.empty or timeline.empty:
        return _empty_dashboard(meta, "No data yet. Run fetch to create the first dataset.")

    country_summary = _country_summary(timeline)
    if country_summary.empty:
        return _empty_dashboard(meta, "No country data in selected date range")

    selected_country = country if country and country in country_summary["country"].tolist() else str(country_summary.iloc[0]["country"])

    metrics = {
        "countries": int(country_summary["country"].nunique()),
        "cities": int(cities["city"].nunique()),
        "avg_score": round(float(country_summary["avg_score"].mean()), 2),
        "best_country": _country_name(str(country_summary.iloc[0]["country"])),
        "best_country_score": round(float(country_summary.iloc[0]["avg_score"]), 2),
        "timeline_start": str(pd.to_datetime(timeline["date"]).min().date()),
        "timeline_end": str(pd.to_datetime(timeline["date"]).max().date()),
        "keyword": str(meta.get("keyword") or DEFAULT_KEYWORD),
    }

    world = await asyncio.to_thread(_render_world_charts, country_summary, cities, selected_country)

    return {
        "workspace": _workspace_payload(meta),
        "has_data": True,
        "selected_country": selected_country,
        "country_options": country_summary[["country", "country_name", "avg_score", "iso3"]].to_dict(orient="records"),
        "metrics": metrics,
        "charts": world,
    }


@app.get("/api/dashboard/country")
async def api_dashboard_country(
    workspace_id: str = Query(...),
    country: str = Query(...),
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict:
    meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)
    if cities.empty or timeline.empty:
        return _empty_dashboard(meta, "No data")

    result = await asyncio.to_thread(_render_country_analysis, timeline, country)
    return {"workspace": _workspace_payload(meta), "has_data": True, **result}


@app.get("/api/dashboard/city")
async def api_dashboard_city(
    workspace_id: str = Query(...),
    country: str = Query(...),
    city: str | None = Query(None),
    geo_code: str | None = Query(None),
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict:
    meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)
    if cities.empty:
        return _empty_dashboard(meta, "No city score data yet. Run fetch first.")

    result = await asyncio.to_thread(_render_city_analysis, timeline, cities, country, city, geo_code)
    return {"workspace": _workspace_payload(meta), "has_data": True, **result}


@app.post("/api/fetch/city-timeline")
async def api_fetch_city_timeline(payload: CityTimelineFetchRequest) -> dict:
    result = await asyncio.to_thread(
        _download_city_timeline_for_workspace,
        payload.workspace_id,
        payload.country,
        payload.city,
        payload.geo_code,
    )

    if not result.get("ok") and result.get("status") == "empty":
        raise HTTPException(status_code=502, detail=str(result.get("message") or "Sehir zaman serisi alinmadi."))

    return result


@app.get("/api/dashboard/hourly")
async def api_dashboard_hourly(workspace_id: str = Query(...), country: str = Query(...)) -> dict:
    meta = load_workspace_meta(workspace_id)
    keyword = str(meta.get("keyword") or DEFAULT_KEYWORD)
    result = await asyncio.to_thread(_render_hourly, keyword, country)
    return {"workspace": _workspace_payload(meta), **result}


@app.get("/api/dashboard/ranking")
async def api_dashboard_ranking(
    workspace_id: str = Query(...),
    compare: str | None = Query(None),
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict:
    meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)
    if cities.empty or timeline.empty:
        return _empty_dashboard(meta, "No data")

    compare_list = [x.strip().upper() for x in (compare or "").split(",") if x.strip()]
    result = await asyncio.to_thread(_render_ranking, timeline, compare_list)
    return {"workspace": _workspace_payload(meta), "has_data": True, **result}


@app.get("/api/dashboard/raw")
async def api_dashboard_raw(
    workspace_id: str = Query(...),
    search: str = Query(""),
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict:
    meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)
    if cities.empty or timeline.empty:
        return _empty_dashboard(meta, "No data")

    result = await asyncio.to_thread(_render_raw, cities, timeline, search)
    return {"workspace": _workspace_payload(meta), "has_data": True, **result}


@app.get("/api/dashboard/related")
async def api_dashboard_related(workspace_id: str = Query(...), country: str = Query(...)) -> dict:
    meta = load_workspace_meta(workspace_id)
    keyword = str(meta.get("keyword") or DEFAULT_KEYWORD)
    cfg = FetchConfig(keyword=keyword, hl="tr-TR")

    client = await asyncio.to_thread(_build_client, cfg)
    try:
        rq = await asyncio.to_thread(fetch_related_queries, client, country, cfg)
        rt = await asyncio.to_thread(fetch_related_topics, client, country, cfg)
    except Exception:
        rq = {"top": pd.DataFrame(), "rising": pd.DataFrame()}
        rt = {"top": pd.DataFrame(), "rising": pd.DataFrame()}

    return {
        "workspace": _workspace_payload(meta),
        "country": country,
        "queries": {
            "top": rq["top"].to_dict(orient="records") if not rq["top"].empty else [],
            "rising": rq["rising"].to_dict(orient="records") if not rq["rising"].empty else [],
        },
        "topics": {
            "top": rt["top"].to_dict(orient="records") if not rt["top"].empty else [],
            "rising": rt["rising"].to_dict(orient="records") if not rt["rising"].empty else [],
        },
    }


@app.get("/api/export/csv")
async def api_export_csv(
    workspace_id: str = Query(...),
    dataset: str = Query("city"),
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> StreamingResponse:
    _meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)

    if dataset == "city":
        content = export_csv(cities)
        filename = f"city_scores_{workspace_id}.csv"
    elif dataset == "timeline":
        content = export_csv(timeline)
        filename = f"timeline_{workspace_id}.csv"
    else:
        raise HTTPException(status_code=400, detail="dataset must be city or timeline")

    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket) -> None:
    await hub.connect(ws)
    await ws.send_json({"type": "connected"})
    try:
        while True:
            try:
                message = await asyncio.wait_for(ws.receive_text(), timeout=20)
                if message.strip().lower() == "ping":
                    await ws.send_json({"type": "pong"})
            except TimeoutError:
                await ws.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        hub.disconnect(ws)


if __name__ == "__main__":
    import uvicorn

    reload_enabled = os.getenv("B2BTREND_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=reload_enabled)
