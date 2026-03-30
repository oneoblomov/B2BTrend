"""Google Trends veri çekme katmanı — Herhangi bir anahtar kelime/topic desteği.

Özellikler:
- Herhangi bir keyword veya Topic ID ile çalışır
- Dil parametresi (hl) desteği
- Ülke / şehir bazlı ilgi skoru
- Zaman serisi (çeşitli aralıklar)
- Related queries & topics
- Dosya tabanlı cache
- Retry / backoff / proxy desteği
- Rate-limit koruması
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
from urllib3.util.retry import Retry

from src.config import (
    CACHE_DIR,
    DATASET_META_FILE,
    DEFAULT_KEYWORD,
    DEFAULT_CITIES_CSV,
    DEFAULT_TIMELINE_CSV,
    ENABLE_FETCH_CACHE,
    FETCH_CACHE_TTL_HOURS,
    FETCH_MAX_SLEEP,
    FETCH_MIN_SLEEP,
    MAX_CACHE_FILES,
    MAX_REPORT_FILES,
    PROXIES,
    REPORTS_DIR,
    SAVE_REPORT_HISTORY,
    TOP_20_COUNTRIES,
)

# ── urllib3 Retry uyumluluk yaması ──────────────────────────────
_ORIGINAL_RETRY_INIT = Retry.__init__


def _patch_retry_init() -> None:
    if getattr(Retry.__init__, "_patched", False):
        return

    def _init(self, *args, **kwargs):
        if "method_whitelist" in kwargs and "allowed_methods" not in kwargs:
            kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
        return _ORIGINAL_RETRY_INIT(self, *args, **kwargs)

    _init._patched = True  # type: ignore[attr-defined]
    Retry.__init__ = _init


_patch_retry_init()

from pytrends_modern.request import TrendReq  # noqa: E402


# ── Konfigürasyon ──────────────────────────────────────────────
@dataclass
class FetchConfig:
    keyword: str = DEFAULT_KEYWORD
    timeframe_12m: str = "today 12-m"
    timeframe_30d: str = "today 1-m"
    timeframe_7d: str = "now 7-d"
    tz: int = 360
    hl: str = "en-US"
    retries: int = 2
    backoff_factor: float = 0.3
    max_attempt_per_country: int = 4
    top_cities_per_country: int = 10
    min_sleep_sec: float = FETCH_MIN_SLEEP
    max_sleep_sec: float = FETCH_MAX_SLEEP
    use_cache: bool = ENABLE_FETCH_CACHE
    cache_ttl_hours: int = FETCH_CACHE_TTL_HOURS


# ── Yardımcılar ───────────────────────────────────────────────
def _build_client(cfg: FetchConfig, proxies: list[str] | None = None) -> TrendReq:
    kwargs = {
        "hl": cfg.hl,
        "tz": cfg.tz,
        "retries": cfg.retries,
        "backoff_factor": cfg.backoff_factor,
    }
    if proxies:
        kwargs["proxies"] = proxies
    return TrendReq(**kwargs)


def _score_col(df: pd.DataFrame) -> str:
    """İlk sayısal (trend skoru) kolonu bul."""
    for c in df.columns:
        if c == "isPartial":
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            return c
    raise ValueError("Sayısal trend kolonu bulunamadı")


def _sleep(cfg: FetchConfig) -> None:
    time.sleep(random.uniform(cfg.min_sleep_sec, cfg.max_sleep_sec))


def _emit_progress(progress_callback: Callable[[dict], None] | None, payload: dict) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(payload)
    except Exception:
        pass


class FetchCancelledError(RuntimeError):
    pass


def _check_cancel(cancel_callback: Callable[[], bool] | None) -> None:
    if cancel_callback and cancel_callback():
        raise FetchCancelledError("Fetch cancelled")


# ── Dosya Tabanlı Cache ───────────────────────────────────────
def _cache_key(prefix: str, **kwargs) -> str:
    raw = json.dumps({"prefix": prefix, **kwargs}, sort_keys=True)
    return prefix + "_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.parquet"


def _cache_get(key: str, ttl_hours: int) -> pd.DataFrame | None:
    fp = _cache_path(key)
    if not fp.exists():
        return None
    age_h = (datetime.now().timestamp() - fp.stat().st_mtime) / 3600
    if age_h > ttl_hours:
        fp.unlink(missing_ok=True)
        return None
    try:
        return pd.read_parquet(fp)
    except Exception:
        fp.unlink(missing_ok=True)
        return None


def _cache_set(key: str, df: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(_cache_path(key), index=False)
        _prune_files(CACHE_DIR, "*.parquet", MAX_CACHE_FILES)
    except Exception:
        pass


def _prune_files(directory: Path, pattern: str, keep: int) -> None:
    if keep <= 0 or not directory.exists():
        return
    files = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    for old_file in files[keep:]:
        old_file.unlink(missing_ok=True)


def _write_dataset_metadata(keyword: str, countries: Iterable[str], cities_df: pd.DataFrame, timeline_df: pd.DataFrame) -> None:
    payload = {
        "keyword": keyword,
        "countries": list(countries),
        "city_rows": int(len(cities_df)),
        "timeline_rows": int(len(timeline_df)),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    DATASET_META_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATASET_META_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── Tekil Çekme Fonksiyonları ─────────────────────────────────
def fetch_country_cities(
    client: TrendReq,
    country: str,
    cfg: FetchConfig,
) -> pd.DataFrame:
    """Bir ülkenin top şehirlerinin ilgi skorunu çeker."""
    kw = cfg.keyword

    if cfg.use_cache:
        key = _cache_key("cities", country=country, topic=kw, tf=cfg.timeframe_12m)
        cached = _cache_get(key, cfg.cache_ttl_hours)
        if cached is not None:
            return cached

    client.build_payload([kw], cat=0, timeframe=cfg.timeframe_12m, geo=country, gprop="")
    region_df = client.interest_by_region(resolution="CITY", inc_low_vol=False, inc_geo_code=True)

    if region_df.empty:
        return pd.DataFrame(columns=["country", "city", "geo_code", "score"])

    col = _score_col(region_df)
    top = region_df.sort_values(col, ascending=False).head(cfg.top_cities_per_country)

    rows = []
    for city, row in top.iterrows():
        geo_code = str(row.get("geoCode", "")).strip()
        score = int(row[col]) if pd.notna(row[col]) else 0
        rows.append({"country": country, "city": str(city), "geo_code": geo_code, "score": score})

    result = pd.DataFrame(rows)
    if cfg.use_cache and not result.empty:
        _cache_set(_cache_key("cities", country=country, topic=kw, tf=cfg.timeframe_12m), result)
    return result


def fetch_timeline(
    client: TrendReq,
    geo: str,
    cfg: FetchConfig,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """Belirli bir geo için zaman serisi çeker."""
    tf = timeframe or cfg.timeframe_12m
    kw = cfg.keyword

    if cfg.use_cache:
        key = _cache_key("timeline", geo=geo, topic=kw, tf=tf)
        cached = _cache_get(key, cfg.cache_ttl_hours)
        if cached is not None:
            return cached

    client.build_payload([kw], cat=0, timeframe=tf, geo=geo, gprop="")
    ts = client.interest_over_time()

    if ts.empty:
        return pd.DataFrame(columns=["date", "score"])

    col = _score_col(ts)
    result = pd.DataFrame({"date": ts.index, "score": ts[col].values})
    result["date"] = pd.to_datetime(result["date"])

    if cfg.use_cache and not result.empty:
        _cache_set(_cache_key("timeline", geo=geo, topic=kw, tf=tf), result)
    return result


def fetch_related_queries(
    client: TrendReq,
    geo: str,
    cfg: FetchConfig,
) -> dict[str, pd.DataFrame]:
    """Related queries (top + rising) çeker."""
    kw = cfg.keyword

    if cfg.use_cache:
        key_top = _cache_key("rq_top", geo=geo, topic=kw)
        key_rising = _cache_key("rq_rising", geo=geo, topic=kw)
        cached_top = _cache_get(key_top, cfg.cache_ttl_hours)
        cached_rising = _cache_get(key_rising, cfg.cache_ttl_hours)
        if cached_top is not None and cached_rising is not None:
            return {"top": cached_top, "rising": cached_rising}

    try:
        client.build_payload([kw], cat=0, timeframe=cfg.timeframe_12m, geo=geo, gprop="")
        related = client.related_queries()
    except Exception:
        return {"top": pd.DataFrame(), "rising": pd.DataFrame()}

    result: dict[str, pd.DataFrame] = {"top": pd.DataFrame(), "rising": pd.DataFrame()}

    if related and kw in related:
        data = related[kw]
        if data.get("top") is not None and not data["top"].empty:
            result["top"] = data["top"].copy()
        if data.get("rising") is not None and not data["rising"].empty:
            result["rising"] = data["rising"].copy()

    if cfg.use_cache:
        if not result["top"].empty:
            _cache_set(_cache_key("rq_top", geo=geo, topic=kw), result["top"])
        if not result["rising"].empty:
            _cache_set(_cache_key("rq_rising", geo=geo, topic=kw), result["rising"])

    return result


def fetch_related_topics(
    client: TrendReq,
    geo: str,
    cfg: FetchConfig,
) -> dict[str, pd.DataFrame]:
    """Related topics (top + rising) çeker."""
    kw = cfg.keyword

    if cfg.use_cache:
        key_top = _cache_key("rt_top", geo=geo, topic=kw)
        key_rising = _cache_key("rt_rising", geo=geo, topic=kw)
        cached_top = _cache_get(key_top, cfg.cache_ttl_hours)
        cached_rising = _cache_get(key_rising, cfg.cache_ttl_hours)
        if cached_top is not None and cached_rising is not None:
            return {"top": cached_top, "rising": cached_rising}

    try:
        client.build_payload([kw], cat=0, timeframe=cfg.timeframe_12m, geo=geo, gprop="")
        related = client.related_topics()
    except Exception:
        return {"top": pd.DataFrame(), "rising": pd.DataFrame()}

    result: dict[str, pd.DataFrame] = {"top": pd.DataFrame(), "rising": pd.DataFrame()}

    if related and kw in related:
        data = related[kw]
        if data.get("top") is not None and not data["top"].empty:
            result["top"] = data["top"].copy()
        if data.get("rising") is not None and not data["rising"].empty:
            result["rising"] = data["rising"].copy()

    if cfg.use_cache:
        if not result["top"].empty:
            _cache_set(_cache_key("rt_top", geo=geo, topic=kw), result["top"])
        if not result["rising"].empty:
            _cache_set(_cache_key("rt_rising", geo=geo, topic=kw), result["rising"])

    return result


# ── Toplu Veri Çekme ──────────────────────────────────────────
def fetch_trends_dataset(
    countries: Iterable[str] | None = None,
    config: FetchConfig | None = None,
    proxies: list[str] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    progress_context: dict[str, object] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Tüm ülkeler için şehir skorları + zaman serileri çeker."""
    cfg = config or FetchConfig()
    country_list = list(countries or TOP_20_COUNTRIES)
    proxy_list = proxies or PROXIES or None
    total_steps = max(1, len(country_list) * (cfg.top_cities_per_country + 2))
    completed_steps = 0
    _check_cancel(cancel_callback)
    context = dict(progress_context or {})
    display_country_total = int(context.get("country_total") or len(country_list))
    display_country_index = int(context.get("country_index") or 0)
    display_country_label = str(context.get("country_label") or "").strip()

    def country_message(country: str, index: int) -> str:
        if display_country_label:
            return display_country_label
        label = display_country_label or country
        label_index = display_country_index or index
        label_total = display_country_total or len(country_list)
        if label_total > 1 or label_index > 1:
            return f"{label} ({label_index}/{label_total})"
        return label

    all_cities: list[pd.DataFrame] = []
    all_timelines: list[pd.DataFrame] = []

    client = _build_client(cfg, proxy_list)
    _emit_progress(
        progress_callback,
        {
            "status": "running",
            "phase": "prepare",
            "message": f"{len(country_list)} ulke icin veri cekimi hazirlaniyor",
            "completed": completed_steps,
            "total": total_steps,
            "progress": 0.0,
        },
    )

    for index, country in enumerate(country_list, start=1):
        _check_cancel(cancel_callback)
        country_label = country_message(country, index)
        _emit_progress(
            progress_callback,
            {
                "status": "running",
                "phase": "country_start",
                "country": country,
                "message": f"{country_label} icin veri cekiliyor",
                "completed": completed_steps,
                "total": total_steps,
                "progress": completed_steps / total_steps,
            },
        )
        for attempt in range(1, cfg.max_attempt_per_country + 1):
            try:
                _check_cancel(cancel_callback)
                city_df = fetch_country_cities(client, country, cfg)
                completed_steps += 1
                _emit_progress(
                    progress_callback,
                    {
                        "status": "running",
                        "phase": "city_list",
                        "country": country,
                        "message": f"{country_label} sehir listesi alindi",
                        "completed": completed_steps,
                        "total": total_steps,
                        "progress": min(0.99, completed_steps / total_steps),
                    },
                )
                if city_df.empty:
                    break
                all_cities.append(city_df)

                for _, crow in city_df.iterrows():
                    _check_cancel(cancel_callback)
                    geo = crow["geo_code"]
                    if not geo:
                        completed_steps += 1
                        continue
                    try:
                        _emit_progress(
                            progress_callback,
                            {
                                "status": "running",
                                "phase": "city_timeline",
                                "country": country,
                                "city": crow["city"],
                                "message": f"{country_label} / {crow['city']} zaman serisi cekiliyor",
                                "completed": completed_steps,
                                "total": total_steps,
                                "progress": min(0.99, completed_steps / total_steps),
                            },
                        )
                        ts = fetch_timeline(client, geo, cfg)
                        if not ts.empty:
                            ts = ts.copy()
                            ts["country"] = country
                            ts["city"] = crow["city"]
                            ts["geo_code"] = geo
                            all_timelines.append(ts)
                    except Exception:
                        continue
                    finally:
                        completed_steps += 1
                    _check_cancel(cancel_callback)
                    _sleep(cfg)

                # Ülke seviyesinde zaman serisini de ekle.
                # Bu, ülke haritası / korelasyon gibi ülke genelinde analizlerde doğru temel sağlar.
                try:
                    _check_cancel(cancel_callback)
                    _emit_progress(
                        progress_callback,
                        {
                            "status": "running",
                            "phase": "country_timeline",
                            "country": country,
                            "message": f"{country_label} ulke bazli zaman serisi cekiliyor",
                            "completed": completed_steps,
                            "total": total_steps,
                            "progress": min(0.99, completed_steps / total_steps),
                        },
                    )
                    country_ts = fetch_timeline(client, country, cfg)
                    if not country_ts.empty:
                        country_ts = country_ts.copy()
                        country_ts["country"] = country
                        country_ts["city"] = ""
                        country_ts["geo_code"] = country
                        all_timelines.append(country_ts)
                except Exception:
                    pass
                finally:
                    completed_steps += 1

                break

            except Exception:
                if attempt == cfg.max_attempt_per_country:
                    break
                wait = min(90, (2 ** attempt) * random.uniform(2.0, 4.5))
                time.sleep(wait)
                _check_cancel(cancel_callback)
                client = _build_client(cfg, proxy_list)
            finally:
                _check_cancel(cancel_callback)
                _sleep(cfg)

    cities_df = pd.DataFrame(columns=["country", "city", "geo_code", "score"])
    timeline_df = pd.DataFrame(columns=["country", "city", "geo_code", "date", "score"])

    if all_cities:
        cities_df = (
            pd.concat(all_cities, ignore_index=True)
            .drop_duplicates(subset=["country", "city", "geo_code"], keep="last")
            .sort_values(["country", "score"], ascending=[True, False])
            .reset_index(drop=True)
        )

    if all_timelines:
        timeline_df = pd.concat(all_timelines, ignore_index=True)
        timeline_df["date"] = pd.to_datetime(timeline_df["date"])
        timeline_df = (
            timeline_df
            .drop_duplicates(subset=["country", "city", "geo_code", "date"], keep="last")
            .sort_values(["country", "city", "date"])
            .reset_index(drop=True)
        )

    _emit_progress(
        progress_callback,
        {
            "status": "running",
            "phase": "finalize",
            "message": "Veri birlestiriliyor",
            "completed": total_steps,
            "total": total_steps,
            "progress": 0.99,
        },
    )

    return cities_df, timeline_df


def fetch_trends_dataset_country_keywords(
    countries: Iterable[str],
    keyword_by_country: dict[str, str],
    default_keyword: str,
    config: FetchConfig | None = None,
    proxies: list[str] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetches trends data per country using country-specific keywords.

    If a country keyword is missing, falls back to the default keyword.
    """
    base_cfg = config or FetchConfig()
    country_list = list(countries)
    all_cities: list[pd.DataFrame] = []
    all_timeline: list[pd.DataFrame] = []
    total_steps = max(1, len(country_list) * (base_cfg.top_cities_per_country + 2))
    _check_cancel(cancel_callback)

    _emit_progress(
        progress_callback,
        {
            "status": "running",
            "phase": "prepare",
            "message": f"{len(country_list)} ulke icin ozel keyword cekimi hazirlaniyor",
            "completed": 0,
            "total": total_steps,
            "progress": 0.0,
        },
    )

    for index, country in enumerate(country_list, start=1):
        _check_cancel(cancel_callback)
        kw = str(keyword_by_country.get(country, "")).strip() or str(default_keyword).strip() or DEFAULT_KEYWORD
        cfg = FetchConfig(**{**base_cfg.__dict__, "keyword": kw})
        country_start = (index - 1) / max(1, len(country_list))
        country_span = 1 / max(1, len(country_list))
        country_label = f"{country} ({index}/{len(country_list)})"

        _emit_progress(
            progress_callback,
            {
                "status": "running",
                "phase": "country_keyword",
                "country": country,
                "keyword": kw,
                "country_index": index,
                "country_total": len(country_list),
                "message": f"{country_label} icin {kw} ile veri cekiliyor",
                "completed": int(country_start * total_steps),
                "total": total_steps,
                "progress": country_start,
            },
        )

        def country_progress_callback(payload: dict, *, _start=country_start, _span=country_span) -> None:
            _check_cancel(cancel_callback)
            data = dict(payload or {})
            inner_progress = float(data.get("progress") or 0.0)
            overall_progress = min(0.99, _start + inner_progress * _span)
            phase = str(data.get("phase") or "")
            if phase == "country_start":
                data["message"] = f"{country_label} icin {kw} ile veri cekiliyor"
            elif phase == "city_list":
                data["message"] = f"{country_label} sehir listesi aliniyor"
            elif phase == "city_timeline" and data.get("city"):
                data["message"] = f"{country_label} / {data['city']} zaman serisi cekiliyor"
            elif phase == "country_timeline":
                data["message"] = f"{country_label} ulke bazli zaman serisi cekiliyor"
            elif phase == "finalize":
                data["message"] = "Ozel keyword verisi birlestiriliyor"
            data["progress"] = overall_progress
            data["total"] = total_steps
            data["completed"] = min(total_steps, max(0, int(overall_progress * total_steps)))
            data.setdefault("country", country)
            data.setdefault("keyword", kw)
            data.setdefault("country_index", index)
            data.setdefault("country_total", len(country_list))
            _emit_progress(progress_callback, data)

        cities_df, timeline_df = fetch_trends_dataset(
            countries=[country],
            config=cfg,
            proxies=proxies,
            progress_callback=country_progress_callback,
            progress_context={
                "country_index": index,
                "country_total": len(country_list),
                "country_label": country_label,
            },
            cancel_callback=cancel_callback,
        )
        if not cities_df.empty:
            all_cities.append(cities_df)
        if not timeline_df.empty:
            all_timeline.append(timeline_df)

    if not all_cities:
        cities_out = pd.DataFrame(columns=["country", "city", "geo_code", "score"])
    else:
        cities_out = pd.concat(all_cities, ignore_index=True)
        cities_out = cities_out.drop_duplicates(subset=["country", "city", "geo_code"], keep="last")

    if not all_timeline:
        timeline_out = pd.DataFrame(columns=["country", "city", "geo_code", "date", "score"])
    else:
        timeline_out = pd.concat(all_timeline, ignore_index=True)
        timeline_out["date"] = pd.to_datetime(timeline_out["date"], errors="coerce")
        timeline_out = timeline_out.dropna(subset=["date"])
        timeline_out = timeline_out.drop_duplicates(subset=["country", "city", "geo_code", "date"], keep="last")
        timeline_out = timeline_out.sort_values(["country", "city", "date"]).reset_index(drop=True)

    _emit_progress(
        progress_callback,
        {
            "status": "running",
            "phase": "finalize",
            "message": "Ozel keyword verisi birlestiriliyor",
            "completed": total_steps,
            "total": total_steps,
            "progress": 0.99,
        },
    )

    return cities_out, timeline_out


# ── Saatlik Veri ──────────────────────────────────────────────
def fetch_hourly_data(
    geo: str,
    config: FetchConfig | None = None,
    proxies: list[str] | None = None,
) -> pd.DataFrame:
    """Son 7 günlük saatlik veri çeker."""
    cfg = config or FetchConfig()
    kw = cfg.keyword
    proxy_list = proxies or PROXIES or None

    if cfg.use_cache:
        key = _cache_key("hourly", geo=geo, topic=kw)
        cached = _cache_get(key, cfg.cache_ttl_hours)
        if cached is not None:
            return cached

    client = _build_client(cfg, proxy_list)

    try:
        client.build_payload([kw], cat=0, timeframe=cfg.timeframe_7d, geo=geo, gprop="")
    except Exception:
        return pd.DataFrame(columns=["datetime", "score", "hour", "dayofweek"])

    try:
        ts = client.interest_over_time()
    except Exception:
        return pd.DataFrame(columns=["datetime", "score", "hour", "dayofweek"])

    if ts.empty:
        return pd.DataFrame(columns=["datetime", "score", "hour", "dayofweek"])

    col = _score_col(ts)
    result = pd.DataFrame({"datetime": ts.index, "score": ts[col].values})
    result["datetime"] = pd.to_datetime(result["datetime"])
    result["hour"] = result["datetime"].dt.hour
    result["dayofweek"] = result["datetime"].dt.dayofweek

    if cfg.use_cache and not result.empty:
        _cache_set(_cache_key("hourly", geo=geo, topic=kw), result)

    return result


# ── Snapshot Kaydetme ─────────────────────────────────────────
def save_snapshot(
    cities_df: pd.DataFrame,
    timeline_df: pd.DataFrame,
    root: str | Path | None = None,
    keyword: str = "",
) -> tuple[Path, Path]:
    """Persist the single active dataset and its metadata."""
    del root

    cities_path = DEFAULT_CITIES_CSV
    timeline_path = DEFAULT_TIMELINE_CSV

    cities_path.parent.mkdir(parents=True, exist_ok=True)
    cities_df.to_csv(cities_path, index=False)
    timeline_df.to_csv(timeline_path, index=False)
    _write_dataset_metadata(
        keyword=keyword or DEFAULT_KEYWORD,
        countries=sorted(cities_df["country"].dropna().astype(str).unique().tolist()) if not cities_df.empty else [],
        cities_df=cities_df,
        timeline_df=timeline_df,
    )

    if SAVE_REPORT_HISTORY:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        manifest = REPORTS_DIR / "latest_dataset.json"
        manifest.write_text(
            json.dumps(
                {
                    "keyword": keyword or DEFAULT_KEYWORD,
                    "cities_file": str(cities_path.relative_to(cities_path.parent.parent.parent)),
                    "timeline_file": str(timeline_path.relative_to(timeline_path.parent.parent.parent)),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _prune_files(REPORTS_DIR, "*.json", max(1, MAX_REPORT_FILES))

    return cities_path, timeline_path


def clear_cache() -> int:
    """Cache dosyalarını temizler. Silinen dosya sayısını döner."""
    count = 0
    for f in CACHE_DIR.glob("*.parquet"):
        f.unlink(missing_ok=True)
        count += 1
    return count
