"""Entry signal generation: RSI(14) oversold/overbought + 50/200 SMA trend
filter + volume confirmation. Pure functions over OHLC candles — no exchange
calls, no state — so they're directly unit-testable.

`compute_snapshot` computes the current indicator readings unconditionally
(RSI, trend, volume ratio) so callers can narrate "what the bot is seeing"
every cycle, independent of whether a trade signal actually fires.
`evaluate` layers the signal decision on top of that same snapshot, so the
narration and the trading decision are always looking at identical numbers —
nothing is computed twice with room to drift apart.
"""

import logging

from .indicators import rsi, sma, last_valid

logger = logging.getLogger("bot.signals")


def _require(signals_cfg, key):
    """Fetch a tunable threshold with no silent fallback — a missing/None
    value in config.json is a misconfiguration, not something we should
    paper over with a hardcoded default that could quietly mask it."""
    value = signals_cfg.get(key)
    if value is None:
        raise ValueError(f"signals config is missing required key '{key}' — check config.json")
    return value


def compute_snapshot(ohlc, signals_cfg):
    """Latest RSI/SMA50/SMA200/volume readings, or None if there isn't enough
    history yet to evaluate the slow SMA."""
    sma_fast_period = signals_cfg.get("sma_fast", 50)
    sma_slow_period = signals_cfg.get("sma_slow", 200)
    rsi_period = signals_cfg.get("rsi_period", 14)
    volume_period = signals_cfg.get("volume_period", 20)

    if len(ohlc) < sma_slow_period + 2:
        return None

    closes = [c["c"] for c in ohlc]
    vols = [c["v"] for c in ohlc]

    last_rsi = last_valid(rsi(closes, rsi_period))
    last_sma_fast = last_valid(sma(closes, sma_fast_period))
    last_sma_slow = last_valid(sma(closes, sma_slow_period))
    last_vol_avg = last_valid(sma(vols, volume_period))

    if None in (last_rsi, last_sma_fast, last_sma_slow, last_vol_avg):
        return None

    last_close = closes[-1]
    last_vol = vols[-1]
    trend = ("up" if last_sma_fast > last_sma_slow
             else "down" if last_sma_fast < last_sma_slow else "flat")
    volume_ratio = (last_vol / last_vol_avg) if last_vol_avg > 0 else 0.0

    return {
        "entry_price": last_close,
        "rsi": last_rsi, "rsi_period": rsi_period,
        "sma_fast": last_sma_fast, "sma_fast_period": sma_fast_period,
        "sma_slow": last_sma_slow, "sma_slow_period": sma_slow_period,
        "trend": trend,
        "volume": last_vol, "volume_avg": last_vol_avg,
        "volume_ratio": volume_ratio, "volume_period": volume_period,
    }


def describe_snapshot(snapshot):
    """One-line plain-language description of the current indicator reading,
    independent of whether a signal fired — used for the per-cycle reasoning
    feed so every pair narrates what it's seeing on every poll, not just on a
    trade."""
    trend_word = {"up": "uptrend", "down": "downtrend", "flat": "flat/no trend"}[snapshot["trend"]]
    return (
        f"RSI({snapshot['rsi_period']})={snapshot['rsi']:.1f}; "
        f"SMA{snapshot['sma_fast_period']} "
        f"{'>' if snapshot['trend'] == 'up' else '<' if snapshot['trend'] == 'down' else '≈'} "
        f"SMA{snapshot['sma_slow_period']} ({trend_word}); "
        f"volume {snapshot['volume_ratio']:.2f}x the {snapshot['volume_period']}-period average."
    )


def evaluate(ohlc, signals_cfg):
    """Returns (snapshot_or_None, candidate_or_None). `snapshot` is the raw
    indicator reading (for narration); `candidate` is non-None only when RSI +
    trend + volume all align into an actual trade signal.

    When no candidate fires, `snapshot["blocked_reasons"]` names exactly
    which condition(s) blocked entry (RSI not extreme / trend mismatch /
    volume not confirmed) so the caller can log something more useful than a
    generic "thresholds not all met"."""
    snapshot = compute_snapshot(ohlc, signals_cfg)
    if snapshot is None:
        return None, None

    oversold = _require(signals_cfg, "rsi_oversold")
    overbought = _require(signals_cfg, "rsi_overbought")
    vol_mult_min = _require(signals_cfg, "volume_mult_min")
    require_volume_confirmation = signals_cfg.get("require_volume_confirmation", True)

    rsi_oversold_hit = snapshot["rsi"] <= oversold
    rsi_overbought_hit = snapshot["rsi"] >= overbought
    trend_up = snapshot["trend"] == "up"
    trend_down = snapshot["trend"] == "down"
    volume_confirmed = (not require_volume_confirmation) or (snapshot["volume_ratio"] >= vol_mult_min)

    snapshot["rsi_oversold_threshold"] = oversold
    snapshot["rsi_overbought_threshold"] = overbought
    snapshot["volume_mult_min"] = vol_mult_min
    snapshot["require_volume_confirmation"] = require_volume_confirmation
    snapshot["volume_confirmed"] = volume_confirmed

    if not require_volume_confirmation:
        logger.debug("volume confirmation disabled by config — treating as satisfied "
                     "(actual ratio %.2fx)", snapshot["volume_ratio"])

    side = None
    if rsi_oversold_hit and trend_up and volume_confirmed:
        side = "buy"
    elif rsi_overbought_hit and trend_down and volume_confirmed:
        side = "sell"

    if side is None:
        blocked_reasons = []
        if not (rsi_oversold_hit or rsi_overbought_hit):
            reason = (f"RSI {snapshot['rsi']:.1f} not extreme "
                      f"(needs <= {oversold} or >= {overbought})")
            blocked_reasons.append(reason)
            logger.debug("signal rejected: %s", reason)
        else:
            rsi_side = "buy" if rsi_oversold_hit else "sell"
            trend_ok = trend_up if rsi_side == "buy" else trend_down
            if not trend_ok:
                reason = (f"RSI hit {rsi_side} threshold but trend is "
                          f"{snapshot['trend']} (needs {'up' if rsi_side == 'buy' else 'down'})")
                blocked_reasons.append(reason)
                logger.debug("signal rejected: %s", reason)
            if not volume_confirmed:
                reason = (f"volume {snapshot['volume_ratio']:.2f}x below required "
                          f"{vol_mult_min}x")
                blocked_reasons.append(reason)
                logger.debug("signal rejected: %s", reason)
        snapshot["blocked_reasons"] = blocked_reasons
        return snapshot, None

    snapshot["blocked_reasons"] = []
    candidate = {
        "side": side,
        "entry_price": snapshot["entry_price"],
        "rsi": snapshot["rsi"],
        "sma_fast": snapshot["sma_fast"],
        "sma_slow": snapshot["sma_slow"],
        "volume": snapshot["volume"],
        "volume_avg": snapshot["volume_avg"],
        "reasoning_text": (
            f"RSI({snapshot['rsi_period']})={snapshot['rsi']:.1f} "
            f"({'oversold' if side == 'buy' else 'overbought'}, threshold "
            f"{oversold if side == 'buy' else overbought}); "
            f"SMA{snapshot['sma_fast_period']} "
            f"{'>' if trend_up else '<'} SMA{snapshot['sma_slow_period']} confirms "
            f"{'uptrend' if side == 'buy' else 'downtrend'}; "
            f"volume {snapshot['volume_ratio']:.2f}x the "
            f"{snapshot['volume_period']}-period average."
        ),
    }
    return snapshot, candidate


def generate_signal(ohlc, signals_cfg):
    """Back-compat wrapper for callers (e.g. bot/backtest.py) that only need
    the trade candidate, not the raw snapshot."""
    return evaluate(ohlc, signals_cfg)[1]
