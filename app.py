from __future__ import annotations

import asyncio
import io
import json
import threading
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import pycountry
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
from src.config import ALL_COUNTRIES, DEFAULT_KEYWORD, GEOCACHE_FILE, TOP_20_COUNTRIES
from src.fetch_job_store import FetchJobStore, JobConflictError
from src.reports import export_csv
from src.trend_fetcher import (
    FetchConfig,
    FetchCancelledError,
    _build_client,
    clear_cache,
    fetch_hourly_data,
    fetch_related_queries,
    fetch_related_topics,
    fetch_trends_dataset,
    fetch_trends_dataset_country_keywords,
    save_snapshot,
)
from src.workspace_store import (
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
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


class FetchCancelRequest(BaseModel):
    workspace_id: str | None = None


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
        pass


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
    return bool(active and active.get("job_id") == job_id and (active.get("cancel_requested") or active.get("status") == "cancelling"))


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
) -> None:
    hl = _language_to_hl(language)
    cfg = FetchConfig(
        keyword=keyword,
        hl=hl,
        retries=3,
        backoff_factor=0.6,
        max_attempt_per_country=4,
        top_cities_per_country=10,
        min_sleep_sec=max(5.0, 4.0),
        max_sleep_sec=max(10.0, 9.0),
    )

    _job_update(
        job_id,
        loop,
        {
            "status": "running",
            "phase": "prepare",
            "message": "Veri cekimi arkaplanda basladi",
            "progress": 0.02,
            "completed": 0,
            "total": max(1, len(countries) * 12),
        },
    )

    def progress_callback(payload: dict) -> None:
        data = dict(payload or {})
        data.setdefault("status", "running")
        data.setdefault("phase", "running")
        _job_update(job_id, loop, data)

    try:
        if use_topic_mode:
            cities, timeline = fetch_trends_dataset(
                countries,
                cfg,
                progress_callback=progress_callback,
                cancel_callback=lambda: _is_job_cancel_requested(job_id),
            )
        else:
            cities, timeline = fetch_trends_dataset_country_keywords(
                countries,
                country_keywords,
                keyword,
                cfg,
                progress_callback=progress_callback,
                cancel_callback=lambda: _is_job_cancel_requested(job_id),
            )

        if cities.empty or timeline.empty:
            # Dataset empty olsa bile bu durum exploit edilebilir; hata yap31lm315f saymayal31m.
            save_workspace_dataset(workspace_id, cities, timeline)
            try:
                save_snapshot(cities, timeline, keyword=keyword)
            except Exception:
                pass

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
            pass

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
        final = fetch_job_store.update_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            message="Veri cekimi iptal edildi",
        )
        if final:
            _schedule_broadcast(loop, {"type": "fetch_job_update", "state": final})
            _schedule_broadcast(loop, {"type": "fetch_cancelled", "state": final})
    except Exception as exc:
        message = f"Veri cekimi basarisiz: {exc}"
        final = fetch_job_store.finish_job(
            job_id,
            status="failed",
            message=message,
            error=str(exc),
        )
        if final:
            _schedule_broadcast(loop, {"type": "fetch_job_update", "state": final})
            _schedule_broadcast(loop, {"type": "fetch_failed", "state": final})


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


def _load_geocache() -> pd.DataFrame:
    if GEOCACHE_FILE.exists():
        cache = pd.read_csv(GEOCACHE_FILE)
        for col in ["country", "city", "lat", "lon"]:
            if col not in cache.columns:
                cache[col] = pd.NA
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
    GEOCACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(GEOCACHE_FILE, index=False)


def _geocode_cities(df: pd.DataFrame) -> pd.DataFrame:
    cache = _load_geocache()
    work = df[["country", "city"]].drop_duplicates().copy()
    merged = work.merge(cache, on=["country", "city"], how="left")

    missing = merged[merged["lat"].isna() | merged["lon"].isna()][["country", "city"]]
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
    city_map_all = cities.sort_values("score", ascending=False).head(250).copy()
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


def _render_city_analysis(timeline: pd.DataFrame, cities: pd.DataFrame, country_code: str, city_name: str | None) -> dict:
    country_cities_df = cities[cities["country"] == country_code].sort_values("score", ascending=False)
    ranked = location_trend_ranking(country_cities_df)
    city_names = sorted([c for c in timeline[timeline["country"] == country_code]["city"].dropna().unique().tolist() if str(c).strip()])

    if not city_names:
        return {
            "country": country_code,
            "city": None,
            "city_options": [],
            "ranking": ranked[["rank", "city", "score", "geo_code"]].head(30).to_dict(orient="records") if not ranked.empty else [],
            "message": "No city timeline data",
            "charts": {},
            "stats": {},
        }

    selected_city = city_name if city_name in city_names else city_names[0]
    city_ts = (
        timeline[(timeline["country"] == country_code) & (timeline["city"] == selected_city)][["date", "score"]]
        .sort_values("date")
        .reset_index(drop=True)
    )

    city_ts["score_clean"], outlier_count = clean_city_outliers(city_ts["score"]) if not city_ts.empty else (pd.Series(dtype=float), 0)
    city_signal = robust_trend_signal(city_ts["score_clean"]) if not city_ts.empty else {"direction": "flat", "label": "No data", "slope": 0, "volatility": 0}
    city_scores = compute_trend_scores(city_ts["score_clean"]) if not city_ts.empty else {"growth_rate": 0}
    city_mas = compute_moving_averages(city_ts["score_clean"]) if not city_ts.empty else {"ma7": pd.Series(dtype=float)}
    city_strength = trend_strength_meter(city_ts["score_clean"]) if not city_ts.empty else {"score": 0, "label": "No data"}

    fig_city = go.Figure()
    if not city_ts.empty:
        fig_city.add_trace(go.Scatter(x=city_ts["date"], y=city_ts["score"], mode="lines", name="Raw", opacity=0.3, line=dict(color="#94a3b8", width=1)))
        fig_city.add_trace(go.Scatter(x=city_ts["date"], y=city_ts["score_clean"], mode="lines", name="Clean", line=dict(color="#0f766e", width=2), fill="tozeroy", fillcolor="rgba(15,118,110,0.08)"))
        fig_city.add_trace(go.Scatter(x=city_ts["date"], y=city_mas["ma7"], mode="lines", name="MA7", line=dict(color="#f97316", width=1.5, dash="dot")))

    city_forecast = advanced_forecast(city_ts["score_clean"], periods=12) if not city_ts.empty else pd.DataFrame()
    if not city_forecast.empty:
        fig_city.add_trace(go.Scatter(x=city_forecast["ds"], y=city_forecast["yhat_upper"], mode="lines", line=dict(width=0), showlegend=False))
        fig_city.add_trace(go.Scatter(x=city_forecast["ds"], y=city_forecast["yhat_lower"], mode="lines", line=dict(width=0), fill="tonexty", fillcolor="rgba(249,115,22,0.1)", name="95% CI"))
        fig_city.add_trace(go.Scatter(x=city_forecast["ds"], y=city_forecast["yhat"], mode="lines", name="Forecast", line=dict(color="#f97316", width=2, dash="dash")))

    fig_city.update_layout(height=430, margin=dict(l=20, r=20, t=45, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", hovermode="x unified")

    return {
        "country": country_code,
        "city": selected_city,
        "city_options": city_names,
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
    await hub.broadcast({"type": "workspace_created", "workspace_id": updated["id"]})
    return {"item": _workspace_payload(updated)}


@app.patch("/api/workspaces/{workspace_id}")
async def api_update_workspace(workspace_id: str, payload: WorkspaceUpdate) -> dict:
    meta = load_workspace_meta(workspace_id)
    updated = update_workspace(
        workspace_id,
        name=payload.name if payload.name is not None else str(meta.get("name") or workspace_id),
        language=payload.language if payload.language is not None else str(meta.get("language") or "tr"),
        keyword=payload.keyword if payload.keyword is not None else str(meta.get("keyword") or DEFAULT_KEYWORD),
        countries=payload.countries if payload.countries is not None else list(meta.get("countries") or TOP_20_COUNTRIES),
        use_topic_mode=payload.use_topic_mode if payload.use_topic_mode is not None else bool(meta.get("use_topic_mode", False)),
        country_keywords=payload.country_keywords if payload.country_keywords is not None else dict(meta.get("country_keywords") or {}),
    )
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
    keyword = str(meta.get("keyword") or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
    countries = list(meta.get("countries") or TOP_20_COUNTRIES)
    use_topic_mode = bool(meta.get("use_topic_mode", False))
    country_keywords = dict(meta.get("country_keywords") or {})
    language = str(meta.get("language") or "tr").strip() or "tr"

    try:
        job = fetch_job_store.start_job(
            workspace_id=payload.workspace_id,
            keyword=keyword,
            countries=countries,
            use_topic_mode=use_topic_mode,
            language=language,
            country_keywords=_clean_country_keywords(country_keywords, countries),
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
            "country_keywords": _clean_country_keywords(country_keywords, countries),
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
    range: str = Query("all"),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict:
    meta, cities, timeline = await asyncio.to_thread(_get_workspace_data, workspace_id, range, start, end)
    if cities.empty or timeline.empty:
        return _empty_dashboard(meta, "No data")

    result = await asyncio.to_thread(_render_city_analysis, timeline, cities, country, city)
    return {"workspace": _workspace_payload(meta), "has_data": True, **result}


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

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
