"""Central application configuration.

Loads environment variables, manages storage paths, and exposes a single
localization interface for the full application.
"""
from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv

SRC_ROOT = Path(__file__).resolve().parent
ROOT = SRC_ROOT.parent

for _env_file in (ROOT / ".env", SRC_ROOT / ".env"):
    if _env_file.exists():
        load_dotenv(_env_file)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = ROOT / "data"
ACTIVE_DATA_DIR = DATA_DIR / "active"
CACHE_DIR = DATA_DIR / "cache"
REPORTS_DIR = DATA_DIR / "reports"
LOCALES_DIR = ROOT / "locales"
GEOCACHE_FILE = DATA_DIR / "city_geocache.csv"
DATASET_META_FILE = ACTIVE_DATA_DIR / "dataset_meta.json"

LEGACY_DATA_DIR = SRC_ROOT / "data"
LEGACY_GEOCACHE_FILE = LEGACY_DATA_DIR / "city_geocache.csv"
LEGACY_CITIES_CSV = SRC_ROOT / "poultry_trends_cities.csv"
LEGACY_TIMELINE_CSV = SRC_ROOT / "poultry_trends_timeline.csv"

DEFAULT_CITIES_CSV = ACTIVE_DATA_DIR / "poultry_trends_cities.csv"
DEFAULT_TIMELINE_CSV = ACTIVE_DATA_DIR / "poultry_trends_timeline.csv"

for _directory in (DATA_DIR, ACTIVE_DATA_DIR, CACHE_DIR, REPORTS_DIR, LOCALES_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

DEFAULT_LANGUAGE = os.getenv("APP_DEFAULT_LANGUAGE", "tr")
DEFAULT_KEYWORD = os.getenv("DEFAULT_KEYWORD", "/m/02vqb5x")

ENABLE_FETCH_CACHE = _env_flag("ENABLE_FETCH_CACHE", True)
FETCH_CACHE_TTL_HOURS = int(os.getenv("FETCH_CACHE_TTL_HOURS", "12"))
MAX_CACHE_FILES = int(os.getenv("MAX_CACHE_FILES", "250"))
SAVE_REPORT_HISTORY = _env_flag("SAVE_REPORT_HISTORY", True)
MAX_REPORT_FILES = int(os.getenv("MAX_REPORT_FILES", "25"))

SUPPORTED_LANGUAGES: dict[str, str] = {
    "tr": "Turkce",
    "en": "English",
    "de": "Deutsch",
    "fr": "Francais",
    "es": "Espanol",
    "pt": "Portugues",
    "ar": "Arabic",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "it": "Italiano",
    "nl": "Nederlands",
    "pl": "Polski",
    "vi": "Tieng Viet",
    "th": "Thai",
    "id": "Bahasa Indonesia",
    "hi": "Hindi",
}

_BASE_EN: dict[str, str] = {
    "app_title": "B2BTrend",
    "sidebar_title": "B2BTrend",
    "sidebar_caption": "Google Trends Marketing Intelligence",
    "language": "Language",
    "keyword": "Keyword or Topic ID",
    "search_placeholder": "Enter a keyword or Google Trends Topic ID",
    "data_section": "Data Fetch",
    "countries": "Countries",
    "fetch_data": "Fetch Data",
    "clear_cache": "Clear Cache",
    "cache_cleared": "{count} cache files removed",
    "data_updated": "Dataset updated successfully",
    "date_filter": "Date Filter",
    "all": "All",
    "last_1m": "Last 1 month",
    "last_3m": "Last 3 months",
    "last_6m": "Last 6 months",
    "custom_range": "Custom range",
    "start_date": "Start date",
    "end_date": "End date",
    "world_map": "World Map",
    "time_series": "Time Series",
    "city_drilldown": "City Analysis",
    "hourly": "Hourly",
    "ranking": "Ranking",
    "raw_data": "Raw Data",
    "overview": "Overview",
    "drill_down": "Choose a country to inspect city-level demand",
    "select_on_map": "Use the map or selector to inspect a country",
    "choose_country": "Choose country",
    "choose_city": "Choose city",
    "total_countries": "Countries",
    "total_cities": "Cities",
    "avg_score": "Average Score",
    "highest": "Highest",
    "no_data": "No data available yet. Fetch a dataset from the sidebar.",
    "loading": "Loading...",
    "trend": "Trend",
    "growth": "Growth",
    "forecast": "Forecast",
    "trend_strength_meter": "Trend Strength",
    "stl_decomposition": "STL Decomposition",
    "change_points": "Change Points",
    "rolling_volatility": "Rolling Volatility",
    "ensemble_forecast": "Ensemble Forecast",
    "correlation": "Correlation",
    "related_queries": "Related Queries",
    "top_queries": "Top Queries",
    "rising_queries": "Rising Queries",
    "model": "Model",
    "seasonal_period": "Seasonal Period",
    "stl_insufficient_data": "Not enough data for STL decomposition. At least 14 points are required.",
    "seasonal_period_info": "Detected seasonal period: {period} data points",
    "change_points_detected": "{count} change points detected using CUSUM",
    "volatility_window": "Volatility window",
    "forecast_periods": "Forecast horizon (weeks)",
    "insufficient_forecast_data": "Not enough data to generate a forecast.",
    "correlation_requires_two": "At least two countries are required for correlation analysis.",
    "city_table_title": "{country} city list ({count})",
    "raw_score": "Raw Score",
    "cleaned_score": "Cleaned Score",
    "outliers_removed": "Removed Outliers",
    "recommendation": "Recommendation",
    "action": "Action",
    "budget": "Suggested Budget",
    "confidence": "Confidence",
    "base_budget": "Base budget ($)",
    "detail": "Detail",
    "download_pdf": "Download PDF Report",
    "download_csv": "Download CSV",
    "last_7d_spikes": "No spikes detected in the last 7 days.",
    "spikes_found": "spikes detected",
    "send_alarm": "Send Alert",
    "telegram_sent": "Telegram alert sent",
    "email_sent": "Email alert sent",
    "choose_notification_channel": "Choose at least one notification channel from the sidebar.",
    "rising": "Rising",
    "falling": "Falling",
    "comparison": "Comparison",
    "search_filter": "Search / Filter",
    "city_scores_tab": "City Scores",
    "timeline_tab": "Timeline",
    "data_points_loaded": "{count} data points loaded",
    "fetch_prompt": "Fetch data to unlock this analysis.",
    "hourly_average": "Hourly Average",
    "day_hour_heatmap": "Day x Hour Heatmap",
    "spike_distribution": "Spike Distribution",
    "bollinger_bands": "Bollinger Bands",
    "volatility_percent": "Volatility (%)",
    "historical_data": "Historical Data",
    "country_correlation_title": "Country Correlation Matrix",
    "best_hours_prefix": "Best ad hours: {ranges}",
    "hourly_no_data": "Not enough hourly data available.",
    "weekday_mon": "Mon",
    "weekday_tue": "Tue",
    "weekday_wed": "Wed",
    "weekday_thu": "Thu",
    "weekday_fri": "Fri",
    "weekday_sat": "Sat",
    "weekday_sun": "Sun",
    "trend_up": "Rising",
    "trend_down": "Falling",
    "trend_flat": "Flat",
    "trend_no_data": "No Data",
    "strength_no_data": "Insufficient Data",
    "strength_very_strong": "Very Strong",
    "strength_strong": "Strong",
    "strength_medium": "Medium",
    "strength_weak": "Weak",
    "strength_very_weak": "Very Weak",
    "action_increase": "Increase",
    "action_start": "Start",
    "action_reduce": "Reduce",
    "action_keep": "Keep",
    "action_stop": "Stop",
    "reason_increase": "Strong positive trend and positive forecast. Increase budget aggressively.",
    "reason_start": "Demand is rising. Start or scale a performance campaign.",
    "reason_stop": "Sharp decline detected. Pause the campaign and reallocate budget.",
    "reason_reduce": "Trend is weakening. Reduce budget and move into experimentation or retargeting.",
    "reason_keep_volatile": "High volatility detected. Keep budget stable and optimize daily.",
    "reason_keep_stable": "Trend is stable. A low to medium always-on budget is appropriate.",
    "recommendation_up": "Demand is accelerating in this location. Increase budget and push performance campaigns.",
    "recommendation_down": "Interest is weakening. Shift to retargeting and creative testing.",
    "recommendation_volatile": "The trend is volatile. Use tighter monitoring and automated bidding.",
    "recommendation_stable": "The trend is stable. Maintain consistent visibility with an always-on campaign.",
    "report_header_title": "B2BTrend Report",
    "report_generated_at": "Generated",
    "report_general": "General Information",
    "report_keyword": "Keyword",
    "report_country": "Country",
    "report_city": "City",
    "report_date": "Report Date",
    "report_trend_analysis": "Trend Analysis",
    "report_trend_status": "Trend Status",
    "report_slope": "Slope",
    "report_momentum": "Momentum",
    "report_volatility": "Volatility",
    "report_strength": "Strength (0-5)",
    "report_r_squared": "R-squared",
    "report_scores": "Trend Scores",
    "report_avg_7d": "7-Day Average",
    "report_avg_30d": "30-Day Average",
    "report_growth_rate": "Growth Rate (%)",
    "report_ad_decision": "Ad Recommendation",
    "report_budget_multiplier": "Budget Multiplier",
    "report_suggested_budget": "Suggested Budget ($)",
    "report_confidence": "Confidence (%)",
    "report_forecast": "Forecast",
    "report_city_scores": "City Scores",
    "report_best_hours": "Best Ad Hours",
    "report_spikes": "Recent Spike Alerts",
    "report_table_date": "Date",
    "report_table_forecast": "Forecast",
    "report_table_lower": "Lower Bound",
    "report_table_upper": "Upper Bound",
    "report_table_score": "Score",
    "report_table_zscore": "Z-Score",
    "alert_subject": "Trend Alert - {count} spikes detected",
    "alert_header": "TREND ALERT",
    "alert_date": "Date",
    "alert_total_spikes": "Total spikes",
    "alert_increase": "INCREASE",
    "alert_decrease": "DECREASE",
    "alert_dashboard_hint": "Open the dashboard for full details.",
}

_BASE_TR: dict[str, str] = {
    "app_title": "B2BTrend",
    "sidebar_title": "B2BTrend",
    "sidebar_caption": "Google Trends Pazarlama Analizi",
    "language": "Dil",
    "keyword": "Anahtar Kelime veya Topic ID",
    "search_placeholder": "Bir anahtar kelime veya Google Trends Topic ID girin",
    "data_section": "Veri Cekimi",
    "countries": "Ulkeler",
    "fetch_data": "Veri Cek",
    "clear_cache": "Onbellegi Temizle",
    "cache_cleared": "{count} onbellek dosyasi silindi",
    "data_updated": "Veri kumesi basariyla guncellendi",
    "date_filter": "Tarih Filtresi",
    "all": "Tumu",
    "last_1m": "Son 1 ay",
    "last_3m": "Son 3 ay",
    "last_6m": "Son 6 ay",
    "custom_range": "Ozel aralik",
    "start_date": "Baslangic tarihi",
    "end_date": "Bitis tarihi",
    "world_map": "Dunya Haritasi",
    "time_series": "Zaman Serisi",
    "city_drilldown": "Sehir Analizi",
    "hourly": "Saatlik",
    "ranking": "Siralama",
    "raw_data": "Ham Veri",
    "overview": "Genel Bakis",
    "drill_down": "Sehir duzeyi talebi incelemek icin ulke secin",
    "select_on_map": "Bir ulkeyi haritadan veya seciciden inceleyin",
    "choose_country": "Ulke secin",
    "choose_city": "Sehir secin",
    "total_countries": "Ulke",
    "total_cities": "Sehir",
    "avg_score": "Ortalama Skor",
    "highest": "En Yuksek",
    "no_data": "Henuz veri yok. Sol panelden veri cekin.",
    "loading": "Yukleniyor...",
    "trend": "Trend",
    "growth": "Buyume",
    "forecast": "Tahmin",
    "trend_strength_meter": "Trend Gucu",
    "stl_decomposition": "STL Ayristirma",
    "change_points": "Degisim Noktalari",
    "rolling_volatility": "Kayan Volatilite",
    "ensemble_forecast": "Ensemble Tahmin",
    "correlation": "Korelasyon",
    "related_queries": "Ilgili Aramalar",
    "top_queries": "En Populer Sorgular",
    "rising_queries": "Yukselen Sorgular",
    "model": "Model",
    "seasonal_period": "Mevsimsellik Periyodu",
    "stl_insufficient_data": "STL ayristirma icin yeterli veri yok. En az 14 nokta gerekli.",
    "seasonal_period_info": "Tespit edilen mevsimsellik periyodu: {period} veri noktasi",
    "change_points_detected": "CUSUM ile {count} degisim noktasi tespit edildi",
    "volatility_window": "Volatilite penceresi",
    "forecast_periods": "Tahmin ufku (hafta)",
    "insufficient_forecast_data": "Tahmin uretmek icin yeterli veri yok.",
    "correlation_requires_two": "Korelasyon analizi icin en az iki ulke gerekli.",
    "city_table_title": "{country} sehir listesi ({count})",
    "raw_score": "Ham Skor",
    "cleaned_score": "Temizlenmis Skor",
    "outliers_removed": "Temizlenen Aykiri Deger",
    "recommendation": "Oneri",
    "action": "Aksiyon",
    "budget": "Onerilen Butce",
    "confidence": "Guven",
    "base_budget": "Baz butce ($)",
    "detail": "Detay",
    "download_pdf": "PDF Raporu Indir",
    "download_csv": "CSV Indir",
    "last_7d_spikes": "Son 7 gunde spike tespit edilmedi.",
    "spikes_found": "spike tespit edildi",
    "send_alarm": "Alarm Gonder",
    "telegram_sent": "Telegram bildirimi gonderildi",
    "email_sent": "E-posta bildirimi gonderildi",
    "choose_notification_channel": "Lutfen kenar cubugundan en az bir bildirim kanali secin.",
    "rising": "Yukselen",
    "falling": "Dusen",
    "comparison": "Karsilastirma",
    "search_filter": "Ara / Filtrele",
    "city_scores_tab": "Sehir Skorlari",
    "timeline_tab": "Zaman Serisi",
    "data_points_loaded": "{count} veri noktasi yuklendi",
    "fetch_prompt": "Bu analizi acmak icin veri cekin.",
    "hourly_average": "Saatlik Ortalama",
    "day_hour_heatmap": "Gun x Saat Isi Haritasi",
    "spike_distribution": "Spike Dagilimi",
    "bollinger_bands": "Bollinger Bantlari",
    "volatility_percent": "Volatilite (%)",
    "historical_data": "Gecmis Veri",
    "country_correlation_title": "Ulkeler Arasi Korelasyon Matrisi",
    "best_hours_prefix": "Reklam icin en uygun saatler: {ranges}",
    "hourly_no_data": "Yeterli saatlik veri yok.",
    "weekday_mon": "Pzt",
    "weekday_tue": "Sal",
    "weekday_wed": "Car",
    "weekday_thu": "Per",
    "weekday_fri": "Cum",
    "weekday_sat": "Cmt",
    "weekday_sun": "Paz",
    "trend_up": "Yukselis",
    "trend_down": "Dusus",
    "trend_flat": "Yatay",
    "trend_no_data": "Veri Yok",
    "strength_no_data": "Yetersiz Veri",
    "strength_very_strong": "Cok Guclu",
    "strength_strong": "Guclu",
    "strength_medium": "Orta",
    "strength_weak": "Zayif",
    "strength_very_weak": "Cok Zayif",
    "action_increase": "Artir",
    "action_start": "Baslat",
    "action_reduce": "Azalt",
    "action_keep": "Koru",
    "action_stop": "Durdur",
    "reason_increase": "Guclu yukselis ve pozitif tahmin var. Butceyi agresif bicimde artirin.",
    "reason_start": "Talep yukseliyor. Performans kampanyasi baslatin veya olcegi buyutun.",
    "reason_stop": "Keskin dusus tespit edildi. Kampanyayi durdurup butceyi yeniden dagitin.",
    "reason_reduce": "Trend zayifliyor. Butceyi azaltin ve test veya yeniden hedeflemeye gecin.",
    "reason_keep_volatile": "Yuksek volatilite var. Butceyi koruyup gunluk optimizasyon yapin.",
    "reason_keep_stable": "Trend stabil. Dusuk veya orta seviyeli always-on butce uygundur.",
    "recommendation_up": "Bu lokasyonda talep hizlaniyor. Butceyi artirip performans kampanyalarini guclendirin.",
    "recommendation_down": "Ilgi zayifliyor. Retargeting ve yaratici testlere yonelin.",
    "recommendation_volatile": "Trend dalgali. Daha siki takip ve otomatik teklif kullanin.",
    "recommendation_stable": "Trend stabil. Surekli gorunurluk icin always-on kampanya uygundur.",
    "report_header_title": "B2BTrend Raporu",
    "report_generated_at": "Olusturulma",
    "report_general": "Genel Bilgi",
    "report_keyword": "Anahtar Kelime",
    "report_country": "Ulke",
    "report_city": "Sehir",
    "report_date": "Rapor Tarihi",
    "report_trend_analysis": "Trend Analizi",
    "report_trend_status": "Trend Durumu",
    "report_slope": "Egilim",
    "report_momentum": "Momentum",
    "report_volatility": "Volatilite",
    "report_strength": "Guc (0-5)",
    "report_r_squared": "R-kare",
    "report_scores": "Trend Skorlari",
    "report_avg_7d": "7 Gunluk Ortalama",
    "report_avg_30d": "30 Gunluk Ortalama",
    "report_growth_rate": "Buyume Orani (%)",
    "report_ad_decision": "Reklam Onerisi",
    "report_budget_multiplier": "Butce Carpani",
    "report_suggested_budget": "Onerilen Butce ($)",
    "report_confidence": "Guven (%)",
    "report_forecast": "Tahmin",
    "report_city_scores": "Sehir Skorlari",
    "report_best_hours": "En Iyi Reklam Saatleri",
    "report_spikes": "Son Spike Alarmlari",
    "report_table_date": "Tarih",
    "report_table_forecast": "Tahmin",
    "report_table_lower": "Alt Sinir",
    "report_table_upper": "Ust Sinir",
    "report_table_score": "Skor",
    "report_table_zscore": "Z-Skor",
    "alert_subject": "Trend Alarmi - {count} spike tespit edildi",
    "alert_header": "TREND ALARMI",
    "alert_date": "Tarih",
    "alert_total_spikes": "Toplam spike",
    "alert_increase": "ARTIS",
    "alert_decrease": "DUSUS",
    "alert_dashboard_hint": "Tum detaylar icin paneli acin.",
}

UI_STRINGS: dict[str, dict[str, str]] = {"en": _BASE_EN, "tr": {**_BASE_EN, **_BASE_TR}}

for _language_code in SUPPORTED_LANGUAGES:
    UI_STRINGS.setdefault(_language_code, deepcopy(_BASE_EN))


def _load_locale_overrides() -> None:
    for locale_file in LOCALES_DIR.glob("*.json"):
        try:
            payload = json.loads(locale_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        if all(isinstance(v, str) for v in payload.values()):
            locale_code = locale_file.stem
            UI_STRINGS.setdefault(locale_code, deepcopy(_BASE_EN)).update(payload)
            continue

        for locale_code, entries in payload.items():
            if isinstance(entries, dict):
                UI_STRINGS.setdefault(locale_code, deepcopy(_BASE_EN)).update(
                    {str(k): str(v) for k, v in entries.items()}
                )


_load_locale_overrides()

for _language_code in SUPPORTED_LANGUAGES:
    merged = deepcopy(_BASE_EN)
    merged.update(UI_STRINGS.get(_language_code, {}))
    UI_STRINGS[_language_code] = merged


def t(lang: str, key: str, **kwargs) -> str:
    language = lang if lang in UI_STRINGS else "en"
    text = UI_STRINGS.get(language, UI_STRINGS["en"]).get(key, UI_STRINGS["en"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


def weekday_labels(lang: str) -> list[str]:
    return [
        t(lang, "weekday_mon"),
        t(lang, "weekday_tue"),
        t(lang, "weekday_wed"),
        t(lang, "weekday_thu"),
        t(lang, "weekday_fri"),
        t(lang, "weekday_sat"),
        t(lang, "weekday_sun"),
    ]


ALL_COUNTRIES: list[str] = [
    "BR", "CN", "IN", "RU", "ID", "JP", "TR", "TH", "VN", "AR", "ZA",
    "KR", "SA", "EG", "NG", "PK", "BD", "PH", "MY", "CO", "CL", "PE",
    "UA", "IL", "AE", "SG", "NZ", "KZ", "UZ", "TM", "KG", "TJ", "MA",
    "DZ", "TN", "LY", "SD", "AL", "BA", "RS", "ME", "MK", "XK", "IR",
    "IQ", "JO", "LB", "SY", "KW", "OM", "QA", "BH", "YE",
]

TOP_20_COUNTRIES: list[str] = ALL_COUNTRIES[:20]

PROXIES: list[str] = [
    proxy.strip()
    for proxy in os.getenv("GOOGLE_TRENDS_PROXIES", "").split(",")
    if proxy.strip()
] or []

FETCH_MIN_SLEEP = float(os.getenv("FETCH_MIN_SLEEP", "4.0"))
FETCH_MAX_SLEEP = float(os.getenv("FETCH_MAX_SLEEP", "9.0"))

TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")

SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str | None = os.getenv("SMTP_USER")
SMTP_PASSWORD: str | None = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL_TO: str | None = os.getenv("ALERT_EMAIL_TO")

SPIKE_Z_THRESHOLD = float(os.getenv("SPIKE_Z_THRESHOLD", "2.5"))

TIMEFRAME_OPTIONS: dict[str, str] = {
    "now 1-H": "Last 1 hour",
    "now 4-H": "Last 4 hours",
    "now 1-d": "Last 1 day",
    "now 7-d": "Last 7 days",
    "today 1-m": "Last 30 days",
    "today 3-m": "Last 90 days",
    "today 12-m": "Last 12 months",
    "today 5-y": "Last 5 years",
}


def _migrate_file(source: Path, target: Path) -> None:
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _migrate_legacy_storage() -> None:
    _migrate_file(LEGACY_CITIES_CSV, DEFAULT_CITIES_CSV)
    _migrate_file(LEGACY_TIMELINE_CSV, DEFAULT_TIMELINE_CSV)
    _migrate_file(LEGACY_GEOCACHE_FILE, GEOCACHE_FILE)


_migrate_legacy_storage()
