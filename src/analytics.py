"""Gelişmiş analitik motoru — İleri seviye zaman serisi, anomali, tahmin, öneri.

Modüller:
- Trend Skoru Hesaplama (7g / 30g ortalama, momentum, artış oranı)
- Anomali / Spike Tespiti (Z-score, IQR, Grubbs)
- Outlier Temizleme
- Mevsimsellik Algılama (ACF tabanlı)
- STL Dekompozisyon (trend / seasonal / residual)
- Change Point Detection (CUSUM + Bayesian)
- Gelişmiş Ensemble Tahmin (Holt-Winters, SARIMA, ETS, Linear, Ensemble)
- Rolling Volatilite & Bollinger Bantları
- Korelasyon Matrisi
- Reklam Karar Algoritması & Öneri Motoru
- Saatlik Analiz (peak saatleri)
"""
from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from scipy.signal import argrelextrema
from statsmodels.tsa.holtwinters import ExponentialSmoothing, SimpleExpSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.seasonal import STL
from statsmodels.tools.sm_exceptions import ConvergenceWarning

from src.config import SPIKE_Z_THRESHOLD, t, weekday_labels


# ══════════════════════════════════════════════════════════════
#  1) TREND SKORU HESAPLAMA
# ══════════════════════════════════════════════════════════════

def compute_moving_averages(series: pd.Series) -> dict[str, pd.Series]:
    """7, 14, 30 günlük hareketli ortalamalar."""
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    return {
        "ma7": s.rolling(window=7, min_periods=1).mean(),
        "ma14": s.rolling(window=14, min_periods=1).mean(),
        "ma30": s.rolling(window=30, min_periods=1).mean(),
    }


def compute_trend_scores(series: pd.Series) -> dict[str, float]:
    """7g ve 30g ortalama + artış oranı."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 2:
        return {"avg_7d": 0.0, "avg_30d": 0.0, "growth_rate": 0.0, "momentum": 0.0, "acceleration": 0.0}

    avg_7d = float(s.tail(7).mean()) if len(s) >= 7 else float(s.mean())
    avg_30d = float(s.tail(30).mean()) if len(s) >= 30 else float(s.mean())

    first_half = s.iloc[: len(s) // 2]
    second_half = s.iloc[len(s) // 2:]
    if first_half.mean() > 0:
        growth_rate = float((second_half.mean() - first_half.mean()) / first_half.mean() * 100)
    else:
        growth_rate = 0.0

    momentum = float(s.tail(7).mean() - s.tail(14).mean()) if len(s) >= 14 else 0.0

    # İvme: son haftanın momentum değişimi
    if len(s) >= 21:
        prev_momentum = float(s.iloc[-14:-7].mean() - s.iloc[-21:-14].mean())
        acceleration = momentum - prev_momentum
    else:
        acceleration = 0.0

    return {
        "avg_7d": round(avg_7d, 2),
        "avg_30d": round(avg_30d, 2),
        "growth_rate": round(growth_rate, 2),
        "momentum": round(momentum, 2),
        "acceleration": round(acceleration, 2),
    }


def robust_trend_signal(series: pd.Series, window: int = 12) -> dict[str, Any]:
    """Trend yönü tespiti — eğim, momentum, volatilite, güç."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {
            "label": t("en", "trend_no_data"),
            "label_key": "trend_no_data",
            "direction": "no_data",
            "slope": 0.0,
            "momentum": 0.0,
            "volatility": 0.0,
            "strength": 0,
            "r_squared": 0.0,
            "direction_confidence": 0.0,
        }

    s = s.tail(max(window, 6))
    x = np.arange(len(s), dtype=float)
    y = s.to_numpy(dtype=float)

    coeffs = np.polyfit(x, y, 1) if len(s) >= 2 else [0.0, 0.0]
    slope = float(coeffs[0])
    momentum = float(y[-1] - y.mean())
    volatility = float(np.std(y))

    # R² hesapla
    y_pred = np.polyval(coeffs, x)
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r_squared = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    r_squared = max(0.0, r_squared)

    # Trend gücü: 0 → 5
    abs_slope = abs(slope)
    if abs_slope > 1.5:
        strength = 5
    elif abs_slope > 1.0:
        strength = 4
    elif abs_slope > 0.5:
        strength = 3
    elif abs_slope > 0.2:
        strength = 2
    elif abs_slope > 0.05:
        strength = 1
    else:
        strength = 0

    # Yön güveni (0-1)
    direction_confidence = min(1.0, abs_slope * r_squared * 2)

    if slope > 0.35 and momentum > 2:
        label_key = "trend_up"
        direction = "up"
    elif slope < -0.35 and momentum < -2:
        label_key = "trend_down"
        direction = "down"
    else:
        label_key = "trend_flat"
        direction = "flat"

    return {
        "label": t("en", label_key),
        "label_key": label_key,
        "direction": direction,
        "slope": round(slope, 4),
        "momentum": round(momentum, 2),
        "volatility": round(volatility, 2),
        "strength": strength,
        "r_squared": round(r_squared, 4),
        "direction_confidence": round(direction_confidence, 3),
    }


def location_trend_ranking(cities_df: pd.DataFrame) -> pd.DataFrame:
    """Lokasyon bazlı trend ranking."""
    if cities_df.empty:
        return cities_df
    ranked = cities_df.sort_values("score", ascending=False).reset_index(drop=True)
    ranked["rank"] = range(1, len(ranked) + 1)
    return ranked


# ══════════════════════════════════════════════════════════════
#  2) ANOMALİ / SPİKE TESPİTİ
# ══════════════════════════════════════════════════════════════

def detect_spikes(
    series: pd.Series,
    z_threshold: float = SPIKE_Z_THRESHOLD,
) -> pd.DataFrame:
    """Z-score tabanlı spike tespiti."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 5:
        return pd.DataFrame(columns=["index", "value", "z_score", "is_spike"])

    y = s.to_numpy(dtype=float)
    mean = np.mean(y)
    std = np.std(y)

    if std == 0:
        z_scores = np.zeros_like(y)
    else:
        z_scores = (y - mean) / std

    return pd.DataFrame({
        "index": s.index,
        "value": y,
        "z_score": np.round(z_scores, 3),
        "is_spike": np.abs(z_scores) > z_threshold,
    })


def detect_anomalies_iqr(series: pd.Series, multiplier: float = 1.5) -> pd.Series:
    """IQR tabanlı anomali tespiti."""
    s = pd.to_numeric(series, errors="coerce")
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr
    return (s < lower) | (s > upper)


# ══════════════════════════════════════════════════════════════
#  3) OUTLIER TEMİZLEME
# ══════════════════════════════════════════════════════════════

def clean_city_outliers(series: pd.Series, z_thresh: float = 3.5) -> tuple[pd.Series, int]:
    """Robust Z-score + IQR ile outlier temizleme."""
    s = pd.to_numeric(series, errors="coerce").copy()
    valid = s.dropna()
    if len(valid) < 6:
        return s, 0

    median = float(valid.median())
    mad = float(np.median(np.abs(valid - median)))
    if mad == 0:
        return s, 0

    robust_z = 0.6745 * (valid - median) / mad
    robust_idx = set(robust_z[np.abs(robust_z) > z_thresh].index)

    q1, q3 = float(valid.quantile(0.25)), float(valid.quantile(0.75))
    iqr = q3 - q1
    iqr_idx: set = set()
    if iqr > 0:
        iqr_idx = set(valid[(valid < q1 - 1.5 * iqr) | (valid > q3 + 1.5 * iqr)].index)

    outlier_idx = list(robust_idx.union(iqr_idx))
    cleaned = s.copy()
    cleaned.loc[outlier_idx] = np.nan
    cleaned = cleaned.interpolate(method="linear", limit_direction="both").ffill().bfill()

    return cleaned, int(len(outlier_idx))


# ══════════════════════════════════════════════════════════════
#  4) MEVSİMSELLİK ALGILAMA
# ══════════════════════════════════════════════════════════════

def detect_seasonal_period(
    series: pd.Series,
    candidates: tuple[int, ...] = (4, 6, 8, 12, 26, 52),
) -> int | None:
    """Otokorelasyona dayalı mevsimsellik periyodu tespiti."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 16:
        return None

    y = s.to_numpy(dtype=float)
    best_p: int | None = None
    best_corr = -np.inf

    for p in candidates:
        if p <= 1 or len(y) < 2 * p:
            continue
        a, b = y[:-p], y[p:]
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        corr = float(np.corrcoef(a, b)[0, 1])
        if np.isfinite(corr) and corr > best_corr:
            best_corr = corr
            best_p = p

    if best_p is None or best_corr < 0.2:
        return None
    return best_p


# ══════════════════════════════════════════════════════════════
#  5) STL DEKOMPOZİSYON
# ══════════════════════════════════════════════════════════════

def stl_decompose(
    series: pd.Series,
    period: int | None = None,
) -> dict[str, pd.Series] | None:
    """STL zaman serisi ayrıştırması: trend + seasonal + residual."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 14:
        return None

    if period is None:
        period = detect_seasonal_period(s) or 7

    if period < 2:
        period = 7
    if len(s) < 2 * period:
        period = max(2, len(s) // 3)

    try:
        stl_result = STL(s.values, period=period, robust=True).fit()
        return {
            "trend": pd.Series(stl_result.trend, index=s.index),
            "seasonal": pd.Series(stl_result.seasonal, index=s.index),
            "residual": pd.Series(stl_result.resid, index=s.index),
            "observed": s,
            "period": period,
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  6) CHANGE POINT DETECTION
# ══════════════════════════════════════════════════════════════

def detect_change_points(
    series: pd.Series,
    threshold: float = 2.0,
) -> list[int]:
    """CUSUM tabanlı değişim noktası tespiti."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 10:
        return []

    y = s.to_numpy(dtype=float)
    mean = np.mean(y)
    std = np.std(y)
    if std == 0:
        return []

    # CUSUM
    cusum_pos = np.zeros(len(y))
    cusum_neg = np.zeros(len(y))

    for i in range(1, len(y)):
        cusum_pos[i] = max(0, cusum_pos[i - 1] + (y[i] - mean) / std - 0.5)
        cusum_neg[i] = max(0, cusum_neg[i - 1] - (y[i] - mean) / std - 0.5)

    cusum_combined = cusum_pos + cusum_neg
    change_points = []

    for i in range(1, len(cusum_combined) - 1):
        if cusum_combined[i] > threshold:
            if cusum_combined[i] > cusum_combined[i - 1] and cusum_combined[i] >= cusum_combined[i + 1]:
                change_points.append(int(s.index[i]))

    # Çok yakın noktaları filtrele (min 3 index fark)
    filtered = []
    for cp in change_points:
        if not filtered or cp - filtered[-1] >= 3:
            filtered.append(cp)

    return filtered


def detect_local_extrema(
    series: pd.Series,
    order: int = 3,
) -> dict[str, list[int]]:
    """Yerel maksimum ve minimum noktaları tespit eder."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < order * 2 + 1:
        return {"maxima": [], "minima": []}

    y = s.to_numpy(dtype=float)

    maxima_idx = argrelextrema(y, np.greater_equal, order=order)[0]
    minima_idx = argrelextrema(y, np.less_equal, order=order)[0]

    return {
        "maxima": [int(s.index[i]) for i in maxima_idx],
        "minima": [int(s.index[i]) for i in minima_idx],
    }


# ══════════════════════════════════════════════════════════════
#  7) ROLLING VOLATİLİTE & BOLLİNGER BANTLARI
# ══════════════════════════════════════════════════════════════

def rolling_volatility(
    series: pd.Series,
    window: int = 7,
) -> pd.DataFrame:
    """Kayan pencere volatilite ve Bollinger bantları."""
    s = pd.to_numeric(series, errors="coerce").fillna(0)

    ma = s.rolling(window=window, min_periods=1).mean()
    std = s.rolling(window=window, min_periods=1).std().fillna(0)

    return pd.DataFrame({
        "value": s,
        "ma": ma,
        "std": std,
        "upper_band": ma + 2 * std,
        "lower_band": ma - 2 * std,
        "volatility_pct": (std / ma.replace(0, 1) * 100).round(2),
    })


# ══════════════════════════════════════════════════════════════
#  8) KORELASYON ANALİZİ
# ══════════════════════════════════════════════════════════════

def compute_correlation_matrix(
    timeline: pd.DataFrame,
    group_col: str = "country",
) -> pd.DataFrame:
    """Ülkeler/şehirler arası korelasyon matrisi hesaplar."""
    if timeline.empty:
        return pd.DataFrame()

    pivot = timeline.pivot_table(
        values="score", index="date", columns=group_col, aggfunc="mean"
    )
    if pivot.shape[1] < 2:
        return pd.DataFrame()

    return pivot.corr().round(3)


# ══════════════════════════════════════════════════════════════
#  9) GELİŞMİŞ ENSEMBLE TAHMİN
# ══════════════════════════════════════════════════════════════

def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if len(y_true) == 0 or len(y_true) != len(y_pred):
        return float("inf")
    return float(np.mean(np.abs(y_true - y_pred)))


def _fit_holt_winters(data: np.ndarray, seasonal_period: int | None = None):
    use_seasonal = seasonal_period if (seasonal_period and len(data) >= 2 * seasonal_period) else None
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=ConvergenceWarning)
        return ExponentialSmoothing(
            data,
            trend="add",
            seasonal="add" if use_seasonal else None,
            seasonal_periods=use_seasonal,
            initialization_method="estimated",
        ).fit(optimized=True)


def _fit_sarima(data: np.ndarray, seasonal_period: int | None = None):
    s = seasonal_period if (seasonal_period and len(data) >= 2 * seasonal_period) else 0
    seasonal_order = (1, 0, 0, s) if s else (0, 0, 0, 0)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=ConvergenceWarning)
        return SARIMAX(
            data,
            order=(1, 1, 1),
            seasonal_order=seasonal_order,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False)


def advanced_forecast(series: pd.Series, periods: int = 12) -> pd.DataFrame:
    """Gelişmiş ensemble (HW, Damped, SARIMA, SES, Theta, Linear)."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    empty_cols = ["ds", "yhat", "yhat_lower", "yhat_upper", "model", "mae", "seasonal_period"]

    if len(s) < 8:
        return pd.DataFrame(columns=empty_cols)

    s = s.tail(104)
    y = s.to_numpy(dtype=float)
    seasonal_period = detect_seasonal_period(s)

    val_size = max(4, min(12, len(y) // 4))
    train, valid = y[:-val_size], y[-val_size:]

    if len(train) < 8:
        return pd.DataFrame(columns=empty_cols)

    candidates: list[dict] = []

    def add_candidate(name, kind, mae_value, sigma_value, forecast_func):
        candidates.append({
            "name": name,
            "kind": kind,
            "mae": float(mae_value),
            "sigma": float(max(sigma_value, 1e-6)),
            "forecast": forecast_func,
        })

    # 1) Holt-Winters
    try:
        season_len = seasonal_period if (seasonal_period and len(train) >= 2 * seasonal_period) else None
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ConvergenceWarning)
            hw = ExponentialSmoothing(
                train, trend="add",
                seasonal="add" if season_len else None,
                seasonal_periods=season_len,
                initialization_method="estimated",
            ).fit(optimized=True)
        hw_valid = np.asarray(hw.forecast(val_size), dtype=float)
        add_candidate(
            "Holt-Winters", "hw",
            _mae(valid, hw_valid),
            np.std(hw.resid) if hasattr(hw, "resid") and len(hw.resid) > 1 else 1.0,
            lambda data: _fit_holt_winters(data, season_len).forecast(periods),
        )
    except Exception:
        pass

    # 2) Damped Holt-Winters
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ConvergenceWarning)
            dhw = ExponentialSmoothing(
                train, trend="add", damped_trend=True,
                initialization_method="estimated",
            ).fit(optimized=True)
        dhw_valid = np.asarray(dhw.forecast(val_size), dtype=float)
        add_candidate(
            "DampedHW", "dhw",
            _mae(valid, dhw_valid),
            np.std(dhw.resid) if hasattr(dhw, "resid") and len(dhw.resid) > 1 else 1.0,
            lambda data: ExponentialSmoothing(
                data, trend="add", damped_trend=True,
                initialization_method="estimated",
            ).fit(optimized=True).forecast(periods),
        )
    except Exception:
        pass

    # 3) SARIMA
    try:
        sarima_season = seasonal_period if (seasonal_period and len(train) >= 2 * seasonal_period) else 0
        sarima_so = (1, 0, 0, sarima_season) if sarima_season else (0, 0, 0, 0)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ConvergenceWarning)
            sarima = SARIMAX(
                train, order=(1, 1, 1), seasonal_order=sarima_so,
                enforce_stationarity=False, enforce_invertibility=False,
            ).fit(disp=False)
        sarima_valid = np.asarray(sarima.forecast(val_size), dtype=float)
        add_candidate(
            f"SARIMA(1,1,1)x{sarima_so}", "sarima",
            _mae(valid, sarima_valid),
            np.std(sarima.resid) if hasattr(sarima, "resid") and len(sarima.resid) > 1 else 1.0,
            lambda data: _fit_sarima(data, seasonal_period).get_forecast(steps=periods).predicted_mean,
        )
    except Exception:
        pass

    # 4) Simple Exponential Smoothing
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ConvergenceWarning)
            ses = SimpleExpSmoothing(train).fit(optimized=True)
        ses_valid = np.asarray(ses.forecast(val_size), dtype=float)
        add_candidate(
            "SES", "ses",
            _mae(valid, ses_valid),
            np.std(ses.resid) if hasattr(ses, "resid") and len(ses.resid) > 1 else 1.0,
            lambda data: SimpleExpSmoothing(data).fit(optimized=True).forecast(periods),
        )
    except Exception:
        pass

    # 5) Theta model (basit lineer + SES harmanlama)
    try:
        lin_fit = np.polyfit(np.arange(len(train), dtype=float), train, 1)
        theta_valid = (
            0.5 * np.polyval(lin_fit, np.arange(len(train), len(train) + val_size, dtype=float))
            + 0.5 * SimpleExpSmoothing(train).fit(optimized=True).forecast(val_size)
        )
        add_candidate(
            "Theta", "theta",
            _mae(valid, theta_valid),
            np.std(train - np.polyval(lin_fit, np.arange(len(train), dtype=float))) if len(train) > 1 else 1.0,
            lambda data: (
                0.5 * np.polyval(np.polyfit(np.arange(len(data), dtype=float), data, 1), np.arange(len(data), len(data) + periods, dtype=float))
                + 0.5 * SimpleExpSmoothing(data).fit(optimized=True).forecast(periods)
            ),
        )
    except Exception:
        pass

    # 6) Linear trend fallback
    x_train = np.arange(len(train), dtype=float)
    slope, intercept = np.polyfit(x_train, train, 1)
    lin_valid = intercept + slope * np.arange(len(train), len(train) + val_size, dtype=float)
    add_candidate(
        "LinearTrend", "linear",
        _mae(valid, lin_valid),
        np.std(train - (intercept + slope * x_train)) if len(train) > 1 else 1.0,
        lambda data: np.polyval(np.polyfit(np.arange(len(data), dtype=float), data, 1), np.arange(len(data), len(data) + periods, dtype=float)),
    )

    if not candidates:
        return pd.DataFrame(columns=empty_cols)

    best = min(candidates, key=lambda c: c["mae"])

    if len(candidates) >= 3:
        selected = sorted(candidates, key=lambda c: c["mae"])[:3]
        inv_mae = [1.0 / max(c["mae"], 1e-6) for c in selected]
        weights = [w / sum(inv_mae) for w in inv_mae]
        model_name = f"Ensemble({'+'.join(c['name'] for c in selected)})"

        yhat = np.zeros(periods)
        yhat_lower = np.zeros(periods)
        yhat_upper = np.zeros(periods)
        sigma = 0.0

        for cand, w in zip(selected, weights):
            try:
                pred = np.asarray(cand["forecast"](y), dtype=float)
                yhat += w * pred
                sigma += w * cand["sigma"]
            except Exception:
                continue

        yhat_lower = yhat - 1.96 * sigma
        yhat_upper = yhat + 1.96 * sigma
        mae_out = float(best["mae"])
    else:
        model_name = best["name"]
        yhat = np.asarray(best["forecast"](y), dtype=float)
        sigma = best["sigma"]
        yhat_lower = yhat - 1.96 * sigma
        yhat_upper = yhat + 1.96 * sigma
        mae_out = float(best["mae"])

    yhat = np.clip(yhat, 0, 100)
    yhat_lower = np.clip(yhat_lower, 0, 100)
    yhat_upper = np.clip(yhat_upper, 0, 100)

    start = pd.Timestamp.today().normalize() + pd.Timedelta(days=7)
    ds = pd.date_range(start=start, periods=periods, freq="W")

    return pd.DataFrame({
        "ds": ds,
        "yhat": np.round(yhat, 2),
        "yhat_lower": np.round(yhat_lower, 2),
        "yhat_upper": np.round(yhat_upper, 2),
        "model": model_name,
        "mae": round(mae_out, 3),
        "seasonal_period": seasonal_period if seasonal_period else 0,
    })


def simple_forecast(series: pd.Series, periods: int = 12) -> pd.DataFrame:
    return advanced_forecast(series=series, periods=periods)


# ══════════════════════════════════════════════════════════════
#  10) TREND GÜCÜ METRİĞİ
# ══════════════════════════════════════════════════════════════

def trend_strength_meter(series: pd.Series) -> dict[str, Any]:
    """Çok boyutlu trend gücü metrici (0-100 arası skor)."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 10:
        return {"score": 0, "components": {}, "label": t("en", "strength_no_data"), "label_key": "strength_no_data"}

    scores = compute_trend_scores(s)
    signal = robust_trend_signal(s)

    # Bileşenler (her biri 0-20 arası)
    slope_score = min(20, abs(signal["slope"]) * 10)
    r2_score = signal.get("r_squared", 0) * 20
    growth_score = min(20, abs(scores["growth_rate"]) / 5)
    momentum_score = min(20, abs(scores["momentum"]) * 2)
    consistency_score = max(0, 20 - signal["volatility"] * 0.5)

    total = slope_score + r2_score + growth_score + momentum_score + consistency_score
    total = min(100, total)

    if total >= 80:
        label_key = "strength_very_strong"
    elif total >= 60:
        label_key = "strength_strong"
    elif total >= 40:
        label_key = "strength_medium"
    elif total >= 20:
        label_key = "strength_weak"
    else:
        label_key = "strength_very_weak"

    return {
        "score": round(total, 1),
        "components": {
            "slope": round(slope_score, 1),
            "r_squared": round(r2_score, 1),
            "growth": round(growth_score, 1),
            "momentum": round(momentum_score, 1),
            "consistency": round(consistency_score, 1),
        },
        "label": t("en", label_key),
        "label_key": label_key,
    }


# ══════════════════════════════════════════════════════════════
#  11) REKLAM KARAR ALGORİTMASI & ÖNERİ MOTORU
# ══════════════════════════════════════════════════════════════

def ad_decision(
    signal: dict,
    scores: dict | None = None,
    forecast_df: pd.DataFrame | None = None,
    budget_base: float = 1000.0,
) -> dict[str, Any]:
    """Reklam kampanya kararı üretir."""
    direction = signal.get("direction", "flat")
    slope = signal.get("slope", 0.0)
    volatility = signal.get("volatility", 0.0)
    strength = signal.get("strength", 0)

    growth_rate = 0.0
    if scores:
        growth_rate = scores.get("growth_rate", 0.0)

    forecast_trend = 0.0
    if forecast_df is not None and not forecast_df.empty:
        yhat = forecast_df["yhat"].values
        if len(yhat) >= 2:
            forecast_trend = float(yhat[-1] - yhat[0])

    if direction == "up" and growth_rate > 10 and forecast_trend > 0:
        action_key = "action_increase"
        multiplier = 1.5 + min(strength * 0.1, 0.5)
        reason_key = "reason_increase"
        confidence = min(0.95, 0.6 + strength * 0.07)
    elif direction == "up":
        action_key = "action_start"
        multiplier = 1.2 + min(strength * 0.05, 0.3)
        reason_key = "reason_start"
        confidence = min(0.85, 0.5 + strength * 0.07)
    elif direction == "down" and growth_rate < -15:
        action_key = "action_stop"
        multiplier = 0.3
        reason_key = "reason_stop"
        confidence = min(0.9, 0.5 + abs(slope) * 0.1)
    elif direction == "down":
        action_key = "action_reduce"
        multiplier = 0.6
        reason_key = "reason_reduce"
        confidence = min(0.8, 0.4 + abs(slope) * 0.1)
    elif volatility > 20:
        action_key = "action_keep"
        multiplier = 0.9
        reason_key = "reason_keep_volatile"
        confidence = 0.55
    else:
        action_key = "action_keep"
        multiplier = 1.0
        reason_key = "reason_keep_stable"
        confidence = 0.6

    return {
        "action": t("en", action_key),
        "action_key": action_key,
        "budget_multiplier": round(multiplier, 2),
        "suggested_budget": round(budget_base * multiplier, 2),
        "reason": t("en", reason_key),
        "reason_key": reason_key,
        "confidence": round(confidence, 2),
    }


def recommendation_from_signal(signal: dict, lang: str = "en") -> str:
    """Kısa öneri metni."""
    direction = signal.get("direction", "flat")
    volatility = signal.get("volatility", 0.0)

    if direction == "up":
        return t(lang, "recommendation_up")
    if direction == "down":
        return t(lang, "recommendation_down")
    if volatility > 20:
        return t(lang, "recommendation_volatile")
    return t(lang, "recommendation_stable")


# ══════════════════════════════════════════════════════════════
#  12) SAATLİK ANALİZ
# ══════════════════════════════════════════════════════════════

def hourly_analysis(hourly_df: pd.DataFrame, lang: str = "en") -> dict[str, Any]:
    """Saatlik veriden peak saatleri ve heatmap matrisi üretir."""
    if hourly_df.empty or "hour" not in hourly_df.columns:
        return {"peak_hours": [], "heatmap_matrix": pd.DataFrame(), "avg_by_hour": pd.Series(dtype=float)}

    avg_by_hour = hourly_df.groupby("hour")["score"].mean().sort_index()

    threshold = avg_by_hour.quantile(0.75)
    peak_hours = sorted(avg_by_hour[avg_by_hour >= threshold].index.tolist())

    day_names = weekday_labels(lang)
    heatmap = hourly_df.pivot_table(
        values="score", index="dayofweek", columns="hour", aggfunc="mean"
    ).reindex(range(7))
    heatmap.index = [day_names[i] if i < len(day_names) else str(i) for i in heatmap.index]

    return {
        "peak_hours": peak_hours,
        "heatmap_matrix": heatmap,
        "avg_by_hour": avg_by_hour,
    }


def best_ad_hours_text(peak_hours: list[int], lang: str = "en") -> str:
    """Peak saatlerden okunabilir metin üretir."""
    if not peak_hours:
        return t(lang, "hourly_no_data")

    ranges: list[str] = []
    start = peak_hours[0]
    prev = start
    for h in peak_hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            ranges.append(f"{start:02d}:00-{prev + 1:02d}:00")
            start = h
            prev = h
    ranges.append(f"{start:02d}:00-{prev + 1:02d}:00")

    return t(lang, "best_hours_prefix", ranges=", ".join(ranges))


# ══════════════════════════════════════════════════════════════
#  13) ÜLKE RANKING & KARŞILAŞTIRMA
# ══════════════════════════════════════════════════════════════

def country_ranking(
    timeline: pd.DataFrame,
    top_n: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """En çok yükselen ve düşen ülkeler."""
    if timeline.empty:
        empty = pd.DataFrame(columns=["country", "avg_score", "recent_avg", "change_pct"])
        return empty, empty.copy()

    max_date = timeline["date"].max()
    cutoff = max_date - pd.Timedelta(weeks=4)

    recent = timeline[timeline["date"] >= cutoff].groupby("country")["score"].mean()
    older = timeline[timeline["date"] < cutoff].groupby("country")["score"].mean()

    both = pd.DataFrame({"recent_avg": recent, "older_avg": older}).dropna()
    both["change_pct"] = ((both["recent_avg"] - both["older_avg"]) / both["older_avg"].replace(0, 1) * 100).round(2)
    both["avg_score"] = timeline.groupby("country")["score"].mean()

    rising = both.sort_values("change_pct", ascending=False).head(top_n).reset_index()
    falling = both.sort_values("change_pct", ascending=True).head(top_n).reset_index()

    return rising, falling
