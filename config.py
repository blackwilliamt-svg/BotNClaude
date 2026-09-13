"""Config loading and live-editable settings.

`config.json` (tracked) holds all non-secret behaviour knobs. Secrets (Kraken
API key/secret, Anthropic API key, dashboard password) never live here — see
`secrets_store.py`. The dashboard edits a Config in place and calls .save().

A handful of ranges are hard-coded floors/ceilings (not settable from the
dashboard at all) so the exposure/drawdown circuit breakers can never be
disabled or widened past what the spec allows.
"""

import copy
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "bot.db"
STATIC_DIR = ROOT / "static"

# Hard floors/ceilings — enforced in risk.py regardless of what's in config.json.
# Not reachable from any dashboard endpoint.
HARD_LIMITS = {
    "risk_pct_max": 0.02,           # never risk more than 2% of equity per trade
    "per_position_exposure_min": 0.05,
    "per_position_exposure_max": 0.10,
    "total_exposure_min": 0.20,
    "total_exposure_max": 0.30,
    "drawdown_breaker_pct": 0.20,    # fixed — not configurable at all
    "max_leverage_utilization": 0.50,  # never deploy more than 50% of a pair's max leverage
}

DEFAULT_CONFIG = {
    "mode": "paper",                # "paper" | "live" — dashboard toggle
    "kill_switch": False,
    "pairs": ["XBTUSD"],
    "poll_interval_sec": 60,
    "checkin_interval_sec": 900,
    "settings_review_interval_hours": 24,

    "signals": {
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "sma_fast": 50,
        "sma_slow": 200,
        "volume_period": 20,
        "volume_mult_min": 1.3,
    },

    "risk": {
        "risk_pct": 0.01,             # fixed-fractional risk per trade (<= risk_pct_max)
        "atr_period": 14,
        "atr_stop_mult": 1.75,        # within 1.5-2x per spec
        "per_position_exposure_pct": 0.075,  # within [0.05, 0.10]
        "total_exposure_pct": 0.25,          # within [0.20, 0.30]
    },

    "cost_filter": {
        "taker_fee_pct": 0.0026,       # Kraken default taker fee, configurable per account tier
        "claude_cost_estimate_usd": 0.03,
        "min_edge_usd": 1.0,
    },

    "leverage": {
        # confidence (0-100) -> leverage tiers; Claude's own recommendation is
        # clamped to min(this table, kraken pair max, 50% of pair max).
        "tiers": [
            {"min_confidence": 0, "max_confidence": 40, "leverage": 0},
            {"min_confidence": 40, "max_confidence": 60, "leverage": 1},
            {"min_confidence": 60, "max_confidence": 75, "leverage": 2},
            {"min_confidence": 75, "max_confidence": 90, "leverage": 5},
            {"min_confidence": 90, "max_confidence": 101, "leverage": 10},
        ],
    },

    "claude": {
        "entry_model": "claude-sonnet-5",
        "checkin_model": "claude-sonnet-5",
        "settings_review_model": "claude-sonnet-5",
    },

    "paper": {
        "starting_equity": 10000.0,
        "slippage_bps": 5,
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Thread-safe dotted-path view over the config dict."""

    def __init__(self, data, path=CONFIG_PATH):
        self._data = data
        self._path = Path(path)
        self._lock = threading.RLock()

    def get(self, dotted, default=None):
        cur = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted, value):
        with self._lock:
            parts = dotted.split(".")
            cur = self._data
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
            self.save()

    def update(self, patch):
        """Deep-merge a patch dict (from the dashboard), clamp to hard limits, persist."""
        with self._lock:
            self._data = _deep_merge(self._data, patch or {})
            self._clamp_to_hard_limits()
            self.save()

    def _clamp_to_hard_limits(self):
        r = self._data.setdefault("risk", {})
        r["risk_pct"] = min(float(r.get("risk_pct", 0.01)), HARD_LIMITS["risk_pct_max"])
        r["per_position_exposure_pct"] = max(HARD_LIMITS["per_position_exposure_min"],
            min(float(r.get("per_position_exposure_pct", 0.075)), HARD_LIMITS["per_position_exposure_max"]))
        r["total_exposure_pct"] = max(HARD_LIMITS["total_exposure_min"],
            min(float(r.get("total_exposure_pct", 0.25)), HARD_LIMITS["total_exposure_max"]))
        # drawdown breaker and max leverage utilization are never read from
        # config at all — risk.py imports HARD_LIMITS directly.

    def as_dict(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self):
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    @property
    def mode(self):
        return self.get("mode", "paper")

    @property
    def is_live(self):
        return self.mode == "live"


def load_config():
    if not CONFIG_PATH.exists():
        cfg = Config(copy.deepcopy(DEFAULT_CONFIG))
        cfg.save()
        return cfg
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data = _deep_merge(DEFAULT_CONFIG, data)
    cfg = Config(data)
    cfg._clamp_to_hard_limits()
    return cfg
