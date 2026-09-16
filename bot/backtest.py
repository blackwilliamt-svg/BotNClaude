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


def count_candidate_signals(ohlc, signals_cfg):
    """Walk-forward replay (same windowing as run_backtest) that just counts
    how many cycles would have produced a candidate entry signal under
    `signals_cfg` — no trade simulation, so it's cheap enough to run as a
    pre-deploy sanity check over the full history in one pass."""
    sma_slow = signals_cfg.get("sma_slow", 200)
    min_window = sma_slow + 2
    if len(ohlc) <= min_window:
        return {"cycles_evaluated": 0, "candidate_signals": 0}

    cycles_evaluated = 0
    candidate_signals = 0
    for i in range(min_window, len(ohlc)):
        window = ohlc[: i + 1]
        cycles_evaluated += 1
        if generate_signal(window, signals_cfg) is not None:
            candidate_signals += 1
    return {"cycles_evaluated": cycles_evaluated, "candidate_signals": candidate_signals}


def compare_thresholds(ohlc, old_signals_cfg, new_signals_cfg):
    """Sanity-check helper for step 4: how many cycles over `ohlc` would have
    produced a candidate signal under the old thresholds vs. the new
    (loosened) ones. Does not touch risk/cost-filter/Claude-gate logic —
    purely counts how often the local pre-filter would have advanced past
    itself to ask Claude for a decision."""
    return {
        "old": count_candidate_signals(ohlc, old_signals_cfg),
        "new": count_candidate_signals(ohlc, new_signals_cfg),
    }


def _cli_report(pair, interval_min, days, old_signals_cfg, new_signals_cfg, ohlc):
    minutes_per_day = 24 * 60
    max_candles = max(1, int(days * minutes_per_day) // interval_min)
    window = ohlc[-max_candles:] if len(ohlc) > max_candles else ohlc
    actual_days = len(window) * interval_min / minutes_per_day

    result = compare_thresholds(window, old_signals_cfg, new_signals_cfg)

    def _fmt(label, cfg, stats):
        cycles = stats["cycles_evaluated"]
        hits = stats["candidate_signals"]
        pct = (hits / cycles * 100.0) if cycles else 0.0
        print(f"{label} (rsi_oversold={cfg['rsi_oversold']}, rsi_overbought={cfg['rsi_overbought']}, "
              f"volume_mult_min={cfg['volume_mult_min']}, "
              f"require_volume_confirmation={cfg.get('require_volume_confirmation', True)}):")
        print(f"  candidate signals: {hits} / {cycles} cycles ({pct:.2f}%)")

    print("=== Signal threshold sanity check ===")
    print(f"Pair: {pair} | Interval: {interval_min}m | "
          f"Window: ~{actual_days:.1f} days ({len(window)} candles)")
    print()
    _fmt("Old thresholds", old_signals_cfg, result["old"])
    print()
    _fmt("New thresholds", new_signals_cfg, result["new"])
    return result


def main():
    """CLI sanity check: fetches recent OHLC for the configured pair from
    Kraken's public API (no credentials needed) and reports how many cycles
    would have fired a candidate signal under the old (30/70/1.3) thresholds
    vs. whatever `signals` block is currently in config.json. Run before
    flipping a loosened config live:

        python -m bot.backtest --days 7
    """
    import argparse

    from config import load_config
    from .kraken_client import KrakenClient

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--pair", default=None, help="Defaults to the first pair in config.json")
    parser.add_argument("--interval", type=int, default=15, help="OHLC candle size in minutes")
    parser.add_argument("--days", type=float, default=7, help="How many recent days to evaluate")
    args = parser.parse_args()

    cfg = load_config()
    pair = args.pair or (cfg.get("pairs") or ["XBTUSD"])[0]
    new_signals_cfg = cfg.get("signals", {})
    old_signals_cfg = dict(new_signals_cfg)
    old_signals_cfg.update({
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "volume_mult_min": 1.3,
        "require_volume_confirmation": True,
    })

    kc = KrakenClient()
    ohlc = kc.ohlc(pair, args.interval)[:-1]  # drop the still-forming candle, same as the engine
    _cli_report(pair, args.interval, args.days, old_signals_cfg, new_signals_cfg, ohlc)


if __name__ == "__main__":
    main()
