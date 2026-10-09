"""
core/feature_engineering.py
Turns raw MarketSnapshot price/volume history into technical indicators
and support/resistance levels. Pure functions over pandas/numpy — no I/O.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.models import MarketSnapshot, TechnicalLevels


def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0 or np.isnan(avg_loss):
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _macd(closes: pd.Series) -> tuple[float, float]:
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return round(float(macd_line.iloc[-1]), 2), round(float(signal_line.iloc[-1]), 2)


def _adx(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> float:
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift()).abs(),
        (lows - closes.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=highs.index).rolling(period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=highs.index).rolling(period).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(period).mean().iloc[-1]
    return round(float(adx), 2) if not np.isnan(adx) else 20.0


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> float:
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift()).abs(),
        (lows - closes.shift()).abs(),
    ], axis=1).max(axis=1)
    return round(float(tr.rolling(period).mean().iloc[-1]), 2)


def compute_technical_levels(snapshot: MarketSnapshot) -> TechnicalLevels:
    """
    Derives a full technical picture from a snapshot's price history.
    Falls back gracefully to synthetic OHLC (from close-only series) since
    the demo provider only stores close prices; a real provider with full
    OHLC history would feed highs/lows directly for more accurate ADX/ATR.
    """
    closes = pd.Series(snapshot.price_history, dtype=float)
    if len(closes) < 30:
        # Not enough history: pad by repeating the first value.
        closes = pd.concat([pd.Series([closes.iloc[0]] * (30 - len(closes))), closes], ignore_index=True)

    # Prefer real daily highs/lows (e.g. from live OHLC data) when available
    # and aligned with the close series; otherwise synthesize a +-0.8% proxy.
    if snapshot.high_history and snapshot.low_history and \
            len(snapshot.high_history) == len(snapshot.price_history) == len(snapshot.low_history):
        highs = pd.Series(snapshot.high_history, dtype=float)
        lows = pd.Series(snapshot.low_history, dtype=float)
        if len(highs) < len(closes):
            pad_h = pd.Series([highs.iloc[0]] * (len(closes) - len(highs)))
            pad_l = pd.Series([lows.iloc[0]] * (len(closes) - len(lows)))
            highs = pd.concat([pad_h, highs], ignore_index=True)
            lows = pd.concat([pad_l, lows], ignore_index=True)
    else:
        highs = closes * 1.008
        lows = closes * 0.992

    price = float(closes.iloc[-1])
    prev_high = float(highs.iloc[-2])
    prev_low = float(lows.iloc[-2])
    prev_close = float(closes.iloc[-2])

    # Classic floor-trader pivot points
    pivot = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * pivot - prev_low
    s1 = 2 * pivot - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    r3 = prev_high + 2 * (pivot - prev_low)
    s3 = prev_low - 2 * (prev_high - pivot)

    sma_50 = float(closes.rolling(min(50, len(closes))).mean().iloc[-1])
    sma_200 = float(closes.rolling(min(200, len(closes))).mean().iloc[-1])
    ema_20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])

    volumes = pd.Series(snapshot.volume_history, dtype=float) if snapshot.volume_history else pd.Series([1] * len(closes))
    if len(volumes) != len(closes):
        volumes = pd.Series([volumes.iloc[-1]] * len(closes))
    vwap = float((closes * volumes).sum() / volumes.sum()) if volumes.sum() > 0 else price

    rsi = _rsi(closes)
    macd, macd_signal = _macd(closes)
    adx = _adx(highs, lows, closes)
    atr = _atr(highs, lows, closes)

    if price > sma_50 > sma_200:
        trend = "Uptrend"
    elif price < sma_50 < sma_200:
        trend = "Downtrend"
    else:
        trend = "Sideways"

    # Nearest support/resistance: closest pivot-derived level below/above price
    supports = sorted([s for s in (s1, s2, s3) if s < price], reverse=True)
    resistances = sorted([r for r in (r1, r2, r3) if r > price])
    nearest_support = supports[0] if supports else s3
    nearest_resistance = resistances[0] if resistances else r3

    recent_high = float(closes.tail(20).max())
    recent_low = float(closes.tail(20).min())

    return TechnicalLevels(
        pivot_point=round(pivot, 2), s1=round(s1, 2), s2=round(s2, 2), s3=round(s3, 2),
        r1=round(r1, 2), r2=round(r2, 2), r3=round(r3, 2),
        breakout_level=round(recent_high, 2),
        breakdown_level=round(recent_low, 2),
        nearest_support=round(nearest_support, 2),
        nearest_resistance=round(nearest_resistance, 2),
        rsi=rsi, macd=macd, macd_signal=macd_signal,
        sma_50=round(sma_50, 2), sma_200=round(sma_200, 2), ema_20=round(ema_20, 2),
        vwap=round(vwap, 2), adx=adx, atr=atr, trend=trend,
    )
