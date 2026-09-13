"""SQLite persistence: trades, Claude decisions, in-trade check-ins, settings
proposals, equity curve, and a generic kv store (also used by secrets_store.py
for encrypted secret blobs).

Single connection guarded by a lock, WAL mode — same pattern as the sibling
kraken-scalper project.
"""

import json
import sqlite3
import threading
import time

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT, side TEXT, mode TEXT,
    entry_time REAL, entry_price REAL, size REAL, leverage REAL,
    stop_price REAL, liquidation_price_est REAL,
    exit_time REAL, exit_price REAL, exit_reason TEXT,
    pnl REAL, pnl_pct REAL, fees REAL,
    status TEXT DEFAULT 'open',
    reasoning_snapshot TEXT,
    kraken_order_id TEXT, kraken_stop_order_id TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    ts REAL NOT NULL,
    stage TEXT,              -- entry | checkin | settings_review
    model TEXT,
    pair TEXT,
    claude_raw_response TEXT,
    approved INTEGER,
    confidence REAL,
    leverage_rec REAL,
    cost_usd REAL,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    ts REAL NOT NULL,
    action TEXT,             -- hold | tighten_stop | reduce | close | adjust_leverage
    reasoning TEXT
);
CREATE TABLE IF NOT EXISTS settings_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    proposed_json TEXT,
    rationale TEXT,
    backtest_result TEXT,
    status TEXT DEFAULT 'pending',   -- pending | approved | rejected
    applied_ts REAL
);
CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    equity REAL, mode TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_checkins_trade ON checkins(trade_id);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts);
"""


class Database:
    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path or DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
            self._conn.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- trades ---------------------------------------------------------
    def open_trade(self, **kw):
        kw.setdefault("entry_time", time.time())
        kw.setdefault("status", "open")
        if not isinstance(kw.get("reasoning_snapshot"), str):
            kw["reasoning_snapshot"] = json.dumps(kw.get("reasoning_snapshot") or {})
        cols = ("pair", "side", "mode", "entry_time", "entry_price", "size", "leverage",
                "stop_price", "liquidation_price_est", "status", "reasoning_snapshot",
                "kraken_order_id", "kraken_stop_order_id")
        cur = self._exec(
            f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(kw.get(c) for c in cols),
        )
        return cur.lastrowid

    def close_trade(self, trade_id, exit_price, exit_reason, pnl, pnl_pct, fees, exit_time=None):
        self._exec(
            "UPDATE trades SET status='closed', exit_time=?, exit_price=?, exit_reason=?, "
            "pnl=?, pnl_pct=?, fees=? WHERE id=?",
            (exit_time or time.time(), exit_price, exit_reason, pnl, pnl_pct, fees, trade_id),
        )

    def update_trade_stop(self, trade_id, stop_price, kraken_stop_order_id=None):
        if kraken_stop_order_id is not None:
            self._exec("UPDATE trades SET stop_price=?, kraken_stop_order_id=? WHERE id=?",
                       (stop_price, kraken_stop_order_id, trade_id))
        else:
            self._exec("UPDATE trades SET stop_price=? WHERE id=?", (stop_price, trade_id))

    def update_trade_size(self, trade_id, size):
        self._exec("UPDATE trades SET size=? WHERE id=?", (size, trade_id))

    def get_trade(self, trade_id):
        rows = self._query("SELECT * FROM trades WHERE id=?", (trade_id,))
        return rows[0] if rows else None

    def open_trades(self):
        return self._query("SELECT * FROM trades WHERE status='open' ORDER BY id ASC")

    def recent_trades(self, limit=200):
        return self._query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def all_closed_trades(self):
        return self._query("SELECT * FROM trades WHERE status='closed' ORDER BY id ASC")

    # ---- decisions / checkins --------------------------------------------
    def insert_decision(self, **kw):
        kw.setdefault("ts", time.time())
        cols = ("trade_id", "ts", "stage", "model", "pair", "claude_raw_response",
                "approved", "confidence", "leverage_rec", "cost_usd", "summary")
        cur = self._exec(
            f"INSERT INTO decisions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(kw.get(c) for c in cols),
        )
        return cur.lastrowid

    def recent_decisions(self, limit=100):
        return self._query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    def set_decision_trade_id(self, decision_id, trade_id):
        self._exec("UPDATE decisions SET trade_id=? WHERE id=?", (trade_id, decision_id))

    def insert_checkin(self, trade_id, action, reasoning, ts=None):
        self._exec("INSERT INTO checkins (trade_id, ts, action, reasoning) VALUES (?,?,?,?)",
                   (trade_id, ts or time.time(), action, reasoning))

    def checkins_for_trade(self, trade_id):
        return self._query("SELECT * FROM checkins WHERE trade_id=? ORDER BY id ASC", (trade_id,))

    # ---- settings proposals ----------------------------------------------
    def insert_settings_proposal(self, proposed_json, rationale, backtest_result=None):
        if not isinstance(proposed_json, str):
            proposed_json = json.dumps(proposed_json)
        if backtest_result is not None and not isinstance(backtest_result, str):
            backtest_result = json.dumps(backtest_result)
        cur = self._exec(
            "INSERT INTO settings_proposals (ts, proposed_json, rationale, backtest_result) "
            "VALUES (?,?,?,?)", (time.time(), proposed_json, rationale, backtest_result),
        )
        return cur.lastrowid

    def pending_settings_proposals(self):
        return self._query("SELECT * FROM settings_proposals WHERE status='pending' ORDER BY id DESC")

    def all_settings_proposals(self, limit=50):
        return self._query("SELECT * FROM settings_proposals ORDER BY id DESC LIMIT ?", (limit,))

    def set_proposal_status(self, proposal_id, status):
        self._exec("UPDATE settings_proposals SET status=?, applied_ts=? WHERE id=?",
                   (status, time.time() if status == "approved" else None, proposal_id))

    def get_proposal(self, proposal_id):
        rows = self._query("SELECT * FROM settings_proposals WHERE id=?", (proposal_id,))
        return rows[0] if rows else None

    # ---- equity curve / events -------------------------------------------
    def insert_equity(self, equity, mode):
        self._exec("INSERT INTO equity_curve (ts, equity, mode) VALUES (?,?,?)",
                   (time.time(), equity, mode))

    def equity_curve(self, limit=2000):
        rows = self._query("SELECT * FROM equity_curve ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    def insert_event(self, level, message):
        self._exec("INSERT INTO events (ts, level, message) VALUES (?,?,?)",
                   (time.time(), level, str(message)[:2000]))

    def recent_events(self, limit=100):
        return self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---- kv ---------------------------------------------------------------
    def kv_set(self, key, value):
        if not isinstance(value, str):
            value = json.dumps(value)
        self._exec("INSERT INTO kv (key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def kv_get_raw(self, key, default=None):
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_get(self, key, default=None):
        raw = self.kv_get_raw(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def kv_delete(self, key):
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    # ---- housekeeping -------------------------------------------------
    def prune(self, keep_decisions=10000, keep_events=5000):
        with self._lock:
            self._conn.execute(
                "DELETE FROM decisions WHERE id NOT IN "
                "(SELECT id FROM decisions ORDER BY id DESC LIMIT ?)", (keep_decisions,))
            self._conn.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (keep_events,))
            self._conn.commit()
