import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.cost_filter import estimate_edge_usd, passes_cost_filter


def test_edge_positive_for_large_atr_move():
    edge = estimate_edge_usd(size=1.0, atr_value=500.0, entry_price=50000.0,
                             taker_fee_pct=0.0026, claude_cost_estimate_usd=0.03)
    # expected_move = 1*500*0.5=250; fees = 50000*0.0026*2=260; edge=250-260-0.03 = -10.03
    assert abs(edge - (-10.03)) < 1e-6
    assert passes_cost_filter(edge, min_edge_usd=1.0) is False


def test_edge_negative_when_fees_dominate_small_move():
    edge = estimate_edge_usd(size=0.01, atr_value=10.0, entry_price=50000.0,
                             taker_fee_pct=0.0026, claude_cost_estimate_usd=0.03)
    assert edge < 0
    assert passes_cost_filter(edge, min_edge_usd=1.0) is False


def test_edge_positive_clears_filter():
    edge = estimate_edge_usd(size=1.0, atr_value=2000.0, entry_price=50000.0,
                             taker_fee_pct=0.0026, claude_cost_estimate_usd=0.03)
    assert passes_cost_filter(edge, min_edge_usd=1.0) is True
