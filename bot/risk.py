"""Position sizing, exposure caps, ATR stops, drawdown circuit breaker, and
leverage/liquidation risk math for Kraken margin trading.

Exposure caps (`per_position_exposure_pct` / `total_exposure_pct`) are applied
against *margin committed* (notional / leverage), i.e. the account equity
actually at stake — not raw notional, which with leverage can exceed equity
many times over. This is the standard reading of "% of equity in a position"
for a leveraged account.

Liquidation price is an ESTIMATE from a simplified isolated-margin formula
(see `liquidation_price_estimate`) — treat Kraken's own account margin display
as authoritative. The point of this estimate, per the design spec, is to keep
liquidation a distant backstop, not to be exact to the dollar.
"""

from config import HARD_LIMITS
from .indicators import atr as atr_series_fn, last_valid


def compute_atr_stop(ohlc, side, entry_price, atr_period, atr_stop_mult):
    """Returns (stop_price, atr_value) or (None, None) if not enough history."""
    highs = [c["h"] for c in ohlc]
    lows = [c["l"] for c in ohlc]
    closes = [c["c"] for c in ohlc]
    series = atr_series_fn(highs, lows, closes, atr_period)
    atr_value = last_valid(series)
    if atr_value is None or atr_value <= 0:
        return None, None
    if side == "buy":
        stop_price = entry_price - atr_stop_mult * atr_value
    else:
        stop_price = entry_price + atr_stop_mult * atr_value
    return stop_price, atr_value


def position_size_and_margin(equity, risk_pct, entry_price, stop_price, leverage,
                              per_position_exposure_pct, total_exposure_pct,
                              open_margin_used):
    """Fixed-fractional risk sizing, then clamped down (never up) to respect
    per-position and total exposure caps measured in margin committed.

    Returns (size_base_units, margin_required_usd, rejection_reason_or_None).
    """
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0 or equity <= 0 or leverage < 1:
        return 0.0, 0.0, "invalid stop distance, equity, or leverage"

    risk_amount = equity * risk_pct
    size_from_risk = risk_amount / stop_dist
    notional_from_risk = size_from_risk * entry_price
    margin_from_risk = notional_from_risk / leverage

    max_margin_per_position = equity * per_position_exposure_pct
    max_margin_total = equity * total_exposure_pct
    remaining_total_margin = max(0.0, max_margin_total - open_margin_used)
    margin_cap = min(max_margin_per_position, remaining_total_margin)

    if margin_cap <= 0:
        return 0.0, 0.0, "exposure cap already reached"

    margin_final = min(margin_from_risk, margin_cap)
    notional_final = margin_final * leverage
    size_final = notional_final / entry_price
    reason = None if margin_final >= margin_from_risk else "sized down to exposure cap"
    return size_final, margin_final, reason


def drawdown_triggered(peak_equity, current_equity):
    """True once equity has fallen `HARD_LIMITS['drawdown_breaker_pct']` off
    its peak — blocks *new* entries only; existing positions still manage out."""
    if peak_equity is None or peak_equity <= 0:
        return False
    dd = (peak_equity - current_equity) / peak_equity
    return dd >= HARD_LIMITS["drawdown_breaker_pct"]


def leverage_hard_cap(kraken_pair_max_leverage):
    """Never deploy more than 50% of the pair's own max leverage capacity."""
    return max(1, int(kraken_pair_max_leverage * HARD_LIMITS["max_leverage_utilization"]))


def resolve_leverage(confidence, tiers, kraken_pair_max_leverage):
    """Map Claude's confidence score (0-100) to a leverage tier, then clamp to
    Kraken's own per-pair ceiling and the 50%-utilization hard cap. Returns 0
    if no tier matches (i.e. no trade)."""
    leverage = 0
    for tier in tiers:
        if tier["min_confidence"] <= confidence < tier["max_confidence"]:
            leverage = tier["leverage"]
            break
    if leverage <= 0:
        return 0
    cap = min(kraken_pair_max_leverage, leverage_hard_cap(kraken_pair_max_leverage))
    return max(0, min(leverage, cap))


def liquidation_price_estimate(entry_price, leverage, side, margin_stop_pct=40.0):
    """Simplified isolated-margin liquidation estimate: the price at which
    equity backing the position falls to `margin_stop_pct` of margin used.
    See module docstring for the derivation and its caveats."""
    leverage = max(leverage, 1)
    factor = (margin_stop_pct / 100.0 - 1.0) / leverage
    if side == "buy":
        return entry_price * (1 + factor)
    return entry_price * (1 - factor)


def margin_level_status(current_margin_level_pct, margin_call_pct=80.0):
    """Classify Kraken's real-time margin level (from TradeBalance `ml`)."""
    if current_margin_level_pct is None:
        return "unknown"
    if current_margin_level_pct <= margin_call_pct:
        return "danger"
    if current_margin_level_pct <= margin_call_pct * 1.5:
        return "warning"
    return "ok"


def exposure_summary(open_trades, equity):
    """Total margin currently committed across open positions, and its % of equity."""
    total_margin = sum((t["size"] * t["entry_price"]) / max(t.get("leverage") or 1, 1)
                       for t in open_trades)
    pct = (total_margin / equity) if equity > 0 else 0.0
    return total_margin, pct
