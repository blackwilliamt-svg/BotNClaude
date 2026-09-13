import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.indicators import rsi, sma, atr, last_valid, clamp


def test_sma_basic():
    out = sma([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0
    assert out[3] == 3.0
    assert out[4] == 4.0


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 20)]
    out = rsi(closes, 14)
    assert last_valid(out) == 100.0


def test_rsi_all_losses_is_0():
    closes = [float(i) for i in range(20, 1, -1)]
    out = rsi(closes, 14)
    assert last_valid(out) == 0.0


def test_atr_positive_when_ranging():
    highs = [10 + (i % 3) for i in range(30)]
    lows = [9 - (i % 2) for i in range(30)]
    closes = [9.5 + (i % 3) * 0.3 for i in range(30)]
    out = atr(highs, lows, closes, 14)
    val = last_valid(out)
    assert val is not None and val > 0


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
