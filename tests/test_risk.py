import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.risk import (
    position_size_and_margin, drawdown_triggered, leverage_hard_cap,
    resolve_leverage, liquidation_price_estimate, margin_level_status,
    exposure_summary,
)

TIERS = [
    {"min_confidence": 0, "max_confidence": 40, "leverage": 0},
    {"min_confidence": 40, "max_confidence": 60, "leverage": 1},
    {"min_confidence": 60, "max_confidence": 75, "leverage": 2},
    {"min_confidence": 75, "max_confidence": 90, "leverage": 5},
    {"min_confidence": 90, "max_confidence": 101, "leverage": 10},
]


def test_position_size_respects_risk_pct():
    equity = 10000.0
    # Stop 15% away from entry so the resulting margin (~$667) stays well
    # under the 10%-of-equity ($1000) exposure cap and isn't clamped.
    entry, stop = 100.0, 85.0
    size, margin, reason = position_size_and_margin(
        equity, 0.01, entry, stop, leverage=1,
        per_position_exposure_pct=0.10, total_exposure_pct=0.30, open_margin_used=0.0,
    )
    expected_size = (equity * 0.01) / (entry - stop)  # risk_amount / stop_dist
    assert abs(size - expected_size) < 1e-6
    assert reason is None


def test_position_size_clamped_by_per_position_cap():
    equity = 10000.0
    entry, stop = 100.0, 99.99  # tiny stop distance -> huge risk-based size
    size, margin, reason = position_size_and_margin(
        equity, 0.01, entry, stop, leverage=1,
        per_position_exposure_pct=0.05, total_exposure_pct=0.30, open_margin_used=0.0,
    )
    assert margin <= equity * 0.05 + 1e-6
    assert reason == "sized down to exposure cap"


def test_position_size_clamped_by_remaining_total_exposure():
    equity = 10000.0
    size, margin, reason = position_size_and_margin(
        equity, 0.01, 100.0, 98.0, leverage=1,
        per_position_exposure_pct=0.10, total_exposure_pct=0.20,
        open_margin_used=1900.0,  # only 100 of the 2000 total-exposure budget left
    )
    assert margin <= 100.0 + 1e-6


def test_leverage_used_reduces_margin_required():
    equity = 10000.0
    # Stop far enough away that neither case is clamped by the exposure cap,
    # so the comparison isolates the effect of leverage.
    _, margin_1x, _ = position_size_and_margin(
        equity, 0.01, 100.0, 85.0, leverage=1,
        per_position_exposure_pct=0.10, total_exposure_pct=0.30, open_margin_used=0.0)
    _, margin_5x, _ = position_size_and_margin(
        equity, 0.01, 100.0, 85.0, leverage=5,
        per_position_exposure_pct=0.10, total_exposure_pct=0.30, open_margin_used=0.0)
    assert margin_5x < margin_1x


def test_drawdown_breaker():
    assert drawdown_triggered(1000.0, 800.0) is True   # exactly -20%
    assert drawdown_triggered(1000.0, 801.0) is False
    assert drawdown_triggered(0, 100) is False


def test_leverage_hard_cap_is_half_of_pair_max():
    assert leverage_hard_cap(20) == 10
    assert leverage_hard_cap(3) == 1  # int(1.5) == 1, floored


def test_resolve_leverage_tiers_and_clamps():
    assert resolve_leverage(20, TIERS, kraken_pair_max_leverage=20) == 0
    assert resolve_leverage(50, TIERS, kraken_pair_max_leverage=20) == 1
    assert resolve_leverage(95, TIERS, kraken_pair_max_leverage=20) == 10  # 50% of 20
    assert resolve_leverage(95, TIERS, kraken_pair_max_leverage=4) == 2   # 50% of 4


def test_liquidation_price_long_below_entry_short_above():
    entry = 100.0
    liq_long = liquidation_price_estimate(entry, leverage=10, side="buy", margin_stop_pct=40.0)
    liq_short = liquidation_price_estimate(entry, leverage=10, side="sell", margin_stop_pct=40.0)
    assert liq_long < entry
    assert liq_short > entry


def test_liquidation_price_higher_leverage_is_closer_to_entry():
    entry = 100.0
    liq_low_lev = liquidation_price_estimate(entry, leverage=2, side="buy")
    liq_high_lev = liquidation_price_estimate(entry, leverage=10, side="buy")
    assert liq_high_lev > liq_low_lev  # less room to move against you at higher leverage


def test_margin_level_status():
    assert margin_level_status(None) == "unknown"
    assert margin_level_status(50, margin_call_pct=80) == "danger"
    assert margin_level_status(100, margin_call_pct=80) == "warning"
    assert margin_level_status(200, margin_call_pct=80) == "ok"


def test_exposure_summary():
    trades = [
        {"size": 1.0, "entry_price": 100.0, "leverage": 2},   # margin 50
        {"size": 2.0, "entry_price": 50.0, "leverage": 1},    # margin 100
    ]
    total_margin, pct = exposure_summary(trades, equity=1000.0)
    assert abs(total_margin - 150.0) < 1e-6
    assert abs(pct - 0.15) < 1e-6
