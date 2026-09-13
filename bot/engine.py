"""Asyncio orchestrator: per-pair scan loop (signal -> cost filter -> Claude
entry check -> size/leverage -> place order+stop), an open-position check-in
loop, and a daily settings-review job. Kill-switch is checked at the top of
every loop iteration; nothing here retries a failed order into a duplicate.
"""

import asyncio
import json
import logging
import time
import traceback
from datetime import datetime, timezone

from .broker import make_broker
from .claude_gate import ClaudeGate, ClaudeGateError
from .cost_filter import estimate_edge_usd, passes_cost_filter
from .kraken_client import KrakenClient, KrakenError
from .reconciler import Reconciler
from .risk import (
    compute_atr_stop, position_size_and_margin, drawdown_triggered,
    resolve_leverage, liquidation_price_estimate, margin_level_status,
    exposure_summary,
)
from .signals import generate_signal
from .backtest import compare_configs

logger = logging.getLogger("bot.engine")

CANDLE_INTERVAL_MIN = 15  # Kraken OHLC candle size used for signal generation


class Engine:
    def __init__(self, db, config, secrets_store):
        self.db = db
        self.cfg = config
        self.secrets = secrets_store
        self.kc = KrakenClient()
        self.claude = ClaudeGate(secrets_store, config)
        self.reconciler = Reconciler(db, self.kc)
        self._broker_mode = None
        self.broker = None
        self._refresh_broker()
        self._tasks = []
        self._latest_status = {}
        # Self-check state, surfaced via status() / /api/status so a stalled
        # or never-started loop is visible from the dashboard, not just logs.
        self._last_poll_at = None
        self._last_checkin_poll_at = None
        self._last_loop_error = None

    # ---- lifecycle --------------------------------------------------------
    def start(self):
        self._tasks = [
            asyncio.create_task(self._loop_guard(self.scan_loop, "scan_loop"), name="scan_loop"),
            asyncio.create_task(self._loop_guard(self.checkin_loop, "checkin_loop"), name="checkin_loop"),
            asyncio.create_task(self._loop_guard(self.settings_review_loop, "settings_review_loop"),
                                name="settings_review_loop"),
        ]
        logger.info("[engine] scheduled %d background loop task(s): %s",
                   len(self._tasks), ", ".join(t.get_name() for t in self._tasks))

    async def stop(self):
        for t in self._tasks:
            t.cancel()

    async def _loop_guard(self, fn, name):
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                tb = traceback.format_exc()
                logger.exception("%s crashed, restarting in 30s", name)
                # Full traceback goes into the event log (not just the journal/
                # console) so a crash loop is visible from the dashboard itself.
                self._last_loop_error = {"loop": name, "ts": time.time(), "traceback": tb}
                self.db.insert_event("error", f"{name} crashed and is restarting:\n{tb}")
                await asyncio.sleep(30)

    def _refresh_broker(self):
        mode = self.cfg.mode
        if mode != self._broker_mode:
            self.broker = make_broker(mode, self.db, self.kc, self.cfg)
            self._broker_mode = mode
            logger.info("Broker mode set to %s", mode)
        if mode == "live":
            key, secret = self.secrets.kraken_credentials()
            self.kc.set_credentials(key, secret)

    def _should_run(self):
        return not self.cfg.get("kill_switch", False)

    def _update_peak_equity(self, equity):
        peak = self.db.kv_get("peak_equity", equity)
        if equity > peak:
            peak = equity
            self.db.kv_set("peak_equity", peak)
        return peak

    def pair_display(self, pair):
        try:
            return self.kc.asset_pairs([pair])[pair]["display"]
        except Exception:
            return pair

    # ---- scan loop (entries) ----------------------------------------------
    async def scan_loop(self):
        while True:
            self._last_poll_at = time.time()
            logger.info("[engine] poll cycle started at %s",
                       datetime.now(timezone.utc).isoformat())
            self._refresh_broker()
            if self._should_run():
                for pair in self.cfg.get("pairs", []):
                    try:
                        await self._scan_pair(pair)
                    except (KrakenError, ClaudeGateError) as e:
                        logger.warning("scan_pair(%s) failed: %s", pair, e)
                        self.db.insert_event("warning", f"{pair} scan failed: {e}")
                    except Exception:
                        tb = traceback.format_exc()
                        logger.exception("scan_pair(%s) unexpected error", pair)
                        self._last_loop_error = {"loop": f"scan_pair:{pair}", "ts": time.time(), "traceback": tb}
                        self.db.insert_event("error", f"{pair} scan hit an unexpected error:\n{tb}")
                try:
                    equity = self.broker.account_equity()
                    self.db.insert_equity(equity, self.cfg.mode)
                    self._update_peak_equity(equity)
                except Exception:
                    tb = traceback.format_exc()
                    logger.exception("Failed to record equity")
                    self.db.insert_event("error", f"Failed to record equity:\n{tb}")
            await asyncio.sleep(self.cfg.get("poll_interval_sec", 60))

    async def _scan_pair(self, pair):
        if any(t["pair"] == pair for t in self.db.open_trades()):
            return  # one position per pair at a time

        ohlc_full = self.kc.ohlc(pair, CANDLE_INTERVAL_MIN)
        ohlc = ohlc_full[:-1]  # drop the still-forming candle
        signals_cfg = self.cfg.get("signals", {})
        candidate = generate_signal(ohlc, signals_cfg)
        if not candidate:
            return

        risk_cfg = self.cfg.get("risk", {})
        stop_price, atr_value = compute_atr_stop(
            ohlc, candidate["side"], candidate["entry_price"],
            risk_cfg.get("atr_period", 14), risk_cfg.get("atr_stop_mult", 1.75),
        )
        if stop_price is None:
            return

        equity = self.broker.account_equity()
        peak = self._update_peak_equity(equity)
        if drawdown_triggered(peak, equity):
            self.db.insert_event("info", f"Drawdown breaker active — skipping new entries")
            return

        open_trades = self.db.open_trades()
        open_margin_used, _ = exposure_summary(open_trades, equity)
        kraken_pair_max = self.kc.max_leverage(pair, candidate["side"])

        tentative_size, _, _ = position_size_and_margin(
            equity, risk_cfg.get("risk_pct", 0.01), candidate["entry_price"], stop_price,
            leverage=1, per_position_exposure_pct=risk_cfg.get("per_position_exposure_pct", 0.075),
            total_exposure_pct=risk_cfg.get("total_exposure_pct", 0.25),
            open_margin_used=open_margin_used,
        )
        if tentative_size <= 0:
            return

        cost_cfg = self.cfg.get("cost_filter", {})
        edge = estimate_edge_usd(tentative_size, atr_value, candidate["entry_price"],
                                 cost_cfg.get("taker_fee_pct", 0.0026),
                                 cost_cfg.get("claude_cost_estimate_usd", 0.03))
        if not passes_cost_filter(edge, cost_cfg.get("min_edge_usd", 1.0)):
            return

        pair_display = self.pair_display(pair)
        account_context = {
            "equity": equity,
            "open_position_count": len(open_trades),
            "drawdown_pct": ((peak - equity) / peak * 100.0) if peak else 0.0,
        }
        parsed, response, cost = self.claude.entry_check(candidate, pair_display, account_context)
        decision_id = self.db.insert_decision(
            trade_id=None, stage="entry", model=self.cfg.get("claude.entry_model"),
            pair=pair, claude_raw_response=response.to_json(), approved=parsed["approve"],
            confidence=parsed["confidence"], leverage_rec=parsed["leverage_recommendation"],
            cost_usd=cost, summary=parsed["rationale"],
        )
        if not parsed["approve"]:
            return

        leverage_tiers = self.cfg.get("leverage.tiers", [])
        leverage = resolve_leverage(parsed["confidence"], leverage_tiers, kraken_pair_max)
        if leverage <= 0:
            self.db.insert_event("info", f"{pair_display}: Claude approved but confidence "
                                        f"tier maps to 0x leverage — no trade")
            return

        size, margin_required, size_reason = position_size_and_margin(
            equity, risk_cfg.get("risk_pct", 0.01), candidate["entry_price"], stop_price,
            leverage=leverage, per_position_exposure_pct=risk_cfg.get("per_position_exposure_pct", 0.075),
            total_exposure_pct=risk_cfg.get("total_exposure_pct", 0.25),
            open_margin_used=open_margin_used,
        )
        if size <= 0:
            self.db.insert_event("info", f"{pair_display}: exposure cap reached, skipping trade")
            return

        margin_stop_pct = self.kc.asset_pairs([pair]).get(pair, {}).get("margin_stop", 40.0)
        liq_est = liquidation_price_estimate(candidate["entry_price"], leverage,
                                             candidate["side"], margin_stop_pct)

        reasoning_snapshot = {
            "signal": candidate["reasoning_text"],
            "claude_rationale": parsed["rationale"],
            "confidence": parsed["confidence"],
            "cost_filter_edge_usd": edge,
        }
        trade_id, fill_price = self.broker.open_position(
            pair, candidate["side"], size, candidate["entry_price"], leverage,
            stop_price, liq_est, reasoning_snapshot,
        )
        self.db.set_decision_trade_id(decision_id, trade_id)
        if self.broker.mode == "live":
            self.reconciler.reconcile_trade(self.db.get_trade(trade_id))
        logger.info("Opened %s %s trade #%s size=%.6f lev=%sx", candidate["side"], pair,
                   trade_id, size, leverage)

    # ---- check-in loop (open positions) ------------------------------------
    async def checkin_loop(self):
        while True:
            self._last_checkin_poll_at = time.time()
            logger.info("[engine] checkin poll cycle started at %s",
                       datetime.now(timezone.utc).isoformat())
            if self._should_run():
                for trade in self.db.open_trades():
                    try:
                        await self._checkin_trade(trade)
                    except (KrakenError, ClaudeGateError) as e:
                        logger.warning("checkin(#%s) failed: %s", trade["id"], e)
                        self.db.insert_event("warning", f"Check-in for trade #{trade['id']} failed: {e}")
                    except Exception:
                        tb = traceback.format_exc()
                        logger.exception("checkin(#%s) unexpected error", trade["id"])
                        self._last_loop_error = {"loop": f"checkin:{trade['id']}", "ts": time.time(), "traceback": tb}
                        self.db.insert_event("error", f"Check-in for trade #{trade['id']} hit an "
                                                      f"unexpected error:\n{tb}")
            if self.broker.mode == "live":
                try:
                    self.reconciler.reconcile_all_open_live()
                except Exception:
                    logger.exception("Reconciliation pass failed")
            await asyncio.sleep(self.cfg.get("checkin_interval_sec", 900))

    async def _checkin_trade(self, trade):
        ticker = self.kc.ticker([trade["pair"]])[trade["pair"]]
        current_price = ticker["last"]

        if self.broker.check_stop_hit(trade, current_price):
            self.broker.close_position(trade, "stop_hit")
            return

        direction = 1 if trade["side"] == "buy" else -1
        notional = trade["entry_price"] * trade["size"]
        margin = notional / max(trade.get("leverage") or 1, 1)
        unrealized = direction * (current_price - trade["entry_price"]) * trade["size"]
        unrealized_pct = (unrealized / margin * 100.0) if margin else 0.0

        pair_display = self.pair_display(trade["pair"])
        parsed, response, cost = self.claude.checkin_check(trade, current_price, unrealized_pct, pair_display)
        self.db.insert_decision(
            trade_id=trade["id"], stage="checkin", model=self.cfg.get("claude.checkin_model"),
            pair=trade["pair"], claude_raw_response=response.to_json(), approved=None,
            confidence=None, leverage_rec=parsed.get("new_leverage"), cost_usd=cost,
            summary=parsed["reasoning"],
        )
        self.db.insert_checkin(trade["id"], parsed["action"], parsed["reasoning"])

        action = parsed["action"]
        if action == "hold":
            return
        if action == "tighten_stop" and parsed.get("new_stop_price"):
            new_stop = parsed["new_stop_price"]
            favorable = (new_stop > trade["stop_price"] if trade["side"] == "buy"
                        else new_stop < trade["stop_price"])
            if favorable:
                self.broker.update_stop(trade, new_stop)
            else:
                self.db.insert_event("info", f"Trade #{trade['id']}: ignored non-favorable "
                                            f"stop tighten suggestion")
        elif action == "reduce" and parsed.get("new_size_fraction") is not None:
            fraction = max(0.0, min(1.0, parsed["new_size_fraction"]))
            new_size = trade["size"] * fraction
            if new_size > 0:
                self.broker.reduce_position(trade, new_size)
            else:
                self.broker.close_position(trade, "claude_checkin_reduce_to_zero")
        elif action == "close":
            self.broker.close_position(trade, "claude_checkin")
        elif action == "adjust_leverage":
            # Kraken doesn't support changing leverage on an open position
            # in-place; logged for visibility, not auto-applied.
            self.db.insert_event("info", f"Trade #{trade['id']}: Claude suggested "
                                        f"leverage adjustment to {parsed.get('new_leverage')}x "
                                        f"— not auto-applied, close and re-open manually if desired")

    # ---- settings review loop -----------------------------------------------
    async def settings_review_loop(self):
        while True:
            interval_hours = self.cfg.get("settings_review_interval_hours", 24)
            await asyncio.sleep(max(1, interval_hours) * 3600)
            try:
                self._run_settings_review()
            except ClaudeGateError as e:
                logger.warning("Settings review skipped: %s", e)
            except Exception:
                logger.exception("Settings review failed")

    def _aggregate_trade_stats(self):
        closed = self.db.all_closed_trades()
        if not closed:
            return {"total_closed_trades": 0}
        wins = [t for t in closed if (t["pnl"] or 0) > 0]
        losses = [t for t in closed if (t["pnl"] or 0) <= 0]
        recent_losses = sorted(losses, key=lambda t: t["exit_time"] or 0, reverse=True)[:10]
        return {
            "total_closed_trades": len(closed),
            "win_rate_pct": len(wins) / len(closed) * 100.0,
            "avg_pnl": sum(t["pnl"] or 0 for t in closed) / len(closed),
            "recent_losses": [
                {"pair": t["pair"], "side": t["side"], "pnl": t["pnl"],
                 "exit_reason": t["exit_reason"], "leverage": t["leverage"]}
                for t in recent_losses
            ],
        }

    def _tunable_config_snapshot(self):
        cfg = self.cfg.as_dict()
        return {"signals": cfg.get("signals"), "risk": cfg.get("risk"),
                "cost_filter": cfg.get("cost_filter"), "leverage": cfg.get("leverage")}

    def _run_settings_review(self):
        stats = self._aggregate_trade_stats()
        if stats.get("total_closed_trades", 0) < 5:
            return  # not enough history yet to say anything meaningful
        snapshot = self._tunable_config_snapshot()
        parsed, response, cost = self.claude.settings_review(stats, snapshot)
        self.db.insert_decision(
            trade_id=None, stage="settings_review", model=self.cfg.get("claude.settings_review_model"),
            pair=None, claude_raw_response=response.to_json(), approved=None, confidence=None,
            leverage_rec=None, cost_usd=cost, summary=parsed["rationale"],
        )
        if parsed.get("proposed_changes"):
            backtest_result = self._backtest_proposal(parsed["proposed_changes"])
            self.db.insert_settings_proposal(parsed["proposed_changes"], parsed["rationale"], backtest_result)

    def _backtest_proposal(self, proposed_changes):
        pair = (self.cfg.get("pairs") or ["XBTUSD"])[0]
        try:
            ohlc = self.kc.ohlc(pair, CANDLE_INTERVAL_MIN)[:-1]
        except Exception:
            return None
        current_signals = self.cfg.get("signals", {})
        current_risk = self.cfg.get("risk", {})
        proposed_signals = dict(current_signals)
        proposed_risk = dict(current_risk)
        for dotted, value in proposed_changes.items():
            if dotted.startswith("signals."):
                proposed_signals[dotted.split(".", 1)[1]] = value
            elif dotted.startswith("risk."):
                proposed_risk[dotted.split(".", 1)[1]] = value
        return compare_configs(ohlc, current_signals, current_risk, proposed_signals, proposed_risk)

    # ---- kill switch / manual controls -------------------------------------
    def kill(self, flatten=False):
        self.cfg.set("kill_switch", True)
        self.db.insert_event("alert", f"Kill switch engaged (flatten={flatten})")
        if flatten:
            for trade in self.db.open_trades():
                try:
                    self.broker.close_position(trade, "kill_switch_flatten")
                except Exception:
                    logger.exception("Failed to flatten trade #%s", trade["id"])

    def resume(self):
        self.cfg.set("kill_switch", False)
        self.db.insert_event("info", "Kill switch released, trading resumed")

    def _loop_alive(self, last_ts, interval_sec):
        if last_ts is None:
            return False
        stale_after = max(180, interval_sec * 3)
        return (time.time() - last_ts) < stale_after

    def status(self):
        equity = self.broker.account_equity() if self.broker else None
        margin_level = self.broker.margin_level() if self.broker else None
        peak = self.db.kv_get("peak_equity", equity)
        scan_alive = self._loop_alive(self._last_poll_at, self.cfg.get("poll_interval_sec", 60))
        checkin_alive = self._loop_alive(self._last_checkin_poll_at, self.cfg.get("checkin_interval_sec", 900))
        return {
            "mode": self.cfg.mode,
            "kill_switch": self.cfg.get("kill_switch", False),
            "equity": equity,
            "peak_equity": peak,
            "drawdown_pct": ((peak - equity) / peak * 100.0) if (peak and equity) else 0.0,
            "margin_level": margin_level,
            "margin_status": margin_level_status(margin_level),
            "open_position_count": len(self.db.open_trades()),
            # Self-check: a loop that never started, or stopped updating
            # its timestamp, shows up here instead of only in the logs.
            "engine_loop_alive": scan_alive and checkin_alive,
            "scan_loop_alive": scan_alive,
            "checkin_loop_alive": checkin_alive,
            "last_poll_at": self._last_poll_at,
            "last_checkin_poll_at": self._last_checkin_poll_at,
            "last_loop_error": self._last_loop_error,
        }
