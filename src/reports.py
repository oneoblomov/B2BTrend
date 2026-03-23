"""Rapor dışa aktarma modülü — PDF ve CSV.

Genericleştirildi: herhangi bir anahtar kelime/konu için rapor üretir.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fpdf import FPDF

from src.config import REPORTS_DIR, t


# ══════════════════════════════════════════════════════════════
#  CSV EXPORT
# ══════════════════════════════════════════════════════════════

def export_csv(df: pd.DataFrame, filename: str | None = None) -> bytes:
    """DataFrame'i CSV byte'larına çevirir."""
    return df.to_csv(index=False).encode("utf-8")


def save_csv(df: pd.DataFrame, name: str) -> Path:
    """CSV'yi dosya olarak kaydeder."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = REPORTS_DIR / f"{name}_{ts}.csv"
    df.to_csv(fp, index=False)
    return fp


# ══════════════════════════════════════════════════════════════
#  PDF RAPOR
# ══════════════════════════════════════════════════════════════

_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_FONT_REGULAR = _FONT_DIR / "DejaVuSans.ttf"
_FONT_BOLD = _FONT_DIR / "DejaVuSans-Bold.ttf"


class _ReportPDF(FPDF):
    """Özelleştirilmiş PDF rapor sınıfı — Unicode desteği."""

    def __init__(self, keyword: str = "", lang: str = "en"):
        super().__init__()
        self.keyword_label = keyword or "Trend Analysis"
        self.lang = lang
        if _FONT_REGULAR.exists():
            self.add_font("DejaVu", "", str(_FONT_REGULAR))
        if _FONT_BOLD.exists():
            self.add_font("DejaVu", "B", str(_FONT_BOLD))
        self._fn = "DejaVu" if _FONT_REGULAR.exists() else "Helvetica"

    def header(self):
        self.set_font(self._fn, "B", 14)
        title = t(self.lang, "report_header_title")
        self.cell(0, 10, f"{self.keyword_label} - {title}",
                  border=False, new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font(self._fn, "", 9)
        generated = t(self.lang, "report_generated_at")
        self.cell(0, 5, f"{generated}: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                  new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font(self._fn, "", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title: str):
        self.set_font(self._fn, "B", 12)
        self.set_fill_color(41, 128, 185)
        self.set_text_color(255, 255, 255)
        self.cell(0, 8, f"  {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def key_value(self, key: str, value: str):
        self.set_font(self._fn, "B", 10)
        self.cell(55, 6, key + ":", new_x="RIGHT", new_y="TOP")
        self.set_font(self._fn, "", 10)
        self.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    def add_table(self, headers: list[str], rows: list[list[str]], col_widths: list[int] | None = None):
        if col_widths is None:
            w = int(190 / max(len(headers), 1))
            col_widths = [w] * len(headers)

        self.set_font(self._fn, "B", 9)
        self.set_fill_color(220, 220, 220)
        for i, h in enumerate(headers):
            cw = col_widths[i] if i < len(col_widths) else col_widths[-1]
            self.cell(cw, 7, h, border=1, fill=True, align="C")
        self.ln()

        self.set_font(self._fn, "", 8)
        for row in rows[:50]:
            for i, cell in enumerate(row):
                cw = col_widths[i] if i < len(col_widths) else col_widths[-1]
                self.cell(cw, 6, str(cell)[:30], border=1, align="C")
            self.ln()


def generate_pdf_report(
    country: str,
    city: str | None = None,
    signal: dict[str, Any] | None = None,
    scores: dict[str, Any] | None = None,
    ad_decision: dict[str, Any] | None = None,
    forecast_df: pd.DataFrame | None = None,
    city_scores: pd.DataFrame | None = None,
    peak_hours: list[int] | None = None,
    spikes: pd.DataFrame | None = None,
    keyword: str = "",
    lang: str = "en",
) -> bytes:
    """Detaylı PDF rapor oluşturur."""
    pdf = _ReportPDF(keyword=keyword, lang=lang)
    pdf.alias_nb_pages()
    pdf.add_page()

    # 1. Genel Bilgi
    pdf.section_title(t(lang, "report_general"))
    pdf.key_value(t(lang, "report_keyword"), keyword or "-")
    pdf.key_value(t(lang, "report_country"), country)
    if city:
        pdf.key_value(t(lang, "report_city"), city)
    pdf.key_value(t(lang, "report_date"), datetime.now().strftime("%Y-%m-%d %H:%M"))
    pdf.ln(3)

    # 2. Trend Sinyali
    if signal:
        pdf.section_title(t(lang, "report_trend_analysis"))
        pdf.key_value(t(lang, "report_trend_status"), t(lang, signal.get("label_key", "trend_no_data")))
        pdf.key_value(t(lang, "report_slope"), str(signal.get("slope", "-")))
        pdf.key_value(t(lang, "report_momentum"), str(signal.get("momentum", "-")))
        pdf.key_value(t(lang, "report_volatility"), str(signal.get("volatility", "-")))
        pdf.key_value(t(lang, "report_strength"), str(signal.get("strength", "-")))
        pdf.key_value(t(lang, "report_r_squared"), str(signal.get("r_squared", "-")))
        pdf.ln(3)

    # 3. Skor Ozeti
    if scores:
        pdf.section_title(t(lang, "report_scores"))
        pdf.key_value(t(lang, "report_avg_7d"), str(scores.get("avg_7d", "-")))
        pdf.key_value(t(lang, "report_avg_30d"), str(scores.get("avg_30d", "-")))
        pdf.key_value(t(lang, "report_growth_rate"), str(scores.get("growth_rate", "-")))
        pdf.key_value(t(lang, "report_momentum"), str(scores.get("momentum", "-")))
        pdf.ln(3)

    # 4. Reklam Karari
    if ad_decision:
        pdf.section_title(t(lang, "report_ad_decision"))
        pdf.key_value(t(lang, "action"), t(lang, ad_decision.get("action_key", "action_keep")))
        pdf.key_value(t(lang, "report_budget_multiplier"), str(ad_decision.get("budget_multiplier", "-")))
        pdf.key_value(t(lang, "report_suggested_budget"), str(ad_decision.get("suggested_budget", "-")))
        pdf.key_value(t(lang, "report_confidence"), str(int(ad_decision.get("confidence", 0) * 100)))
        pdf.set_font(pdf._fn, "", 9)
        pdf.multi_cell(0, 5, t(lang, ad_decision.get("reason_key", "reason_keep_stable")))
        pdf.ln(3)

    # 5. Tahmin
    if forecast_df is not None and not forecast_df.empty:
        pdf.section_title(t(lang, "report_forecast"))
        pdf.key_value(t(lang, "model"), str(forecast_df["model"].iloc[0]))
        pdf.key_value("MAE", str(forecast_df["mae"].iloc[0]))
        pdf.key_value(t(lang, "seasonal_period"), str(forecast_df["seasonal_period"].iloc[0]))
        pdf.ln(2)

        headers = [
            t(lang, "report_table_date"),
            t(lang, "report_table_forecast"),
            t(lang, "report_table_lower"),
            t(lang, "report_table_upper"),
        ]
        rows = []
        for _, r in forecast_df.iterrows():
            rows.append([
                str(r["ds"].strftime("%Y-%m-%d") if hasattr(r["ds"], "strftime") else r["ds"]),
                str(r["yhat"]), str(r["yhat_lower"]), str(r["yhat_upper"]),
            ])
        pdf.add_table(headers, rows, [50, 40, 50, 50])
        pdf.ln(3)

    # 6. Sehir Skorlari
    if city_scores is not None and not city_scores.empty:
        pdf.add_page()
        pdf.section_title(f"{t(lang, 'report_city_scores')} - {country}")
        headers = [t(lang, "report_city"), t(lang, "report_table_score")]
        rows = [[str(r["city"]), str(r["score"])] for _, r in city_scores.head(20).iterrows()]
        pdf.add_table(headers, rows, [120, 70])
        pdf.ln(3)

    # 7. Peak Saatler
    if peak_hours:
        pdf.section_title(t(lang, "report_best_hours"))
        hours_str = ", ".join(f"{h:02d}:00" for h in peak_hours)
        pdf.set_font(pdf._fn, "", 10)
        pdf.cell(0, 6, hours_str, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # 8. Spike Alarmlari
    if spikes is not None and not spikes.empty:
        pdf.section_title(t(lang, "report_spikes"))
        headers = [
            t(lang, "report_country"),
            t(lang, "report_city"),
            t(lang, "report_table_date"),
            t(lang, "report_table_score"),
            t(lang, "report_table_zscore"),
        ]
        rows = []
        for _, r in spikes.head(20).iterrows():
            rows.append([
                str(r["country"]), str(r["city"]),
                str(r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else r["date"]),
                str(r["score"]), str(r["z_score"]),
            ])
        pdf.add_table(headers, rows, [35, 45, 40, 35, 35])

    return bytes(pdf.output())


def save_pdf_report(**kwargs) -> Path:
    """PDF raporu dosya olarak kaydeder."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    country = kwargs.get("country", "report")
    fp = REPORTS_DIR / f"report_{country}_{ts}.pdf"
    content = generate_pdf_report(**kwargs)
    fp.write_bytes(content)
    return fp
