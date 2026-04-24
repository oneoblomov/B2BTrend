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

from collections import defaultdict
import logging
import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
from urllib3.util.retry import Retry

from src.config import (
    CACHE_DIR,
    COOKIE_PROFILE_FILE,
    COOKIE_PROFILE_TTL_HOURS,
    DATASET_META_FILE,
    DEFAULT_KEYWORD,
    DEFAULT_CITIES_CSV,
    DEFAULT_TIMELINE_CSV,
    ENABLE_COOKIE_PROFILE,
    ENABLE_FETCH_CACHE,
    FETCH_BURST_EVERY,
    FETCH_BURST_MAX_SLEEP,
    FETCH_BURST_MIN_SLEEP,
    FETCH_CACHE_TTL_HOURS,
    FETCH_MAX_SLEEP,
    FETCH_MIN_SLEEP,
    FETCH_PHASE_CITY_LIST_FACTOR,
    FETCH_PHASE_CITY_TIMELINE_FACTOR,
    FETCH_PHASE_COUNTRY_FACTOR,
    FETCH_RETRY_COOLDOWN_BASE,
    FETCH_RETRY_COOLDOWN_MAX,
    MAX_CACHE_FILES,
    MAX_REPORT_FILES,
    PROXIES,
    REPORTS_DIR,
    SAVE_REPORT_HISTORY,
    TOP_20_COUNTRIES,
    USER_AGENTS,
    USER_AGENT,
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


logger = logging.getLogger(__name__)


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
    top_cities_per_country: int = 100
    min_sleep_sec: float = FETCH_MIN_SLEEP
    max_sleep_sec: float = FETCH_MAX_SLEEP
    use_cache: bool = ENABLE_FETCH_CACHE
    cache_ttl_hours: int = FETCH_CACHE_TTL_HOURS
    user_agent: str | None = None
    enable_cookie_profile: bool = ENABLE_COOKIE_PROFILE
    cookie_profile_ttl_hours: int = COOKIE_PROFILE_TTL_HOURS
    phase_country_factor: float = FETCH_PHASE_COUNTRY_FACTOR
    phase_city_list_factor: float = FETCH_PHASE_CITY_LIST_FACTOR
    phase_city_timeline_factor: float = FETCH_PHASE_CITY_TIMELINE_FACTOR
    burst_every: int = FETCH_BURST_EVERY
    burst_min_sleep_sec: float = FETCH_BURST_MIN_SLEEP
    burst_max_sleep_sec: float = FETCH_BURST_MAX_SLEEP
    retry_cooldown_base_sec: float = FETCH_RETRY_COOLDOWN_BASE
    retry_cooldown_max_sec: float = FETCH_RETRY_COOLDOWN_MAX


# ── Yardımcılar ───────────────────────────────────────────────
def get_random_user_agent() -> str:
    """Rastgele bir User-Agent döndürür. USER_AGENT env varsa onu kullanır."""
    if USER_AGENT:
        return USER_AGENT
    return random.choice(USER_AGENTS)


def _cookie_profile_payload_valid(payload: dict[str, Any], ttl_hours: int) -> bool:
    updated_at = str(payload.get("updated_at") or "").strip()
    if not updated_at:
        return False
    try:
        updated_dt = datetime.fromisoformat(updated_at)
    except Exception:
        return False
    age_hours = (datetime.now() - updated_dt).total_seconds() / 3600
    if age_hours > max(1, ttl_hours):
        return False
    cookies = payload.get("cookies")
    return isinstance(cookies, dict) and bool(cookies)


def _load_cookie_profile(ttl_hours: int) -> dict[str, str] | None:
    try:
        if not COOKIE_PROFILE_FILE.exists():
            return None
        payload = json.loads(COOKIE_PROFILE_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if not _cookie_profile_payload_valid(payload, ttl_hours):
            return None
        cookies = payload.get("cookies") or {}
        out: dict[str, str] = {}
        for key, value in cookies.items():
            ck = str(key).strip()
            cv = str(value).strip()
            if ck and cv:
                out[ck] = cv
        return out or None
    except Exception:
        return None


def _save_cookie_profile(cookies: dict[str, str], user_agent: str, hl: str) -> None:
    safe_cookies: dict[str, str] = {}
    for key, value in (cookies or {}).items():
        ck = str(key).strip()
        cv = str(value).strip()
        if ck and cv:
            safe_cookies[ck] = cv
    if not safe_cookies:
        return

    payload = {
        "cookies": safe_cookies,
        "user_agent": user_agent,
        "hl": hl,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        COOKIE_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_PROFILE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _apply_realistic_headers(client: TrendReq, user_agent: str, hl: str) -> None:
    language = str(hl or "en-US").strip() or "en-US"
    primary_lang = language.split("-")[0].lower()
    client.headers["User-Agent"] = user_agent
    client.headers["accept-language"] = f"{language},{primary_lang};q=0.9,en;q=0.8"
    client.headers["Accept"] = "application/json, text/plain, */*"
    client.headers["Referer"] = "https://trends.google.com/trends/"
    client.headers["Origin"] = "https://trends.google.com"
    client.headers["sec-ch-ua-mobile"] = "?0"
    client.headers["sec-fetch-site"] = "same-origin"
    client.headers["sec-fetch-mode"] = "cors"
    client.headers["sec-fetch-dest"] = "empty"


class FetchScheduler:
    """Adaptive pacing to reduce burst traffic and IP throttling risk."""

    def __init__(self, cfg: FetchConfig) -> None:
        self.cfg = cfg
        self.request_count = 0
        self.error_streak = 0

    def _phase_factor(self, phase: str) -> float:
        if phase == "country_timeline":
            return max(0.25, self.cfg.phase_country_factor)
        if phase == "city_list":
            return max(0.25, self.cfg.phase_city_list_factor)
        return max(0.25, self.cfg.phase_city_timeline_factor)

    def before_request(self, phase: str, retry_attempt: int = 0) -> None:
        base_min = max(0.05, float(self.cfg.min_sleep_sec))
        base_max = max(base_min, float(self.cfg.max_sleep_sec))
        factor = self._phase_factor(phase)
        delay = random.uniform(base_min, base_max) * factor

        if self.error_streak > 0:
            delay *= 1.0 + min(2.0, self.error_streak * 0.25)

        if retry_attempt > 0:
            retry_delay = min(
                float(self.cfg.retry_cooldown_max_sec),
                float(self.cfg.retry_cooldown_base_sec) * (2 ** (retry_attempt - 1)),
            )
            delay += random.uniform(retry_delay * 0.85, retry_delay * 1.15)

        time.sleep(delay)
        self.request_count += 1

        if self.cfg.burst_every > 0 and self.request_count % self.cfg.burst_every == 0:
            burst_min = max(0.1, float(self.cfg.burst_min_sleep_sec))
            burst_max = max(burst_min, float(self.cfg.burst_max_sleep_sec))
            time.sleep(random.uniform(burst_min, burst_max))

    def mark_success(self) -> None:
        self.error_streak = max(0, self.error_streak - 1)

    def mark_error(self) -> None:
        self.error_streak = min(10, self.error_streak + 1)


def _build_client(cfg: FetchConfig, proxies: list[str] | None = None) -> TrendReq:
    ua = cfg.user_agent or get_random_user_agent()
    kwargs = {
        "hl": cfg.hl,
        "tz": cfg.tz,
        "retries": cfg.retries,
        "backoff_factor": cfg.backoff_factor,
        "rotate_user_agent": False,
    }
    if proxies:
        kwargs["proxies"] = proxies

    client = TrendReq(**kwargs)

    logger.info(
        "Trend client ready: hl=%s tz=%s proxies=%s user_agent=%s",
        cfg.hl,
        cfg.tz,
        len(proxies or []),
        "custom" if cfg.user_agent else "random",
    )
    
    _apply_realistic_headers(client, ua, cfg.hl)

    if cfg.enable_cookie_profile:
        cached_cookies = _load_cookie_profile(cfg.cookie_profile_ttl_hours)
        if cached_cookies:
            client.cookies = cached_cookies

    original_get_cookie = client._get_google_cookie

    def _get_cookie_with_profile() -> dict[str, str]:
        if cfg.enable_cookie_profile:
            cached = _load_cookie_profile(cfg.cookie_profile_ttl_hours)
            if cached:
                return cached

        cookies = original_get_cookie()
        if cfg.enable_cookie_profile and cookies:
            _save_cookie_profile(cookies, user_agent=ua, hl=cfg.hl)
        return cookies

    client._get_user_agent = lambda: ua
    client._get_google_cookie = _get_cookie_with_profile

    if cfg.enable_cookie_profile and client.cookies:
        _save_cookie_profile(client.cookies, user_agent=ua, hl=cfg.hl)

    return client


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
    del key, df
    return


def _prune_files(directory: Path, pattern: str, keep: int) -> None:
    if keep <= 0 or not directory.exists():
        return
    files = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    for old_file in files[keep:]:
        old_file.unlink(missing_ok=True)


def _write_dataset_metadata(keyword: str, countries: Iterable[str], cities_df: pd.DataFrame, timeline_df: pd.DataFrame) -> None:
    del keyword, countries, cities_df, timeline_df
    return


# ── Tekil Çekme Fonksiyonları ─────────────────────────────────
def fetch_country_cities(
    client: TrendReq,
    country: str,
    cfg: FetchConfig,
    *,
    scheduler: FetchScheduler | None = None,
    retry_attempt: int = 0,
) -> pd.DataFrame:
    """Bir ülkenin top şehirlerinin ilgi skorunu çeker."""
    kw = cfg.keyword

    if cfg.use_cache:
        key = _cache_key("cities", country=country, topic=kw, tf=cfg.timeframe_12m)
        cached = _cache_get(key, cfg.cache_ttl_hours)
        if cached is not None:
            logger.info(
                "Cities cache hit: country=%s keyword=%s timeframe=%s rows=%s",
                country,
                kw,
                cfg.timeframe_12m,
                len(cached),
            )
            return cached

    logger.info(
        "Fetching cities: country=%s keyword=%s timeframe=%s",
        country,
        kw,
        cfg.timeframe_12m,
    )
    try:
        if scheduler:
            scheduler.before_request("city_list", retry_attempt=retry_attempt)
        client.build_payload([kw], cat=0, timeframe=cfg.timeframe_12m, geo=country, gprop="")
        region_df = client.interest_by_region(resolution="CITY", inc_low_vol=False, inc_geo_code=True)
        if scheduler:
            scheduler.mark_success()
    except Exception:
        if scheduler:
            scheduler.mark_error()
        logger.exception(
            "City fetch failed: country=%s keyword=%s timeframe=%s",
            country,
            kw,
            cfg.timeframe_12m,
        )
        raise

    if region_df.empty:
        logger.warning(
            "No city data returned: country=%s keyword=%s timeframe=%s",
            country,
            kw,
            cfg.timeframe_12m,
        )
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
    *,
    scheduler: FetchScheduler | None = None,
    phase: str = "city_timeline",
    retry_attempt: int = 0,
) -> pd.DataFrame:
    """Belirli bir geo için zaman serisi çeker."""
    tf = timeframe or cfg.timeframe_12m
    kw = cfg.keyword

    if cfg.use_cache:
        key = _cache_key("timeline", geo=geo, topic=kw, tf=tf)
        cached = _cache_get(key, cfg.cache_ttl_hours)
        if cached is not None:
            logger.info(
                "Timeline cache hit: geo=%s keyword=%s timeframe=%s rows=%s",
                geo,
                kw,
                tf,
                len(cached),
            )
            return cached

    logger.info("Fetching timeline: geo=%s keyword=%s timeframe=%s", geo, kw, tf)
    try:
        if scheduler:
            scheduler.before_request(phase, retry_attempt=retry_attempt)
        client.build_payload([kw], cat=0, timeframe=tf, geo=geo, gprop="")
        ts = client.interest_over_time()
        if scheduler:
            scheduler.mark_success()
    except Exception:
        if scheduler:
            scheduler.mark_error()
        logger.exception("Timeline fetch failed: geo=%s keyword=%s timeframe=%s", geo, kw, tf)
        raise

    if ts.empty:
        logger.warning("No timeline data returned: geo=%s keyword=%s timeframe=%s", geo, kw, tf)
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
    seed_cities: pd.DataFrame | None = None,
    seed_timeline: pd.DataFrame | None = None,
    resume_state: dict[str, Any] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    partial_save_callback: Callable[[pd.DataFrame, pd.DataFrame, dict[str, Any]], None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch country timelines and city score lists with optional resume/checkpoint support."""
    cfg = config or FetchConfig()
    country_list = list(countries or TOP_20_COUNTRIES)
    proxy_list = proxies or PROXIES or None
    scheduler = FetchScheduler(cfg)

    city_cols = ["country", "city", "geo_code", "score"]
    timeline_cols = ["country", "city", "geo_code", "date", "score"]

    existing_cities = seed_cities.copy() if isinstance(seed_cities, pd.DataFrame) else pd.DataFrame(columns=city_cols)
    existing_timeline = seed_timeline.copy() if isinstance(seed_timeline, pd.DataFrame) else pd.DataFrame(columns=timeline_cols)

    for col in city_cols:
        if col not in existing_cities.columns:
            existing_cities[col] = pd.NA
    for col in timeline_cols:
        if col not in existing_timeline.columns:
            existing_timeline[col] = pd.NA

    existing_cities = existing_cities[city_cols]
    existing_timeline = existing_timeline[timeline_cols]
    if not existing_timeline.empty:
        existing_timeline["date"] = pd.to_datetime(existing_timeline["date"], errors="coerce")
        existing_timeline = existing_timeline.dropna(subset=["date"]).reset_index(drop=True)

    all_cities: list[pd.DataFrame] = [existing_cities] if not existing_cities.empty else []
    all_timelines: list[pd.DataFrame] = [existing_timeline] if not existing_timeline.empty else []

    country_city_map: dict[str, pd.DataFrame] = {}
    if not existing_cities.empty:
        for country, cdf in existing_cities.groupby("country", dropna=False):
            key = str(country).strip()
            if key:
                country_city_map[key] = cdf.reset_index(drop=True).copy()

    existing_country_timelines: set[str] = set()
    if not existing_timeline.empty:
        tmp = existing_timeline.copy()
        tmp["country"] = tmp["country"].astype(str).str.strip()
        tmp["city"] = tmp["city"].fillna("").astype(str).str.strip()
        tmp["geo_code"] = tmp["geo_code"].fillna("").astype(str).str.strip()
        existing_country_timelines = set(tmp[tmp["city"] == ""]["country"].tolist())

    resume_payload = dict(resume_state or {})
    completed_country_timelines = {str(item).strip() for item in resume_payload.get("completed_country_timelines", []) if str(item).strip()}
    completed_city_lists = {str(item).strip() for item in resume_payload.get("completed_city_lists", []) if str(item).strip()}

    if resume_payload:
        completed_country_timelines = {item for item in completed_country_timelines if item in existing_country_timelines}
        completed_city_lists = {item for item in completed_city_lists if item in country_city_map}

        completed_country_timelines |= existing_country_timelines
        completed_city_lists |= set(country_city_map.keys())

    total_steps = max(1, len(country_list) * 2)
    completed_steps = len(completed_country_timelines) + len(completed_city_lists)
    completed_steps = min(total_steps, completed_steps)

    def _materialize() -> tuple[pd.DataFrame, pd.DataFrame]:
        cities_df = pd.DataFrame(columns=city_cols)
        timeline_df = pd.DataFrame(columns=timeline_cols)

        if all_cities:
            cities_df = pd.concat(all_cities, ignore_index=True)
            for col in city_cols:
                if col not in cities_df.columns:
                    cities_df[col] = pd.NA
            cities_df = (
                cities_df[city_cols]
                .drop_duplicates(subset=["country", "city", "geo_code"], keep="last")
                .sort_values(["country", "score"], ascending=[True, False])
                .reset_index(drop=True)
            )

        if all_timelines:
            timeline_df = pd.concat(all_timelines, ignore_index=True)
            for col in timeline_cols:
                if col not in timeline_df.columns:
                    timeline_df[col] = pd.NA
            timeline_df = timeline_df[timeline_cols]
            timeline_df["date"] = pd.to_datetime(timeline_df["date"], errors="coerce")
            timeline_df = (
                timeline_df
                .dropna(subset=["date"])
                .drop_duplicates(subset=["country", "city", "geo_code", "date"], keep="last")
                .sort_values(["country", "city", "date"])
                .reset_index(drop=True)
            )

        return cities_df, timeline_df

    def _checkpoint_payload(phase: str, country: str = "", city: str = "") -> dict[str, Any]:
        progress = min(0.99, completed_steps / max(1, total_steps))
        return {
            "phase": phase,
            "country": country,
            "city": city,
            "completed_steps": int(completed_steps),
            "total_steps": int(total_steps),
            "progress": float(progress),
            "completed_country_timelines": sorted(completed_country_timelines),
            "completed_city_lists": sorted(completed_city_lists),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _emit_checkpoint(phase: str, country: str = "", city: str = "") -> dict[str, Any]:
        payload = _checkpoint_payload(phase=phase, country=country, city=city)
        if checkpoint_callback:
            try:
                checkpoint_callback(payload)
            except Exception:
                pass
        return payload

    def _persist_partial(phase: str, country: str = "", city: str = "") -> None:
        if not partial_save_callback:
            return
        try:
            cities_df, timeline_df = _materialize()
            partial_save_callback(cities_df, timeline_df, _checkpoint_payload(phase=phase, country=country, city=city))
        except Exception:
            logger.exception("Partial dataset save failed: phase=%s country=%s city=%s", phase, country, city)

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

    client = _build_client(cfg, proxy_list)
    resume_note = f" ({completed_steps} adim kaldigi yerden)" if completed_steps > 0 else ""
    _emit_progress(
        progress_callback,
        {
            "status": "running",
            "phase": "prepare",
            "message": f"{len(country_list)} ulke icin veri cekimi hazirlaniyor{resume_note}",
            "completed": completed_steps,
            "total": total_steps,
            "progress": min(0.99, completed_steps / total_steps),
        },
    )
    _emit_checkpoint("prepare")

    # Phase 1: Fetch country-level timeline first (high-priority summary data)
    for index, country in enumerate(country_list, start=1):
        _check_cancel(cancel_callback)
        country_label = country_message(country, index)

        if country in completed_country_timelines:
            _emit_progress(
                progress_callback,
                {
                    "status": "running",
                    "phase": "country_timeline",
                    "country": country,
                    "message": f"{country_label} ulke zaman serisi daha once alinmis, atlaniyor",
                    "completed": completed_steps,
                    "total": total_steps,
                    "progress": min(0.99, completed_steps / total_steps),
                },
            )
            continue

        for attempt in range(1, cfg.max_attempt_per_country + 1):
            try:
                _check_cancel(cancel_callback)
                ts = fetch_timeline(
                    client,
                    country,
                    cfg,
                    scheduler=scheduler,
                    phase="country_timeline",
                    retry_attempt=attempt - 1,
                )
                if not ts.empty:
                    ts = ts.copy()
                    ts["country"] = country
                    ts["city"] = ""
                    ts["geo_code"] = country
                    all_timelines.append(ts)

                completed_country_timelines.add(country)
                completed_steps = min(total_steps, completed_steps + 1)

                _emit_progress(
                    progress_callback,
                    {
                        "status": "running",
                        "phase": "country_timeline",
                        "country": country,
                        "message": f"{country_label} ulke bazli zaman serisi alindi",
                        "completed": completed_steps,
                        "total": total_steps,
                        "progress": min(0.99, completed_steps / total_steps),
                    },
                )
                _emit_checkpoint("country_timeline", country=country)
                _persist_partial("country_timeline", country=country)
                break
            except Exception:
                if attempt == cfg.max_attempt_per_country:
                    logger.warning("Country timeline fetch skipped after retries: %s", country)
                else:
                    client = _build_client(cfg, proxy_list)
                _check_cancel(cancel_callback)

    # Phase 2: Fetch city lists by country
    for index, country in enumerate(country_list, start=1):
        _check_cancel(cancel_callback)
        country_label = country_message(country, index)

        if country in completed_city_lists:
            if country not in country_city_map:
                country_city_map[country] = pd.DataFrame(columns=city_cols)
            _emit_progress(
                progress_callback,
                {
                    "status": "running",
                    "phase": "city_list",
                    "country": country,
                    "message": f"{country_label} sehir listesi daha once alinmis, atlaniyor",
                    "completed": completed_steps,
                    "total": total_steps,
                    "progress": min(0.99, completed_steps / total_steps),
                },
            )
            continue

        for attempt in range(1, cfg.max_attempt_per_country + 1):
            try:
                _check_cancel(cancel_callback)
                city_df = fetch_country_cities(
                    client,
                    country,
                    cfg,
                    scheduler=scheduler,
                    retry_attempt=attempt - 1,
                )
                if not city_df.empty:
                    all_cities.append(city_df)
                country_city_map[country] = city_df.copy()
                completed_city_lists.add(country)
                completed_steps = min(total_steps, completed_steps + 1)

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
                _emit_checkpoint("city_list", country=country)
                _persist_partial("city_list", country=country)
                break
            except Exception:
                if attempt == cfg.max_attempt_per_country:
                    logger.warning("City list fetch skipped after retries: %s", country)
                    country_city_map[country] = pd.DataFrame(columns=city_cols)
                else:
                    client = _build_client(cfg, proxy_list)
                _check_cancel(cancel_callback)

    cities_df, timeline_df = _materialize()

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
    _emit_checkpoint("finalize")

    return cities_df, timeline_df


def fetch_trends_dataset_country_keywords(
    countries: Iterable[str],
    keyword_by_country: dict[str, str],
    default_keyword: str,
    config: FetchConfig | None = None,
    proxies: list[str] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    seed_cities: pd.DataFrame | None = None,
    seed_timeline: pd.DataFrame | None = None,
    resume_state: dict[str, Any] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    partial_save_callback: Callable[[pd.DataFrame, pd.DataFrame, dict[str, Any]], None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetches trends data per country using country-specific keywords.

    If a country keyword is missing, falls back to the default keyword.
    """
    base_cfg = config or FetchConfig()
    country_list = list(countries)
    city_cols = ["country", "city", "geo_code", "score"]
    timeline_cols = ["country", "city", "geo_code", "date", "score"]

    existing_cities = seed_cities.copy() if isinstance(seed_cities, pd.DataFrame) else pd.DataFrame(columns=city_cols)
    existing_timeline = seed_timeline.copy() if isinstance(seed_timeline, pd.DataFrame) else pd.DataFrame(columns=timeline_cols)

    for col in city_cols:
        if col not in existing_cities.columns:
            existing_cities[col] = pd.NA
    for col in timeline_cols:
        if col not in existing_timeline.columns:
            existing_timeline[col] = pd.NA

    existing_cities = existing_cities[city_cols]
    existing_timeline = existing_timeline[timeline_cols]
    if not existing_timeline.empty:
        existing_timeline["date"] = pd.to_datetime(existing_timeline["date"], errors="coerce")
        existing_timeline = existing_timeline.dropna(subset=["date"]).reset_index(drop=True)

    all_cities: list[pd.DataFrame] = [existing_cities] if not existing_cities.empty else []
    all_timeline: list[pd.DataFrame] = [existing_timeline] if not existing_timeline.empty else []

    resume_payload = dict(resume_state or {})
    completed_countries = {str(item).strip() for item in resume_payload.get("completed_countries", []) if str(item).strip()}
    per_country_resume = resume_payload.get("per_country") if isinstance(resume_payload.get("per_country"), dict) else {}

    existing_completed_countries: set[str] = set()
    if not existing_timeline.empty:
        tmp = existing_timeline.copy()
        tmp["country"] = tmp["country"].astype(str).str.strip()
        tmp["city"] = tmp["city"].fillna("").astype(str).str.strip()
        existing_completed_countries = set(tmp[tmp["city"] == ""]["country"].tolist())

    if resume_payload:
        completed_countries = {item for item in completed_countries if item in existing_completed_countries}
    completed_countries |= existing_completed_countries

    total_steps = max(1, len(country_list) * 2)
    running_country_state: dict[str, Any] = {}

    def _materialize() -> tuple[pd.DataFrame, pd.DataFrame]:
        if not all_cities:
            cities_out = pd.DataFrame(columns=city_cols)
        else:
            cities_out = pd.concat(all_cities, ignore_index=True)
            cities_out = cities_out[city_cols].drop_duplicates(subset=["country", "city", "geo_code"], keep="last")

        if not all_timeline:
            timeline_out = pd.DataFrame(columns=timeline_cols)
        else:
            timeline_out = pd.concat(all_timeline, ignore_index=True)
            timeline_out["date"] = pd.to_datetime(timeline_out["date"], errors="coerce")
            timeline_out = (
                timeline_out
                .dropna(subset=["date"])
                .drop_duplicates(subset=["country", "city", "geo_code", "date"], keep="last")
                .sort_values(["country", "city", "date"])
                .reset_index(drop=True)
            )
        return cities_out, timeline_out

    def _emit_checkpoint(active_country: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": "country_keywords",
            "completed_countries": sorted(completed_countries),
            "active_country": active_country,
            "per_country": dict(running_country_state),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if checkpoint_callback:
            try:
                checkpoint_callback(payload)
            except Exception:
                pass
        return payload

    def _persist_partial(active_country: str = "") -> None:
        if not partial_save_callback:
            return
        try:
            cities_out, timeline_out = _materialize()
            partial_save_callback(cities_out, timeline_out, _emit_checkpoint(active_country=active_country))
        except Exception:
            logger.exception("Partial dataset save failed in country keyword mode: country=%s", active_country)

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

        if country in completed_countries:
            _emit_progress(
                progress_callback,
                {
                    "status": "running",
                    "phase": "country_keyword",
                    "country": country,
                    "country_index": index,
                    "country_total": len(country_list),
                    "message": f"{country} ({index}/{len(country_list)}) daha once tamamlandi, atlaniyor",
                    "completed": int((index - 1) * total_steps / max(1, len(country_list))),
                    "total": total_steps,
                    "progress": min(0.99, (index - 1) / max(1, len(country_list))),
                },
            )
            continue

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
            running_country_state[country] = {
                "phase": str(data.get("phase") or "running"),
                "progress": float(data.get("progress") or 0.0),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            _emit_checkpoint(active_country=country)
            _emit_progress(progress_callback, data)

        country_seed_cities = existing_cities[existing_cities["country"] == country].copy() if not existing_cities.empty else pd.DataFrame(columns=city_cols)
        country_seed_timeline = existing_timeline[existing_timeline["country"] == country].copy() if not existing_timeline.empty else pd.DataFrame(columns=timeline_cols)

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
            seed_cities=country_seed_cities,
            seed_timeline=country_seed_timeline,
            resume_state=per_country_resume.get(country) if isinstance(per_country_resume, dict) else None,
            checkpoint_callback=lambda payload, *, _country=country: running_country_state.__setitem__(_country, payload),
        )
        if not cities_df.empty:
            all_cities.append(cities_df)
        if not timeline_df.empty:
            all_timeline.append(timeline_df)

        completed_countries.add(country)
        running_country_state[country] = {
            "phase": "completed",
            "progress": 1.0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _emit_checkpoint(active_country=country)
        _persist_partial(active_country=country)

    cities_out, timeline_out = _materialize()

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
    _emit_checkpoint(active_country="")

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
            logger.info("Hourly cache hit: geo=%s keyword=%s rows=%s", geo, kw, len(cached))
            return cached

    client = _build_client(cfg, proxy_list)

    try:
        client.build_payload([kw], cat=0, timeframe=cfg.timeframe_7d, geo=geo, gprop="")
    except Exception:
        logger.exception("Hourly payload build failed: geo=%s keyword=%s", geo, kw)
        return pd.DataFrame(columns=["datetime", "score", "hour", "dayofweek"])

    try:
        ts = client.interest_over_time()
    except Exception:
        logger.exception("Hourly timeline fetch failed: geo=%s keyword=%s", geo, kw)
        return pd.DataFrame(columns=["datetime", "score", "hour", "dayofweek"])

    if ts.empty:
        logger.warning("No hourly data returned: geo=%s keyword=%s", geo, kw)
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
    del cities_df, timeline_df, root, keyword
    return Path("browser://dataset/cities.csv"), Path("browser://dataset/timeline.csv")


def clear_cache() -> int:
    return 0
