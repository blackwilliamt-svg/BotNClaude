import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.signals import evaluate

BASE_CFG = {
    "rsi_period": 14,
    "rsi_oversold": 40,
    "rsi_overbought": 60,
    "sma_fast": 50,
    "sma_slow": 200,
    "volume_period": 20,
    "volume_mult_min": 1.0,
    "require_volume_confirmation": True,
}


def _candle(i, c, v):
    return {"t": i, "o": c, "h": c + 0.5, "l": c - 0.5, "c": c, "v": v}


def _uptrend_then_dip(n_up=190, n_down=20, vol=100.0, last_vol=None):
    """Long uptrend (so SMA-fast > SMA-slow) followed by a sharp pullback,
    which drives RSI(14) oversold while the trend is still 'up' — the
    textbook buy-the-dip-in-an-uptrend setup this bot is meant to catch."""
    closes = []
    price = 100.0
    for _ in range(n_up):
        price += 1.0
        closes.append(price)
    for _ in range(n_down):
        price -= 3.0
        closes.append(price)
    ohlc = [_candle(i, c, vol) for i, c in enumerate(closes)]
    if last_vol is not None:
        ohlc[-1]["v"] = last_vol
    return ohlc


def _uptrend_then_spike(n_up=190, n_spike=20, vol=100.0):
    """Uptrend followed by a further sharp rally — pushes RSI overbought
    while the trend is still 'up', not 'down', so a sell candidate should be
    blocked on the trend-mismatch condition rather than firing."""
    closes = []
    price = 100.0
    for _ in range(n_up):
        price += 1.0
        closes.append(price)
    for _ in range(n_spike):
        price += 3.0
        closes.append(price)
    return [_candle(i, c, vol) for i, c in enumerate(closes)]


def test_missing_required_key_raises_instead_of_silently_defaulting():
    ohlc = _uptrend_then_dip()
    bad_cfg = dict(BASE_CFG)
    del bad_cfg["rsi_oversold"]
    with pytest.raises(ValueError):
        evaluate(ohlc, bad_cfg)


def test_loosened_defaults_fire_a_buy_candidate():
    ohlc = _uptrend_then_dip()
    snapshot, candidate = evaluate(ohlc, BASE_CFG)
    assert candidate is not None
    assert candidate["side"] == "buy"
    assert snapshot["blocked_reasons"] == []


def test_rsi_not_extreme_is_reported():
    # Flat-ish series: RSI stays mid-range, never crosses either threshold.
    closes = [100.0 + (i % 3) * 0.1 for i in range(210)]
    ohlc = [_candle(i, c, 100.0) for i, c in enumerate(closes)]
    snapshot, candidate = evaluate(ohlc, BASE_CFG)
    assert candidate is None
    assert len(snapshot["blocked_reasons"]) == 1
    assert "not extreme" in snapshot["blocked_reasons"][0]


def test_trend_mismatch_is_reported():
    ohlc = _uptrend_then_spike()
    snapshot, candidate = evaluate(ohlc, BASE_CFG)
    assert candidate is None
    assert any("trend is" in r for r in snapshot["blocked_reasons"])


def test_volume_not_confirmed_is_reported():
    ohlc = _uptrend_then_dip(last_vol=10.0)  # far below the 20-period average
    snapshot, candidate = evaluate(ohlc, BASE_CFG)
    assert candidate is None
    assert any("volume" in r for r in snapshot["blocked_reasons"])


def test_require_volume_confirmation_false_bypasses_volume_check():
    ohlc = _uptrend_then_dip(last_vol=10.0)
    cfg = dict(BASE_CFG, require_volume_confirmation=False)
    snapshot, candidate = evaluate(ohlc, cfg)
    assert candidate is not None
    assert candidate["side"] == "buy"
