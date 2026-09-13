"""Cost-aware pre-filter, applied *before* spending money on a Claude API call.

This is a deliberately simple heuristic (not a probability model): assume the
trade captures roughly half of one ATR move in its favor, net out round-trip
exchange fees and the estimated Claude cost, and only proceed if what's left
clears a configurable minimum edge. Its job is to stop the bot from paying for
a Claude check on trades that can't possibly be worth it after costs, not to
predict actual profitability.
"""


def estimate_edge_usd(size, atr_value, entry_price, taker_fee_pct, claude_cost_estimate_usd,
                       capture_fraction=0.5):
    expected_move_usd = size * atr_value * capture_fraction
    notional = size * entry_price
    round_trip_fees = notional * taker_fee_pct * 2
    return expected_move_usd - round_trip_fees - claude_cost_estimate_usd


def passes_cost_filter(edge_usd, min_edge_usd):
    return edge_usd >= min_edge_usd
