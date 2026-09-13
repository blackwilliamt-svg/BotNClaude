"""Lightweight backtest used to sanity-check a proposed settings change before
a human approves it from the dashboard. Not a full trading simulator — it
replays the same `signals.generate_signal` function bar-by-bar over historical
OHLC and simulates each fired signal forward to its ATR stop or a fixed
reward-multiple target, reporting simple aggregate stats. Good enough to catch
"this parameter change obviously makes things worse", not a substitute for
paper trading the change for real.
"""

from .indicators import atr as atr_series_fn
from .signals import generate_signal

DEFAULT_TARGET_R = 2.0        # simulate exit at 2R if stop isn't hit first
MAX_HOLD_BARS = 200           # give up and exit at market after this many bars


def _simulate_forward(ohlc, entry_idx, side, entry_price, stop_price, atr_value, target_r):
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0:
        return None
    target_price = (entry_price + target_r * stop_dist if side == "buy"
                    else entry_price - target_r * stop_dist)
    end = min(len(ohlc), entry_idx + 1 + MAX_HOLD_BARS)
    for i in range(entry_idx + 1, end):
        bar = ohlc[i]
        if side == "buy":
            if bar["l"] <= stop_price:
                return -1.0
            if bar["h"] >= target_price:
                return target_r
        else:
            if bar["h"] >= stop_price:
                return -1.0
            if bar["l"] <= target_price:
                return target_r
    # Timed out — mark to close price as a fraction of R
    close = ohlc[end - 1]["c"]
    r = ((close - entry_price) / stop_dist if side == "buy"
        else (entry_price - close) / stop_dist)
    return r


def run_backtest(ohlc, signals_cfg, risk_cfg, target_r=DEFAULT_TARGET_R):
    """`ohlc` oldest->newest historical candles (as many as available).
    Returns aggregate stats: trades, win_rate, avg_r, expectancy_r.
    """
    sma_slow = signals_cfg.get("sma_slow", 200)
    atr_period = risk_cfg.get("atr_period", 14)
    atr_stop_mult = risk_cfg.get("atr_stop_mult", 1.75)
    min_window = sma_slow + 2

    highs = [c["h"] for c in ohlc]
    lows = [c["l"] for c in ohlc]
    closes = [c["c"] for c in ohlc]
    full_atr = atr_series_fn(highs, lows, closes, atr_period)

    r_multiples = []
    i = min_window
    while i < len(ohlc):
        window = ohlc[: i + 1]
        candidate = generate_signal(window, signals_cfg)
        if candidate and full_atr[i] not in (None, 0):
            atr_value = full_atr[i]
            entry_price = candidate["entry_price"]
            side = candidate["side"]
            stop_price = (entry_price - atr_stop_mult * atr_value if side == "buy"
                         else entry_price + atr_stop_mult * atr_value)
            r = _simulate_forward(ohlc, i, side, entry_price, stop_price, atr_value, target_r)
            if r is not None:
                r_multiples.append(r)
                i += 5  # skip ahead a bit past this trade to avoid heavily overlapping signals
                continue
        i += 1

    if not r_multiples:
        return {"trades": 0, "win_rate": None, "avg_r": None, "expectancy_r": None}

    wins = [r for r in r_multiples if r > 0]
    return {
        "trades": len(r_multiples),
        "win_rate": len(wins) / len(r_multiples) * 100.0,
        "avg_r": sum(r_multiples) / len(r_multiples),
        "expectancy_r": sum(r_multiples) / len(r_multiples),
    }


def compare_configs(ohlc, current_signals_cfg, current_risk_cfg, proposed_signals_cfg, proposed_risk_cfg):
    return {
        "current": run_backtest(ohlc, current_signals_cfg, current_risk_cfg),
        "proposed": run_backtest(ohlc, proposed_signals_cfg, proposed_risk_cfg),
    }
