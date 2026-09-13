"""Entry signal generation: RSI(14) oversold/overbought + 50/200 SMA trend
filter + volume confirmation. Pure function over OHLC candles — no exchange
calls, no state — so it's directly unit-testable.
"""

from .indicators import rsi, sma, last_valid


def generate_signal(ohlc, signals_cfg):
    """`ohlc` is oldest->newest list of {o,h,l,c,v} dicts (Kraken candle shape,
    excluding the still-forming last candle — callers should drop it).

    Returns a candidate dict or None if no signal fires or there isn't enough
    history yet to evaluate the slow SMA.
    """
    sma_slow_period = signals_cfg.get("sma_slow", 200)
    if len(ohlc) < sma_slow_period + 2:
        return None

    closes = [c["c"] for c in ohlc]
    vols = [c["v"] for c in ohlc]

    rsi_period = signals_cfg.get("rsi_period", 14)
    rsi_series = rsi(closes, rsi_period)
    sma_fast_series = sma(closes, signals_cfg.get("sma_fast", 50))
    sma_slow_series = sma(closes, sma_slow_period)
    vol_avg_series = sma(vols, signals_cfg.get("volume_period", 20))

    last_rsi = last_valid(rsi_series)
    last_sma_fast = last_valid(sma_fast_series)
    last_sma_slow = last_valid(sma_slow_series)
    last_vol_avg = last_valid(vol_avg_series)
    last_close = closes[-1]
    last_vol = vols[-1]

    if None in (last_rsi, last_sma_fast, last_sma_slow, last_vol_avg):
        return None

    oversold = signals_cfg.get("rsi_oversold", 30)
    overbought = signals_cfg.get("rsi_overbought", 70)
    vol_mult_min = signals_cfg.get("volume_mult_min", 1.3)

    volume_confirmed = last_vol_avg > 0 and (last_vol / last_vol_avg) >= vol_mult_min
    trend_up = last_sma_fast > last_sma_slow
    trend_down = last_sma_fast < last_sma_slow

    side = None
    if last_rsi <= oversold and trend_up and volume_confirmed:
        side = "buy"
    elif last_rsi >= overbought and trend_down and volume_confirmed:
        side = "sell"
    else:
        return None

    return {
        "side": side,
        "entry_price": last_close,
        "rsi": last_rsi,
        "sma_fast": last_sma_fast,
        "sma_slow": last_sma_slow,
        "volume": last_vol,
        "volume_avg": last_vol_avg,
        "reasoning_text": (
            f"RSI({rsi_period})={last_rsi:.1f} "
            f"({'oversold' if side == 'buy' else 'overbought'}, threshold "
            f"{oversold if side == 'buy' else overbought}); "
            f"SMA{signals_cfg.get('sma_fast', 50)} "
            f"{'>' if trend_up else '<'} SMA{sma_slow_period} confirms "
            f"{'uptrend' if side == 'buy' else 'downtrend'}; "
            f"volume {last_vol / last_vol_avg:.2f}x the "
            f"{signals_cfg.get('volume_period', 20)}-period average."
        ),
    }
