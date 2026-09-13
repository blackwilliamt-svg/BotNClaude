"""Pure-python technical indicators, adapted from the kraken-scalper project.
All series-returning functions return lists aligned to the input length,
padded with None where the indicator is not yet defined. No numpy dependency.
"""


def sma(values, period):
    n = len(values)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    run = sum(values[:period])
    out[period - 1] = run / period
    for i in range(period, n):
        run += values[i] - values[i - period]
        out[i] = run / period
    return out


def rsi(closes, period=14):
    """Wilder-smoothed RSI, 0..100. None until `period` gains/losses exist."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)

    def _rsi(avg_gain, avg_loss):
        if avg_loss <= 1e-12:
            return 100.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def atr(highs, lows, closes, period=14):
    """Wilder Average True Range, in price units. None while warming up."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    val = sum(tr[1:period + 1]) / period
    out[period] = val
    for i in range(period + 1, n):
        val = (val * (period - 1) + tr[i]) / period
        out[i] = val
    return out


def last_valid(series):
    """Most recent non-None value in a series, or None."""
    for v in reversed(series):
        if v is not None:
            return v
    return None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
