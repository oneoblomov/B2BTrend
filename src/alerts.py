"""Alarm sistemi — Spike tespiti, Telegram ve Email bildirimleri.

Artık herhangi bir anahtar kelime ile çalışır.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import numpy as np
import pandas as pd

from src.config import (
    ALERT_EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    SPIKE_Z_THRESHOLD,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    t,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  SPIKE DETECTION
# ══════════════════════════════════════════════════════════════

def detect_recent_spikes(
    timeline: pd.DataFrame,
    z_threshold: float = SPIKE_Z_THRESHOLD,
    lookback_days: int = 7,
) -> pd.DataFrame:
    """Son N gün içindeki spike'ları tespit eder."""
    if timeline.empty:
        return pd.DataFrame(columns=["country", "city", "date", "score", "z_score"])

    cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
    recent = timeline[timeline["date"] >= cutoff].copy()

    if recent.empty:
        return pd.DataFrame(columns=["country", "city", "date", "score", "z_score"])

    spikes_list: list[dict] = []

    for (country, city), grp in timeline.groupby(["country", "city"]):
        values = grp["score"].to_numpy(dtype=float)
        if len(values) < 5:
            continue

        mean = np.mean(values)
        std = np.std(values)
        if std == 0:
            continue

        recent_grp = grp[grp["date"] >= cutoff]
        for _, row in recent_grp.iterrows():
            z = (row["score"] - mean) / std
            if abs(z) > z_threshold:
                spikes_list.append({
                    "country": country,
                    "city": city,
                    "date": row["date"],
                    "score": row["score"],
                    "z_score": round(z, 2),
                })

    return pd.DataFrame(spikes_list)


# ══════════════════════════════════════════════════════════════
#  ALERT MESSAGE BUILDER
# ══════════════════════════════════════════════════════════════

def _build_alert_message(spikes: pd.DataFrame, keyword: str = "", lang: str = "en") -> str:
    """Spike listesinden okunabilir alarm mesajı oluşturur."""
    if spikes.empty:
        return ""

    kw_label = keyword if keyword else t(lang, "app_title")

    lines = [
        f"🚨 {kw_label.upper()} {t(lang, 'alert_header')} 🚨",
        f"{t(lang, 'alert_date')}: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"{t(lang, 'alert_total_spikes')}: {len(spikes)}",
        "─" * 40,
    ]

    for _, row in spikes.iterrows():
        direction = t(lang, "alert_increase") if row["z_score"] > 0 else t(lang, "alert_decrease")
        lines.append(
            f"{direction} | {row['country']} / {row['city']} | "
            f"Skor: {row['score']} | Z: {row['z_score']}"
        )

    lines.append("─" * 40)
    lines.append(t(lang, "alert_dashboard_hint"))
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

def send_telegram(message: str) -> bool:
    """Telegram bildirimi gönderir."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram ayarları eksik")
        return False

    try:
        import telegram
        import asyncio

        async def _send():
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, _send()).result(timeout=15)
        except RuntimeError:
            asyncio.run(_send())

        logger.info("Telegram bildirimi gönderildi.")
        return True
    except ImportError:
        logger.error("python-telegram-bot paketi yüklü değil.")
        return False
    except Exception as e:
        logger.error(f"Telegram gönderim hatası: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════

def send_email(subject: str, body: str) -> bool:
    """Email bildirimi gönderir."""
    if not all([SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO]):
        logger.warning("Email ayarları eksik")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_EMAIL_TO

        msg.attach(MIMEText(body, "plain", "utf-8"))
        html_body = body.replace("\n", "<br>")
        msg.attach(MIMEText(f"<html><body><pre>{html_body}</pre></body></html>", "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        logger.info(f"Email gönderildi → {ALERT_EMAIL_TO}")
        return True
    except Exception as e:
        logger.error(f"Email gönderim hatası: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  ANA FONKSİYON
# ══════════════════════════════════════════════════════════════

def check_and_alert(
    timeline: pd.DataFrame,
    z_threshold: float = SPIKE_Z_THRESHOLD,
    lookback_days: int = 7,
    channels: list[str] | None = None,
    keyword: str = "",
    lang: str = "en",
) -> dict[str, Any]:
    """Spike tespiti yapar ve yapılandırılmış kanallara alarm gönderir."""
    channels = channels or ["telegram", "email"]

    spikes = detect_recent_spikes(timeline, z_threshold, lookback_days)
    message = _build_alert_message(spikes, keyword=keyword, lang=lang)

    result: dict[str, Any] = {
        "spikes": spikes,
        "message": message,
        "telegram_sent": False,
        "email_sent": False,
    }

    if spikes.empty:
        return result

    if "telegram" in channels:
        result["telegram_sent"] = send_telegram(message)

    if "email" in channels:
        subject = t(lang, "alert_subject", count=len(spikes))
        if keyword:
            subject = f"{keyword} - {subject}"
        result["email_sent"] = send_email(subject, message)

    return result
