"""B2BTrend — Profesyonel Dashboard.

Herhangi bir anahtar kelime veya Google Trends Topic ID ile çalışır.
Çok dilli, modern, minimalist, dinamik görselleştirme.
İleri seviye zaman serisi analizi: STL, Change Point, Ensemble Forecast.
Google Trends tarzi interaktif harita drill-down.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import streamlit as st
from geopy.geocoders import Nominatim

from src.config import (
    ALL_COUNTRIES,
    DEFAULT_LANGUAGE,
    DEFAULT_KEYWORD,
    GEOCACHE_FILE,
    SUPPORTED_LANGUAGES,
    TIMEFRAME_OPTIONS,
    TOP_20_COUNTRIES,
    t,
)
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
from src.reports import export_csv, generate_pdf_report
from src.trend_fetcher import (
    FetchConfig,
    clear_cache,
    fetch_hourly_data,
    fetch_related_queries,
    fetch_related_topics,
    fetch_trends_dataset_country_keywords,
    fetch_trends_dataset,
    _build_client,
)
from src.workspace_store import (
    create_workspace,
    delete_workspace,
    get_default_workspace_id,
    list_workspaces,
    load_workspace_dataset,
    load_workspace_meta,
    save_workspace_dataset,
    set_default_workspace,
    update_workspace,
    workspace_summary,
)


# ══════════════════════════════════════════════════════════════
#  SAYFA KONFİGÜRASYONU
# ══════════════════════════════════════════════════════════════

st.set_page_config(
    page_title=t(DEFAULT_LANGUAGE, "app_title"),
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ══════════════════════════════════════════════════════════════
#  MODERN MİNİMALİST CSS
# ══════════════════════════════════════════════════════════════

st.markdown("""
<style>
    /* ── Global ── */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    /* ── Sidebar ── */
    [data-testid="stSidebar"] {
        min-width: 300px;
        background: linear-gradient(180deg, #0f0f23 0%, #1a1a3e 100%);
        transition: min-width 0.3s ease, width 0.3s ease;
    }
    /* When the sidebar is collapsed we no longer keep a fixed min-width, avoiding an empty blank strip */
    [data-testid="stSidebar"][aria-expanded="false"] {
        min-width: 0px !important;
        width: 0px !important;
    }
    [data-testid="stSidebar"] * {
        color: #e0e0e0 !important;
    }
    [data-testid="stSidebar"] .stSelectbox label,
    [data-testid="stSidebar"] .stMultiSelect label,
    [data-testid="stSidebar"] .stSlider label {
        color: #a0a0c0 !important;
        font-size: 0.85rem;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    /* ── Metrics ── */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #667eea11 0%, #764ba211 100%);
        border-radius: 12px;
        padding: 16px;
        border: 1px solid #667eea22;
        backdrop-filter: blur(10px);
    }
    div[data-testid="stMetric"] label {
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        opacity: 0.7;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: 1.8rem !important;
        font-weight: 700 !important;
    }

    /* ── Tabs ── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: transparent;
        border-bottom: 2px solid #667eea22;
        padding-bottom: 0;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 10px 20px;
        font-weight: 500;
        font-size: 0.85rem;
        letter-spacing: 0.3px;
    }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white !important;
    }

    /* ── Cards / Containers ── */
    [data-testid="stExpander"] {
        border: 1px solid #667eea22;
        border-radius: 12px;
        overflow: hidden;
    }

    /* ── Search Bar ── */
    .search-container {
        background: linear-gradient(135deg, #667eea15 0%, #764ba215 100%);
        border: 2px solid #667eea33;
        border-radius: 16px;
        padding: 24px;
        margin-bottom: 24px;
        backdrop-filter: blur(10px);
    }
    .search-title {
        font-size: 1.6rem;
        font-weight: 700;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 12px;
    }

    /* ── Strength Gauge ── */
    .strength-bar {
        height: 8px;
        border-radius: 4px;
        background: #e0e0e0;
        overflow: hidden;
    }
    .strength-fill {
        height: 100%;
        border-radius: 4px;
        transition: width 0.5s ease;
    }

    /* ── Data table ── */
    .stDataFrame {
        border-radius: 12px;
        overflow: hidden;
    }

    /* ── Divider ── */
    hr {
        border: none;
        border-top: 1px solid #667eea15;
        margin: 16px 0;
    }

    /* ── Hide Streamlit branding ── */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* ── Info/Warning ── */
    .stAlert {
        border-radius: 12px;
    }

    /* ── Button ── */
    .stButton > button {
        border-radius: 12px !important;
        border: 1px solid rgba(80, 110, 160, 0.22) !important;
        background: linear-gradient(150deg, rgba(255,255,255,0.86), rgba(245,248,255,0.95)) !important;
        color: #16324f !important;
        font-weight: 600;
        letter-spacing: 0.3px;
        transition: all 0.2s ease;
        text-align: left !important;
    }
    .stButton > button:hover {
        border-color: rgba(102, 126, 234, 0.5) !important;
        box-shadow: 0 4px 12px rgba(102, 126, 234, 0.15) !important;
        transform: translateY(-2px);
        background: linear-gradient(150deg, rgba(255,255,255,0.95), rgba(245,248,255,1)) !important;
    }

    /* Workspace cards */
    .ws-grid-note {
        opacity: 0.7;
        font-size: 0.9rem;
        margin: 0 0 16px 0;
    }
    .ws-card {
        border: 1px solid rgba(80, 110, 160, 0.22);
        background: linear-gradient(150deg, rgba(255,255,255,0.86), rgba(245,248,255,0.95));
        border-radius: 16px;
        padding: 14px 16px;
        margin-bottom: 10px;
    }
    .ws-card-title {
        font-size: 1rem;
        font-weight: 700;
        color: #16324f;
    }
    .ws-chip {
        display: inline-block;
        background: #edf3ff;
        color: #1d4e89;
        border: 1px solid rgba(29,78,137,0.18);
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 0.75rem;
        margin-top: 6px;
    }
    .ws-stats {
        margin-top: 10px;
        font-size: 0.86rem;
        color: #355070;
    }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ══════════════════════════════════════════════════════════════

def _country_iso3(iso2: str) -> str:
    c = pycountry.countries.get(alpha_2=iso2)
    return c.alpha_3 if c else iso2


def _country_name(iso2: str) -> str:
    c = pycountry.countries.get(alpha_2=iso2)
    return c.name if c else iso2

def _workspace_label(meta: dict) -> str:
    name = str(meta.get("name") or meta.get("id") or "workspace")
    keyword = str(meta.get("keyword") or DEFAULT_KEYWORD)
    return f"{name}  ·  {keyword}"


# ── Geocoding ─────────────────────────────────────────────────

def _load_geocache() -> pd.DataFrame:
    if GEOCACHE_FILE.exists():
        cache = pd.read_csv(GEOCACHE_FILE)
        for col in ["country", "city", "lat", "lon"]:
            if col not in cache.columns:
                cache[col] = pd.NA
        cache["lat"] = pd.to_numeric(cache["lat"], errors="coerce")
        cache["lon"] = pd.to_numeric(cache["lon"], errors="coerce")
        return cache[["country", "city", "lat", "lon"]]
    return pd.DataFrame({"country": pd.Series(dtype="string"), "city": pd.Series(dtype="string"),
                          "lat": pd.Series(dtype="float64"), "lon": pd.Series(dtype="float64")})


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

    geolocator = Nominatim(user_agent="trend-marketing-assistant", timeout=8)
    new_rows = []
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


# ── Plotly template ──
_PLOTLY_TEMPLATE = dict(
    layout=dict(
        font=dict(family="Inter, sans-serif"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=50, b=20),
        colorway=["#667eea", "#764ba2", "#f093fb", "#f5576c",
                   "#4facfe", "#00f2fe", "#43e97b", "#fa709a",
                   "#fee140", "#30cfd0"],
        xaxis=dict(gridcolor="#e0e0e022", zerolinecolor="#e0e0e022"),
        yaxis=dict(gridcolor="#e0e0e022", zerolinecolor="#e0e0e022"),
        hoverlabel=dict(font_size=12, font_family="Inter"),
    )
)


def _apply_plotly_style(fig: go.Figure, height: int = 450) -> go.Figure:
    """Plotly grafiğine modern minimalist stil uygular."""
    fig.update_layout(
        height=height,
        font=dict(family="Inter, sans-serif"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=50, b=20),
        xaxis=dict(gridcolor="rgba(128,128,128,0.1)", zerolinecolor="rgba(128,128,128,0.1)"),
        yaxis=dict(gridcolor="rgba(128,128,128,0.1)", zerolinecolor="rgba(128,128,128,0.1)"),
        hoverlabel=dict(font_size=12, font_family="Inter"),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02,
            xanchor="right", x=1, font=dict(size=11),
        ),
    )
    return fig


# ══════════════════════════════════════════════════════════════
#  SESSION STATE DEFAULTS
# ══════════════════════════════════════════════════════════════

if "lang" not in st.session_state:
    st.session_state["lang"] = "tr"
if "selected_country" not in st.session_state:
    st.session_state["selected_country"] = None
if "workspace_id" not in st.session_state:
    st.session_state["workspace_id"] = None
if "show_workspace_page" not in st.session_state:
    st.session_state["show_workspace_page"] = False

LANG = st.session_state["lang"]


def tr(key: str, **kwargs) -> str:
    return t(LANG, key, **kwargs)


def signal_label(signal: dict) -> str:
    return tr(signal.get("label_key", "trend_no_data"))


def strength_label(data: dict) -> str:
    return tr(data.get("label_key", "strength_no_data"))


def decision_action_label(decision: dict) -> str:
    return tr(decision.get("action_key", "action_keep"))


def decision_reason(decision: dict) -> str:
    return tr(decision.get("reason_key", "reason_keep_stable"))


def _country_keywords_to_text(mapping: dict[str, str]) -> str:
    lines: list[str] = []
    for country in sorted(mapping.keys()):
        value = str(mapping[country]).strip()
        if value:
            lines.append(f"{country}:{value}")
    return "\n".join(lines)


def _parse_country_keywords_text(raw_text: str, allowed_countries: list[str]) -> dict[str, str]:
    allowed = {c.upper() for c in allowed_countries}
    out: dict[str, str] = {}
    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        country, value = line.split(":", 1)
        cc = country.strip().upper()
        term = value.strip()
        if cc and term and cc in allowed:
            out[cc] = term
    return out


def _render_workspace_home() -> None:
    st.markdown(f"## 📂 {t(LANG, 'app_title')} Workspaces")
    st.markdown('<p class="ws-grid-note">Karttan secim yapabilir, + ile workspace olusturabilir, ⋯ menusuyle duzenleme/silme/varsayilan islemlerini yonetebilirsin.</p>', unsafe_allow_html=True)

    workspaces = list_workspaces()
    default_workspace_id = get_default_workspace_id()

    header_col_1, header_col_2 = st.columns([1.5, 1])
    with header_col_1:
        create_open = st.button("➕ Yeni Workspace", type="primary", key="ws_create_open")
    with header_col_2:
        lang_options = list(SUPPORTED_LANGUAGES.keys())
        current_lang = st.session_state.get("lang", "tr")
        lang_idx = lang_options.index(current_lang) if current_lang in lang_options else 0
        selected_lang = st.selectbox(
            "Dil",
            options=lang_options,
            index=lang_idx,
            format_func=lambda x: SUPPORTED_LANGUAGES[x],
            key="workspace_page_lang",
        )
        if selected_lang != st.session_state.get("lang"):
            st.session_state["lang"] = selected_lang
            st.rerun()

    if create_open:
        st.session_state["show_create_workspace"] = True

    if st.session_state.get("show_create_workspace", False):
        with st.expander("Yeni Workspace", expanded=True):
            new_name = st.text_input("Workspace adi", value="", key="create_ws_name")
            new_keyword = st.text_input("Arama metni veya Topic ID", value=DEFAULT_KEYWORD, key="create_ws_keyword")
            new_countries = st.multiselect(
                "Ulkeler",
                ALL_COUNTRIES,
                default=list(TOP_20_COUNTRIES),
                key="create_ws_countries",
            )
            use_topic_mode = st.toggle(
                "Google Topic ID modu (/m/...)",
                value=False,
                key="create_ws_topic_mode",
                help="Aciksa tek bir Topic ID tum ulkelerde kullanilir. Kapaliysa ulke bazli kelimeler kullanilabilir.",
            )
            country_keywords_text = st.text_area(
                "Ulke bazli anahtar kelimeler (CC:kelime)",
                value="",
                key="create_ws_country_keywords",
                disabled=use_topic_mode,
                height=120,
                placeholder="TR:tavuk\nUS:chicken\nDE:hahnchen",
            )

            c1, c2 = st.columns(2)
            with c1:
                if st.button("Workspace Olustur", type="primary", use_container_width=True, key="create_ws_submit"):
                    created = create_workspace(
                        name=new_name or "Yeni Workspace",
                        keyword=new_keyword,
                        countries=new_countries,
                    )
                    keywords_map = _parse_country_keywords_text(country_keywords_text, new_countries or list(TOP_20_COUNTRIES))
                    update_workspace(
                        created["id"],
                        name=new_name or "Yeni Workspace",
                        language=st.session_state.get("lang", "tr"),
                        keyword=new_keyword,
                        countries=new_countries,
                        use_topic_mode=use_topic_mode,
                        country_keywords=keywords_map,
                    )
                    st.session_state["workspace_id"] = created["id"]
                    st.session_state["show_create_workspace"] = False
                    st.success("Workspace olusturuldu.")
                    st.rerun()
            with c2:
                if st.button("Vazgec", use_container_width=True, key="create_ws_cancel"):
                    st.session_state["show_create_workspace"] = False
                    st.rerun()

    if not workspaces:
        st.info("Henuz workspace yok. + butonundan olustur.")
        return

    for idx, ws in enumerate(workspaces):
        ws_id = ws["id"]
        stats = workspace_summary(ws_id)
        default_badge = "(VARSAYILAN)" if ws_id == default_workspace_id else ""

        col_card, col_menu = st.columns([0.92, 0.08], gap="small")
        
        with col_card:
            if st.button(
                f"📂 **{ws.get('name', ws_id)}** {default_badge}\n"
                f"🔍 {ws.get('keyword', DEFAULT_KEYWORD)}\n\n"
                f"📊 {stats['countries_count']} ülke • {stats['cities_count']} şehir • ⭐ {stats['avg_score']:.1f}",
                key=f"open_ws_{idx}",
                use_container_width=True,
            ):
                st.session_state["workspace_id"] = ws_id
                st.rerun()
        
        with col_menu:
            with st.popover("⋯"):
                if st.button("Varsayilan Yap", key=f"default_ws_{idx}", use_container_width=True):
                    set_default_workspace(ws_id)
                    st.success("Varsayilan workspace guncellendi.")
                    st.rerun()

                with st.expander("Duzenle", expanded=False):
                    edit_name = st.text_input("Workspace adi", value=str(ws.get("name") or ws_id), key=f"edit_name_{idx}")
                    edit_lang = st.selectbox(
                        "Dil",
                        options=list(SUPPORTED_LANGUAGES.keys()),
                        index=list(SUPPORTED_LANGUAGES.keys()).index(str(ws.get("language") or "tr")) if str(ws.get("language") or "tr") in SUPPORTED_LANGUAGES else 0,
                        format_func=lambda x: SUPPORTED_LANGUAGES[x],
                        key=f"edit_lang_{idx}",
                    )
                    edit_keyword = st.text_input(
                        "Arama metni veya Topic ID",
                        value=str(ws.get("keyword") or DEFAULT_KEYWORD),
                        key=f"edit_keyword_{idx}",
                    )
                    edit_countries = st.multiselect(
                        "Ulkeler",
                        ALL_COUNTRIES,
                        default=list(ws.get("countries") or TOP_20_COUNTRIES),
                        key=f"edit_countries_{idx}",
                    )
                    edit_topic_mode = st.toggle(
                        "Google Topic ID modu (/m/...)",
                        value=bool(ws.get("use_topic_mode", False)),
                        key=f"edit_topic_mode_{idx}",
                    )
                    edit_country_keywords = st.text_area(
                        "Ulke bazli anahtar kelimeler (CC:kelime)",
                        value=_country_keywords_to_text(ws.get("country_keywords") or {}),
                        key=f"edit_country_keywords_{idx}",
                        disabled=edit_topic_mode,
                        height=120,
                    )

                    if st.button("Kaydet", type="primary", key=f"save_ws_{idx}", use_container_width=True):
                        keywords_map = _parse_country_keywords_text(edit_country_keywords, edit_countries or list(TOP_20_COUNTRIES))
                        update_workspace(
                            ws_id,
                            name=edit_name,
                            language=edit_lang,
                            keyword=edit_keyword,
                            countries=edit_countries,
                            use_topic_mode=edit_topic_mode,
                            country_keywords=keywords_map,
                        )
                        st.success("Workspace guncellendi.")
                        st.rerun()

                if st.button("Sil", key=f"delete_ws_{idx}", use_container_width=True):
                    delete_workspace(ws_id)
                    if st.session_state.get("workspace_id") == ws_id:
                        st.session_state["workspace_id"] = None
                    st.success("Workspace silindi.")
                    st.rerun()


# Ilk giriste zorunlu workspace secim/olusturma/duzenleme ekranı
all_workspaces = list_workspaces()
available_ids = [item["id"] for item in all_workspaces]
if st.session_state["workspace_id"] not in available_ids:
    st.session_state["workspace_id"] = None

if st.session_state.get("show_workspace_page", False):
    st.session_state["show_workspace_page"] = False
    _render_workspace_home()
    st.stop()

if st.session_state["workspace_id"] is None:
    default_workspace_id = get_default_workspace_id()
    if default_workspace_id and default_workspace_id in available_ids:
        st.session_state["workspace_id"] = default_workspace_id

if st.session_state["workspace_id"] is None:
    _render_workspace_home()
    st.stop()

active_workspace_id = st.session_state["workspace_id"]
active_workspace_meta = load_workspace_meta(active_workspace_id)

workspace_lang = str(active_workspace_meta.get("language") or "tr")
if workspace_lang not in SUPPORTED_LANGUAGES:
    workspace_lang = "tr"
if st.session_state.get("lang") != workspace_lang:
    st.session_state["lang"] = workspace_lang
LANG = workspace_lang

hl_map = {
    "tr": "tr-TR", "en": "en-US", "de": "de-DE", "fr": "fr-FR",
    "es": "es-ES", "pt": "pt-BR", "ar": "ar-SA", "zh": "zh-CN",
    "ja": "ja-JP", "ko": "ko-KR", "ru": "ru-RU", "it": "it-IT",
}


# ══════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown(f"### 📊 {tr('sidebar_title')}")
    st.caption(tr("sidebar_caption"))

    st.divider()

    workspace_items = list_workspaces()
    workspace_ids = [item["id"] for item in workspace_items]
    current_idx = workspace_ids.index(active_workspace_id) if active_workspace_id in workspace_ids else 0
    selected_workspace_id = st.selectbox(
        "🗂️ Workspace",
        options=workspace_ids,
        format_func=lambda ws_id: _workspace_label(next(item for item in workspace_items if item["id"] == ws_id)),
        index=current_idx,
        key="sidebar_workspace_select",
    )
    if selected_workspace_id != active_workspace_id:
        st.session_state["workspace_id"] = selected_workspace_id
        st.rerun()

    st.caption("Workspace yonetimi icin ilk ekrana donebilirsiniz.")
    if st.button("Workspace Sayfasina Don", use_container_width=True, key="goto_workspace_home"):
        st.session_state["show_workspace_page"] = True
        st.rerun()

    active_workspace_meta = load_workspace_meta(active_workspace_id)

    # ── Veri Çekimi ──
    st.markdown(f"**📥 {t(LANG, 'data_section')}**")
    st.caption(str(active_workspace_meta.get("name") or active_workspace_id))
    st.caption(f"🔍 {str(active_workspace_meta.get('keyword') or DEFAULT_KEYWORD)}")
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        fetch_btn = st.button(f"📥 {t(LANG, 'fetch_data')}", type="primary", use_container_width=True)
    with col_f2:
        if st.button(f"🗑️ {t(LANG, 'clear_cache')}", use_container_width=True):
            n = clear_cache()
            st.success(f"✅ {tr('cache_cleared', count=n)}")

    if fetch_btn:
        with st.spinner(t(LANG, "loading")):
            kw = str(active_workspace_meta.get("keyword") or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
            effective_countries = list(active_workspace_meta.get("countries") or TOP_20_COUNTRIES)
            use_topic_mode = bool(active_workspace_meta.get("use_topic_mode", False))
            country_keywords = active_workspace_meta.get("country_keywords") or {}
            cfg = FetchConfig(
                keyword=kw,
                top_cities_per_country=10,
                hl=hl_map.get(LANG, "en-US"),
            )

            if use_topic_mode:
                cities_new, timeline_new = fetch_trends_dataset(countries=effective_countries, config=cfg)
            else:
                cities_new, timeline_new = fetch_trends_dataset_country_keywords(
                    countries=effective_countries,
                    keyword_by_country=country_keywords,
                    default_keyword=kw,
                    config=cfg,
                )

            if cities_new.empty or timeline_new.empty:
                st.error(t(LANG, "no_data"))
            else:
                save_workspace_dataset(active_workspace_id, cities_new, timeline_new)
                st.success(f"✅ {tr('data_updated')}")
                st.rerun()

    st.divider()

    # ── Tarih Filtresi ──
    st.markdown(f"**📅 {t(LANG, 'date_filter')}**")
    date_options = [t(LANG, "all"), t(LANG, "last_1m"), t(LANG, "last_3m"),
                    t(LANG, "last_6m"), t(LANG, "custom_range")]
    date_range_option = st.selectbox(
        t(LANG, "date_filter"), date_options, index=0, key="date_range",
        label_visibility="collapsed",
    )
    custom_start, custom_end = None, None
    if date_range_option == t(LANG, "custom_range"):
        custom_start = st.date_input(t(LANG, "start_date"), key="date_start")
        custom_end = st.date_input(t(LANG, "end_date"), key="date_end")

    st.divider()
    st.caption("v3.0 — B2BTrend Suite")


# ══════════════════════════════════════════════════════════════
#  VERİ YÜKLEME
# ══════════════════════════════════════════════════════════════

cities, timeline = load_workspace_dataset(active_workspace_id)

if cities.empty or timeline.empty:
    # Göster: arama ekranı
    st.markdown(f"""
    <div class="search-container">
        <div class="search-title">📊 {t(LANG, 'app_title')}</div>
        <p style="opacity:0.7;">{t(LANG, 'no_data')}</p>
    </div>
    """, unsafe_allow_html=True)
    st.info(f"👈 {t(LANG, 'no_data')}")
    st.stop()

# ── Tarih filtresi ──
if date_range_option == t(LANG, "last_1m"):
    cutoff = timeline["date"].max() - pd.Timedelta(days=30)
    timeline = timeline[timeline["date"] >= cutoff]
elif date_range_option == t(LANG, "last_3m"):
    cutoff = timeline["date"].max() - pd.Timedelta(days=90)
    timeline = timeline[timeline["date"] >= cutoff]
elif date_range_option == t(LANG, "last_6m"):
    cutoff = timeline["date"].max() - pd.Timedelta(days=180)
    timeline = timeline[timeline["date"] >= cutoff]
elif date_range_option == t(LANG, "custom_range") and custom_start and custom_end:
    timeline = timeline[
        (timeline["date"] >= pd.Timestamp(custom_start))
        & (timeline["date"] <= pd.Timestamp(custom_end))
    ]

kw_display = str(active_workspace_meta.get("keyword") or DEFAULT_KEYWORD)


# ══════════════════════════════════════════════════════════════
#  HEADER — Arama Bölümü
# ══════════════════════════════════════════════════════════════

st.markdown(f"""
<div class="search-container">
    <div class="search-title">📊 {t(LANG, 'app_title')}</div>
    <p style="opacity:0.6; margin:0; font-size:0.9rem;">
        🔍 <strong>{kw_display}</strong> &nbsp;·&nbsp;
        📆 {timeline['date'].min().strftime('%Y-%m-%d')} → {timeline['date'].max().strftime('%Y-%m-%d')} &nbsp;·&nbsp;
        🌍 {cities['country'].nunique()} {t(LANG, 'total_countries')} &nbsp;·&nbsp;
        🏙️ {cities['city'].nunique()} {t(LANG, 'total_cities')}
    </p>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  ÜST METRİKLER
# ══════════════════════════════════════════════════════════════

country_summary = timeline[timeline["city"].fillna("") == ""].groupby("country", as_index=False)
country_summary = country_summary.agg(avg_score=("score", "mean"), latest_date=("date", "max")).sort_values("avg_score", ascending=False)
if country_summary.empty:
    # Yedek: şehir bazlı toplanmış veri varsa, yine de devam edelim.
    country_summary = (
        timeline.groupby("country", as_index=False)
        .agg(avg_score=("score", "mean"), latest_date=("date", "max"))
        .sort_values("avg_score", ascending=False)
    )
country_summary["iso3"] = country_summary["country"].apply(_country_iso3)
country_summary["country_name"] = country_summary["country"].apply(_country_name)

m1, m2, m3, m4 = st.columns(4)
m1.metric(f"🌍 {t(LANG, 'total_countries')}", country_summary.shape[0])
m2.metric(f"🏙️ {t(LANG, 'total_cities')}", cities.shape[0])
m3.metric(f"📊 {t(LANG, 'avg_score')}", f"{country_summary['avg_score'].mean():.1f}")
best_row = country_summary.iloc[0] if not country_summary.empty else None
if best_row is not None:
    m4.metric(f"📈 {t(LANG, 'highest')}", f"{best_row['avg_score']:.1f} ({best_row['country']})")

st.divider()


# ══════════════════════════════════════════════════════════════
#  TAB YAPISI
# ══════════════════════════════════════════════════════════════

tab_map, tab_ts, tab_city, tab_hourly, tab_rank, tab_data = st.tabs([
    f"🌍 {t(LANG, 'world_map')}",
    f"📈 {t(LANG, 'time_series')}",
    f"🏙️ {t(LANG, 'city_drilldown')}",
    f"⏰ {t(LANG, 'hourly')}",
    f"🏆 {t(LANG, 'ranking')}",
    f"📋 {t(LANG, 'raw_data')}",
])

country_list = country_summary["country"].tolist()


# ══════════════════════════════════════════════════════════════
#  TAB 1: İNTERAKTİF DÜNYA HARİTASI + DRILL-DOWN
# ══════════════════════════════════════════════════════════════

with tab_map:
    st.subheader(f"🌍 {t(LANG, 'world_map')}")
    st.caption(t(LANG, "select_on_map"))

    # Tek bir harita: choropleth + scatter combo
    # Ülke seçme dropdown — haritadaki ülkeye tıklamayı simüle eder
    map_mode = st.radio(
        "Mod", [t(LANG, "overview"), t(LANG, "city_drilldown")],
        horizontal=True, key="map_mode", label_visibility="collapsed",
    )

    if map_mode == t(LANG, "overview"):
        # Dünya choropleth haritası
        fig_world = px.choropleth(
            country_summary,
            locations="iso3",
            color="avg_score",
            hover_name="country_name",
            hover_data={"avg_score": ":.1f", "iso3": False},
            labels={"avg_score": t(LANG, "avg_score")},
            projection="natural earth",
            color_continuous_scale="YlOrRd",
        )
        fig_world.update_layout(
            height=550,
            margin=dict(l=0, r=0, t=10, b=0),
            geo=dict(
                showframe=False,
                showcoastlines=True,
                coastlinecolor="rgba(128,128,128,0.3)",
                showland=True,
                landcolor="rgba(240,240,240,0.3)",
                showocean=True,
                oceancolor="rgba(200,220,255,0.1)",
                showlakes=False,
                projection_type="natural earth",
            ),
            coloraxis_colorbar=dict(
                title=t(LANG, "avg_score"),
                thickness=15,
                len=0.5,
            ),
        )
        st.plotly_chart(fig_world, use_container_width=True)

        # Ülke seçimi — dropdown ile (haritadan seçim simülasyonu)
        st.markdown(f"**{t(LANG, 'drill_down')}:**")
        selected_for_drill = st.selectbox(
            t(LANG, "choose_country"),
            ["---"] + [f"{c} — {_country_name(c)}" for c in country_list],
            index=0, key="drill_country",
            label_visibility="collapsed",
        )

        if selected_for_drill != "---":
            drill_country = selected_for_drill.split(" — ")[0]
            st.session_state["selected_country"] = drill_country

            # Şehir scatter haritası
            city_map_data = cities[cities["country"] == drill_country].sort_values("score", ascending=False)
            if city_map_data.empty:
                st.warning(t(LANG, "no_data"))
            else:
                city_points = _geocode_cities(city_map_data)
                city_points = city_points.merge(
                    city_map_data[["country", "city", "score"]],
                    on=["country", "city"], how="left",
                ).dropna(subset=["lat", "lon"])

                if not city_points.empty:
                    fig_drill = px.scatter_map(
                        city_points,
                        lat="lat", lon="lon",
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
                    fig_drill.update_layout(
                        height=500,
                        margin=dict(l=0, r=0, t=10, b=0),
                        coloraxis_colorbar=dict(title="Skor", thickness=12),
                    )
                    st.plotly_chart(fig_drill, use_container_width=True)
                    # Bir satır boşluk
                    st.markdown("<br><br>", unsafe_allow_html=True)
                    # Şehir tablosu — kompakt
                    with st.expander(f"📋 {_country_name(drill_country)} — {t(LANG, 'total_cities')}: {len(city_map_data)}"):
                        st.dataframe(
                            city_map_data[["city", "score", "geo_code"]].reset_index(drop=True),
                            use_container_width=True, hide_index=True,
                        )
    else:
        # Doğrudan şehir haritası (tüm dünya)
        city_map_all = cities.sort_values("score", ascending=False).head(200).copy()
        if not city_map_all.empty:
            city_pts = _geocode_cities(city_map_all)
            city_pts = city_pts.merge(
                city_map_all[["country", "city", "score"]],
                on=["country", "city"], how="left",
            ).dropna(subset=["lat", "lon"])

            if not city_pts.empty:
                fig_all_cities = px.scatter_map(
                    city_pts,
                    lat="lat", lon="lon",
                    color="score",
                    size="score",
                    size_max=20,
                    hover_name="city",
                    hover_data={"country": True, "score": True, "lat": False, "lon": False},
                    zoom=1.5,
                    map_style="carto-positron",
                    color_continuous_scale="Plasma",
                )
                fig_all_cities.update_layout(
                    height=600,
                    margin=dict(l=0, r=0, t=10, b=0),
                )
                st.plotly_chart(fig_all_cities, use_container_width=True)


# ══════════════════════════════════════════════════════════════
#  TAB 2: İLERİ ZAMAN SERİSİ ANALİZİ
# ══════════════════════════════════════════════════════════════

with tab_ts:
    st.subheader(f"📈 {t(LANG, 'time_series')}")

    sel_country = st.selectbox(t(LANG, "choose_country"), country_list, index=0, key="ts_country")
    country_tl = timeline[(timeline["country"] == sel_country) & (timeline["city"].fillna("") == "")].copy()
    if country_tl.empty:
        # Ülke seviyesinde veri yoksa şehir bazlı ortalamayı kullan
        country_tl = timeline[timeline["country"] == sel_country].copy()

    if country_tl.empty:
        st.warning(t(LANG, "no_data"))
    else:
        country_agg = (
            country_tl.groupby("date", as_index=False)
            .agg(score=("score", "mean"))
            .sort_values("date")
            .reset_index(drop=True)
        )

        # ── Üst metrikler ──
        mas = compute_moving_averages(country_agg["score"])
        scores = compute_trend_scores(country_agg["score"])
        signal = robust_trend_signal(country_agg["score"])
        strength = trend_strength_meter(country_agg["score"])

        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
        mc1.metric(t(LANG, "trend"), signal_label(signal))
        mc2.metric("7d Avg", f"{scores['avg_7d']:.1f}")
        mc3.metric("30d Avg", f"{scores['avg_30d']:.1f}")
        mc4.metric(t(LANG, "growth"), f"{scores['growth_rate']:+.1f}%")
        mc5.metric(t(LANG, "trend_strength_meter"), f"{strength['score']:.0f}/100 - {strength_label(strength)}")

        # ── Alt tab'lar ──
        ts_sub1, ts_sub2, ts_sub3, ts_sub4, ts_sub5 = st.tabs([
            f"📈 {t(LANG, 'overview')}",
            f"🔬 {t(LANG, 'stl_decomposition')}",
            f"📍 {t(LANG, 'change_points')}",
            f"📊 {t(LANG, 'rolling_volatility')}",
            f"🔮 {t(LANG, 'ensemble_forecast')}",
        ])

        # ── Alt Tab 1: Overview ──
        with ts_sub1:
            fig_ts = go.Figure()
            fig_ts.add_trace(go.Scatter(
                x=country_agg["date"], y=country_agg["score"],
                mode="lines", name="Ham Skor", opacity=0.4,
                line=dict(color="#bdc3c7", width=1),
                fill="tozeroy", fillcolor="rgba(102,126,234,0.05)",
            ))
            fig_ts.add_trace(go.Scatter(
                x=country_agg["date"], y=mas["ma7"],
                mode="lines", name="7d MA",
                line=dict(color="#667eea", width=2.5),
            ))
            fig_ts.add_trace(go.Scatter(
                x=country_agg["date"], y=mas["ma30"],
                mode="lines", name="30d MA",
                line=dict(color="#f5576c", width=2, dash="dash"),
            ))

            # Spike noktaları
            if len(country_agg) > 5:
                spike_df = detect_spikes(country_agg["score"], z_threshold=2.0)
                peak_mask = spike_df["is_spike"]
                if peak_mask.any():
                    peak_dates = country_agg.loc[spike_df[peak_mask]["index"], "date"]
                    peak_scores = country_agg.loc[spike_df[peak_mask]["index"], "score"]
                    fig_ts.add_trace(go.Scatter(
                        x=peak_dates, y=peak_scores,
                        mode="markers", name="Anomali",
                        marker=dict(color="#f5576c", size=10, symbol="diamond",
                                    line=dict(width=2, color="white")),
                    ))

            fig_ts = _apply_plotly_style(fig_ts, 420)
            fig_ts.update_layout(
                title=f"{_country_name(sel_country)} — Trend",
                xaxis_title="", yaxis_title="Skor (0-100)",
                hovermode="x unified",
            )
            st.plotly_chart(fig_ts, use_container_width=True)

        # ── Alt Tab 2: STL Decomposition ──
        with ts_sub2:
            decomp = stl_decompose(country_agg["score"])
            if decomp is not None:
                fig_stl = make_subplots(
                    rows=4, cols=1, shared_xaxes=True,
                    subplot_titles=["Observed", "Trend", "Seasonal", "Residual"],
                    vertical_spacing=0.06,
                )

                dates = country_agg["date"]
                fig_stl.add_trace(go.Scatter(
                    x=dates, y=decomp["observed"],
                    mode="lines", name="Observed",
                    line=dict(color="#667eea", width=1.5),
                ), row=1, col=1)
                fig_stl.add_trace(go.Scatter(
                    x=dates, y=decomp["trend"],
                    mode="lines", name="Trend",
                    line=dict(color="#f5576c", width=2),
                ), row=2, col=1)
                fig_stl.add_trace(go.Scatter(
                    x=dates, y=decomp["seasonal"],
                    mode="lines", name="Seasonal",
                    line=dict(color="#43e97b", width=1.5),
                    fill="tozeroy", fillcolor="rgba(67,233,123,0.1)",
                ), row=3, col=1)
                fig_stl.add_trace(go.Scatter(
                    x=dates, y=decomp["residual"],
                    mode="markers+lines", name="Residual",
                    line=dict(color="#fa709a", width=1),
                    marker=dict(size=3),
                ), row=4, col=1)

                fig_stl = _apply_plotly_style(fig_stl, 650)
                fig_stl.update_layout(
                    title=f"STL Decomposition (period={decomp['period']})",
                    showlegend=False,
                )
                st.plotly_chart(fig_stl, use_container_width=True)

                st.info(f"🔬 {tr('seasonal_period_info', period=decomp['period'])}")
            else:
                st.warning(tr("stl_insufficient_data"))

        # ── Alt Tab 3: Change Point Detection ──
        with ts_sub3:
            change_pts = detect_change_points(country_agg["score"], threshold=2.0)
            extrema = detect_local_extrema(country_agg["score"], order=3)

            fig_cp = go.Figure()
            fig_cp.add_trace(go.Scatter(
                x=country_agg["date"], y=country_agg["score"],
                mode="lines", name="Score",
                line=dict(color="#667eea", width=2),
            ))

            # Change points
            if change_pts:
                cp_dates = [country_agg["date"].iloc[i] for i in change_pts if i < len(country_agg)]
                cp_scores = [country_agg["score"].iloc[i] for i in change_pts if i < len(country_agg)]
                fig_cp.add_trace(go.Scatter(
                    x=cp_dates, y=cp_scores,
                    mode="markers", name="Change Point",
                    marker=dict(color="#f5576c", size=14, symbol="x",
                                line=dict(width=2, color="white")),
                ))
                for d, s in zip(cp_dates, cp_scores):
                    fig_cp.add_vline(x=d, line_dash="dot", line_color="rgba(245,87,108,0.3)")

            # Local maxima/minima
            for idx in extrema.get("maxima", []):
                if idx < len(country_agg):
                    fig_cp.add_trace(go.Scatter(
                        x=[country_agg["date"].iloc[idx]],
                        y=[country_agg["score"].iloc[idx]],
                        mode="markers", name="Peak",
                        marker=dict(color="#43e97b", size=8, symbol="triangle-up"),
                        showlegend=idx == (extrema["maxima"][0] if extrema["maxima"] else -1),
                    ))
            for idx in extrema.get("minima", []):
                if idx < len(country_agg):
                    fig_cp.add_trace(go.Scatter(
                        x=[country_agg["date"].iloc[idx]],
                        y=[country_agg["score"].iloc[idx]],
                        mode="markers", name="Valley",
                        marker=dict(color="#fa709a", size=8, symbol="triangle-down"),
                        showlegend=idx == (extrema["minima"][0] if extrema["minima"] else -1),
                    ))

            fig_cp = _apply_plotly_style(fig_cp, 420)
            fig_cp.update_layout(
                title=f"{t(LANG, 'change_points')} — {_country_name(sel_country)}",
                hovermode="x unified",
            )
            st.plotly_chart(fig_cp, use_container_width=True)
            st.info(f"📍 {tr('change_points_detected', count=len(change_pts))}")

        # ── Alt Tab 4: Rolling Volatility + Bollinger ──
        with ts_sub4:
            vol_window = st.slider(tr("volatility_window"), 3, 30, 7, key="vol_window")
            vol_df = rolling_volatility(country_agg["score"], window=vol_window)

            fig_vol = make_subplots(
                rows=2, cols=1, shared_xaxes=True,
                subplot_titles=[tr("bollinger_bands"), tr("volatility_percent")],
                row_heights=[0.65, 0.35], vertical_spacing=0.08,
            )

            # Bollinger bands
            fig_vol.add_trace(go.Scatter(
                x=country_agg["date"], y=vol_df["upper_band"],
                mode="lines", line=dict(width=0), showlegend=False,
            ), row=1, col=1)
            fig_vol.add_trace(go.Scatter(
                x=country_agg["date"], y=vol_df["lower_band"],
                mode="lines", line=dict(width=0),
                fill="tonexty", fillcolor="rgba(102,126,234,0.1)",
                name="Bollinger Band",
            ), row=1, col=1)
            fig_vol.add_trace(go.Scatter(
                x=country_agg["date"], y=vol_df["ma"],
                mode="lines", name="MA",
                line=dict(color="#667eea", width=2),
            ), row=1, col=1)
            fig_vol.add_trace(go.Scatter(
                x=country_agg["date"], y=vol_df["value"],
                mode="lines", name="Score",
                line=dict(color="#764ba2", width=1), opacity=0.6,
            ), row=1, col=1)

            # Volatilite chart
            fig_vol.add_trace(go.Bar(
                x=country_agg["date"], y=vol_df["volatility_pct"],
                name="Volatilite %",
                marker=dict(
                    color=vol_df["volatility_pct"],
                    colorscale="YlOrRd",
                    showscale=False,
                ),
            ), row=2, col=1)

            fig_vol = _apply_plotly_style(fig_vol, 550)
            fig_vol.update_layout(title=f"{tr('bollinger_bands')} & {tr('rolling_volatility')}")
            st.plotly_chart(fig_vol, use_container_width=True)

        # ── Alt Tab 5: Ensemble Forecast ──
        with ts_sub5:
            forecast_periods = st.slider(tr("forecast_periods"), 4, 24, 12, key="fc_periods")
            forecast_df = advanced_forecast(country_agg["score"], periods=forecast_periods)

            if not forecast_df.empty:
                fc1, fc2, fc3 = st.columns(3)
                fc1.metric(tr("model"), str(forecast_df["model"].iloc[0]))
                fc2.metric("MAE", f"{forecast_df['mae'].iloc[0]:.2f}")
                sp = int(forecast_df["seasonal_period"].iloc[0])
                fc3.metric(tr("seasonal_period"), f"{sp}" if sp > 0 else "-")

                fig_fc = go.Figure()
                fig_fc.add_trace(go.Scatter(
                    x=country_agg["date"], y=country_agg["score"],
                    mode="lines", name=tr("historical_data"),
                    line=dict(color="#667eea", width=2),
                ))
                fig_fc.add_trace(go.Scatter(
                    x=forecast_df["ds"], y=forecast_df["yhat_upper"],
                    mode="lines", line=dict(width=0), showlegend=False,
                ))
                fig_fc.add_trace(go.Scatter(
                    x=forecast_df["ds"], y=forecast_df["yhat_lower"],
                    mode="lines", line=dict(width=0),
                    fill="tonexty", fillcolor="rgba(102,126,234,0.15)",
                    name="95% CI",
                ))
                fig_fc.add_trace(go.Scatter(
                    x=forecast_df["ds"], y=forecast_df["yhat"],
                    mode="lines", name=t(LANG, "forecast"),
                    line=dict(color="#f5576c", width=2.5, dash="dash"),
                ))

                fig_fc = _apply_plotly_style(fig_fc, 420)
                fig_fc.update_layout(
                    title=f"{_country_name(sel_country)} — {t(LANG, 'ensemble_forecast')}",
                    hovermode="x unified",
                )
                st.plotly_chart(fig_fc, use_container_width=True)

                with st.expander(f"📊 {tr('forecast')}"):
                    st.dataframe(forecast_df, use_container_width=True, hide_index=True)
            else:
                st.info(tr("insufficient_forecast_data"))

        # ── Korelasyon Analizi ──
        st.divider()
        with st.expander(f"🔗 {t(LANG, 'correlation')}"):
            country_corr_timeline = timeline[timeline["city"].fillna("") == ""]
            if country_corr_timeline.empty:
                country_corr_timeline = timeline
            corr_matrix = compute_correlation_matrix(country_corr_timeline, group_col="country")
            if not corr_matrix.empty and corr_matrix.shape[0] > 1:
                fig_corr = px.imshow(
                    corr_matrix,
                    color_continuous_scale="RdBu_r",
                    zmin=-1, zmax=1,
                    labels=dict(color="Korelasyon"),
                )
                fig_corr = _apply_plotly_style(fig_corr, 500)
                fig_corr.update_layout(title=tr("country_correlation_title"))
                st.plotly_chart(fig_corr, use_container_width=True)
            else:
                st.info(tr("correlation_requires_two"))

        # ── Related Queries ──
        st.divider()
        with st.expander(f"🔍 {t(LANG, 'related_queries')}"):
            if st.button(f"🔍 {sel_country} — {t(LANG, 'related_queries')}", key="rq_btn"):
                with st.spinner(t(LANG, "loading")):
                    cfg = FetchConfig(keyword=kw_display, hl=hl_map.get(LANG, "en-US"))
                    client = _build_client(cfg)
                    rq = fetch_related_queries(client, sel_country, cfg)
                    col_rq1, col_rq2 = st.columns(2)
                    with col_rq1:
                        if not rq["top"].empty:
                            st.markdown(f"**{tr('top_queries')}**")
                            st.dataframe(rq["top"], use_container_width=True, hide_index=True)
                    with col_rq2:
                        if not rq["rising"].empty:
                            st.markdown(f"**{tr('rising_queries')}**")
                            st.dataframe(rq["rising"], use_container_width=True, hide_index=True)
                    if rq["top"].empty and rq["rising"].empty:
                        st.info(t(LANG, "no_data"))


# ══════════════════════════════════════════════════════════════
#  TAB 3: ŞEHİR DRILLDOWN
# ══════════════════════════════════════════════════════════════

with tab_city:
    st.subheader(f"🏙️ {t(LANG, 'city_drilldown')}")

    city_country = st.selectbox(t(LANG, "choose_country"), country_list, index=0, key="city_country")
    country_cities_df = cities[cities["country"] == city_country].sort_values("score", ascending=False)
    country_tl_city = timeline[timeline["country"] == city_country].copy()

    if country_cities_df.empty:
        st.warning(t(LANG, "no_data"))
    else:
        ranked = location_trend_ranking(country_cities_df)
        st.dataframe(
            ranked[["rank", "city", "score", "geo_code"]].head(20),
            use_container_width=True, hide_index=True,
        )

        city_names = sorted([c for c in country_tl_city["city"].dropna().unique().tolist() if str(c).strip()])
        if not city_names:
            st.warning(t(LANG, "no_data"))
        else:
            sel_city = st.selectbox(t(LANG, "choose_city"), city_names, index=0, key="city_select")
            city_ts = (
                country_tl_city[country_tl_city["city"] == sel_city][["date", "score"]]
                .sort_values("date").reset_index(drop=True)
            )

            if city_ts.empty:
                st.warning(t(LANG, "no_data"))
            else:
                city_ts["score_clean"], outlier_count = clean_city_outliers(city_ts["score"])
                city_signal = robust_trend_signal(city_ts["score_clean"])
                city_scores = compute_trend_scores(city_ts["score_clean"])
                city_mas = compute_moving_averages(city_ts["score_clean"])
                city_strength = trend_strength_meter(city_ts["score_clean"])

                cc1, cc2, cc3, cc4, cc5 = st.columns(5)
                cc1.metric(t(LANG, "trend"), signal_label(city_signal))
                cc2.metric("Slope", f"{city_signal['slope']:.3f}")
                cc3.metric(tr("rolling_volatility"), f"{city_signal['volatility']:.1f}")
                cc4.metric(t(LANG, "growth"), f"{city_scores['growth_rate']:+.1f}%")
                cc5.metric(t(LANG, "trend_strength_meter"), f"{city_strength['score']:.0f}/100 - {strength_label(city_strength)}")

                fig_city = go.Figure()
                fig_city.add_trace(go.Scatter(
                    x=city_ts["date"], y=city_ts["score"],
                    mode="lines", name="Ham", opacity=0.3,
                    line=dict(color="#bdc3c7", width=1),
                ))
                fig_city.add_trace(go.Scatter(
                    x=city_ts["date"], y=city_ts["score_clean"],
                    mode="lines", name="Clean",
                    line=dict(color="#667eea", width=2),
                    fill="tozeroy", fillcolor="rgba(102,126,234,0.05)",
                ))
                fig_city.add_trace(go.Scatter(
                    x=city_ts["date"], y=city_mas["ma7"],
                    mode="lines", name="7d MA",
                    line=dict(color="#43e97b", width=1.5, dash="dot"),
                ))

                # Forecast
                city_forecast = advanced_forecast(city_ts["score_clean"], periods=12)
                if not city_forecast.empty:
                    fig_city.add_trace(go.Scatter(
                        x=city_forecast["ds"], y=city_forecast["yhat_upper"],
                        mode="lines", line=dict(width=0), showlegend=False,
                    ))
                    fig_city.add_trace(go.Scatter(
                        x=city_forecast["ds"], y=city_forecast["yhat_lower"],
                        mode="lines", line=dict(width=0),
                        fill="tonexty", fillcolor="rgba(102,126,234,0.1)",
                        name="95% CI",
                    ))
                    fig_city.add_trace(go.Scatter(
                        x=city_forecast["ds"], y=city_forecast["yhat"],
                        mode="lines", name=t(LANG, "forecast"),
                        line=dict(color="#f5576c", width=2, dash="dash"),
                    ))

                fig_city = _apply_plotly_style(fig_city, 420)
                fig_city.update_layout(
                    title=f"{sel_city}, {_country_name(city_country)}",
                    hovermode="x unified",
                )
                st.plotly_chart(fig_city, use_container_width=True)
                st.info(f"💡 {recommendation_from_signal(city_signal, lang=LANG)}")


# ══════════════════════════════════════════════════════════════
#  TAB 4: SAATLİK ANALİZ
# ══════════════════════════════════════════════════════════════

with tab_hourly:
    st.subheader(f"⏰ {t(LANG, 'hourly')}")

    hourly_geo = st.selectbox(t(LANG, "choose_country"), country_list, index=0, key="hourly_geo")

    if st.button(f"⏰ {t(LANG, 'fetch_data')}", key="hourly_fetch"):
        with st.spinner(t(LANG, "loading")):
            cfg = FetchConfig(keyword=kw_display, hl=hl_map.get(LANG, "en-US"))
            hourly_df = fetch_hourly_data(hourly_geo, config=cfg)
            if hourly_df.empty:
                st.warning(t(LANG, "no_data"))
            else:
                st.session_state["hourly_data"] = hourly_df
                st.session_state["hourly_geo_label"] = hourly_geo
                st.success(f"✅ {tr('data_points_loaded', count=len(hourly_df))}")

    if "hourly_data" in st.session_state:
        hdf = st.session_state["hourly_data"]
        h_analysis = hourly_analysis(hdf, lang=LANG)

        col_h1, col_h2 = st.columns([1, 1])

        with col_h1:
            if not h_analysis["avg_by_hour"].empty:
                fig_hourly = px.bar(
                    x=h_analysis["avg_by_hour"].index,
                    y=h_analysis["avg_by_hour"].values,
                    labels={"x": "Hour", "y": "Avg Score"},
                    color=h_analysis["avg_by_hour"].values,
                    color_continuous_scale="Viridis",
                )
                fig_hourly = _apply_plotly_style(fig_hourly, 380)
                fig_hourly.update_layout(
                    title=tr("hourly_average"),
                    showlegend=False,
                    coloraxis_showscale=False,
                )
                st.plotly_chart(fig_hourly, use_container_width=True)

        with col_h2:
            if not h_analysis["heatmap_matrix"].empty:
                fig_hm = px.imshow(
                    h_analysis["heatmap_matrix"],
                    labels=dict(x="Hour", y="Day", color="Score"),
                    color_continuous_scale="Viridis",
                    aspect="auto",
                )
                fig_hm = _apply_plotly_style(fig_hm, 380)
                fig_hm.update_layout(title=tr("day_hour_heatmap"))
                st.plotly_chart(fig_hm, use_container_width=True)

            st.success(f"🎯 {best_ad_hours_text(h_analysis['peak_hours'], lang=LANG)}")
    else:
        st.info(f"👆 {t(LANG, 'fetch_data')}")


# ══════════════════════════════════════════════════════════════
#  TAB 7: RANKING & TOP 10
# ══════════════════════════════════════════════════════════════

with tab_rank:
    st.subheader(f"🏆 {t(LANG, 'ranking')}")

    country_ranking_timeline = timeline[timeline["city"].fillna("") == ""]
    if country_ranking_timeline.empty:
        country_ranking_timeline = timeline
    rising, falling = country_ranking(country_ranking_timeline, top_n=10)

    col_r, col_f = st.columns(2)

    with col_r:
        st.markdown(f"### 📈 {t(LANG, 'rising')}")
        if not rising.empty:
            fig_rising = px.bar(
                rising.head(10),
                x="change_pct", y="country",
                orientation="h",
                color="change_pct",
                color_continuous_scale="Greens",
                labels={"change_pct": "Change %", "country": ""},
            )
            fig_rising = _apply_plotly_style(fig_rising, 380)
            fig_rising.update_layout(yaxis=dict(autorange="reversed"), showlegend=False,
                                     coloraxis_showscale=False)
            st.plotly_chart(fig_rising, use_container_width=True)
        else:
            st.info(t(LANG, "no_data"))

    with col_f:
        st.markdown(f"### 📉 {t(LANG, 'falling')}")
        if not falling.empty:
            fig_falling = px.bar(
                falling.head(10),
                x="change_pct", y="country",
                orientation="h",
                color="change_pct",
                color_continuous_scale="Reds_r",
                labels={"change_pct": "Change %", "country": ""},
            )
            fig_falling = _apply_plotly_style(fig_falling, 380)
            fig_falling.update_layout(yaxis=dict(autorange="reversed"), showlegend=False,
                                      coloraxis_showscale=False)
            st.plotly_chart(fig_falling, use_container_width=True)
        else:
            st.info(t(LANG, "no_data"))

    st.divider()

    st.subheader(f"🔀 {t(LANG, 'comparison')}")
    compare_countries = st.multiselect(
        t(LANG, "comparison"),
        country_list,
        default=country_list[:3] if len(country_list) >= 3 else country_list,
        key="compare_countries",
        label_visibility="collapsed",
    )

    if compare_countries:
        compare_data = timeline[timeline["country"].isin(compare_countries)].copy()
        compare_agg = compare_data.groupby(["country", "date"], as_index=False)["score"].mean()

        fig_compare = px.line(
            compare_agg,
            x="date", y="score",
            color="country",
            labels={"score": "Score", "date": "", "country": ""},
        )
        fig_compare = _apply_plotly_style(fig_compare, 400)
        fig_compare.update_layout(title=t(LANG, "comparison"), hovermode="x unified")
        st.plotly_chart(fig_compare, use_container_width=True)


# ══════════════════════════════════════════════════════════════
#  TAB 8: HAM VERİ & EXPORT
# ══════════════════════════════════════════════════════════════

with tab_data:
    st.subheader(f"📋 {t(LANG, 'raw_data')}")

    data_tab1, data_tab2 = st.tabs(["City Scores", "Timeline"])

    with data_tab1:
        search_country = st.text_input(f"🔍 {t(LANG, 'search_filter')}", "", key="search_country")
        display_cities = cities.copy()
        if search_country:
            display_cities = display_cities[
                display_cities["country"].str.contains(search_country, case=False, na=False)
                | display_cities["city"].str.contains(search_country, case=False, na=False)
            ]

        st.dataframe(display_cities, use_container_width=True, hide_index=True)
        st.download_button(
            f"⬇️ {t(LANG, 'download_csv')}",
            data=export_csv(display_cities),
            file_name=f"city_scores_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )

    with data_tab2:
        search_tl = st.text_input(f"🔍 {t(LANG, 'search_filter')}", "", key="search_tl")
        display_tl = timeline.copy()
        if search_tl:
            mask = (
                display_tl["country"].str.contains(search_tl, case=False, na=False)
                | display_tl["city"].str.contains(search_tl, case=False, na=False)
            )
            display_tl = display_tl[mask]

        st.dataframe(
            display_tl.sort_values(["country", "city", "date"]),
            use_container_width=True, hide_index=True,
        )
        st.download_button(
            f"⬇️ {t(LANG, 'download_csv')}",
            data=export_csv(display_tl),
            file_name=f"timeline_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )


# ══════════════════════════════════════════════════════════════
#  FOOTER
# ══════════════════════════════════════════════════════════════

st.divider()
st.markdown(f"""
<div style="text-align:center; opacity:0.5; padding:12px 0;">
    📊 B2BTrend v3.0 &nbsp;·&nbsp;
    🔍 <strong>{kw_display}</strong> &nbsp;·&nbsp;
    Professional Marketing Intelligence Suite
</div>
""", unsafe_allow_html=True)
