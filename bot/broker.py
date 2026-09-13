"""PaperBroker and LiveBroker behind one interface so the engine never
branches on trading mode. Both operate on the same `db.Database` trade rows;
PaperBroker simulates fills (with slippage/fees) against live ticker prices,
LiveBroker places real Kraken margin orders including an attached exchange-
side stop-loss.
"""

import logging
import time

logger = logging.getLogger("bot.broker")


class BrokerError(Exception):
    pass


class PaperBroker:
    mode = "paper"

    def __init__(self, db, kraken_client, config):
        self.db = db
        self.kc = kraken_client
        self.cfg = config

    def _slippage_price(self, side, price):
        bps = self.cfg.get("paper.slippage_bps", 5)
        adj = price * (bps / 10000.0)
        return price + adj if side == "buy" else price - adj

    def open_position(self, pair, side, size, entry_price, leverage, stop_price,
                      liquidation_price_est, reasoning_snapshot):
        fill_price = self._slippage_price(side, entry_price)
        trade_id = self.db.open_trade(
            pair=pair, side=side, mode="paper", entry_price=fill_price, size=size,
            leverage=leverage, stop_price=stop_price,
            liquidation_price_est=liquidation_price_est,
            reasoning_snapshot=reasoning_snapshot,
        )
        logger.info("PAPER open %s %s size=%.6f @ %.2f lev=%sx stop=%.2f",
                   side, pair, size, fill_price, leverage, stop_price)
        return trade_id, fill_price

    def close_position(self, trade, exit_reason):
        current = self.kc.ticker([trade["pair"]])[trade["pair"]]
        raw_price = current["bid"] if trade["side"] == "buy" else current["ask"]
        fill_price = self._slippage_price("sell" if trade["side"] == "buy" else "buy", raw_price)
        pnl, pnl_pct, fees = self._pnl(trade, fill_price)
        self.db.close_trade(trade["id"], fill_price, exit_reason, pnl, pnl_pct, fees)
        logger.info("PAPER close #%s %s @ %.2f reason=%s pnl=%.2f",
                   trade["id"], trade["pair"], fill_price, exit_reason, pnl)
        return fill_price, pnl

    def check_stop_hit(self, trade, current_price):
        if trade["side"] == "buy":
            return current_price <= trade["stop_price"]
        return current_price >= trade["stop_price"]

    def update_stop(self, trade, new_stop_price):
        self.db.update_trade_stop(trade["id"], new_stop_price)

    def reduce_position(self, trade, new_size):
        fraction_closed = 1.0 - (new_size / trade["size"])
        current = self.kc.ticker([trade["pair"]])[trade["pair"]]
        raw_price = current["bid"] if trade["side"] == "buy" else current["ask"]
        fill_price = self._slippage_price("sell" if trade["side"] == "buy" else "buy", raw_price)
        partial = dict(trade)
        partial["size"] = trade["size"] * fraction_closed
        pnl, _, fees = self._pnl(partial, fill_price)
        self.db.update_trade_size(trade["id"], new_size)
        logger.info("PAPER reduce #%s to %.6f @ %.2f realized_pnl=%.2f",
                   trade["id"], new_size, fill_price, pnl)
        return pnl

    def _pnl(self, trade, exit_price):
        direction = 1 if trade["side"] == "buy" else -1
        pnl = direction * (exit_price - trade["entry_price"]) * trade["size"]
        notional = trade["entry_price"] * trade["size"]
        fees = notional * self.cfg.get("cost_filter.taker_fee_pct", 0.0026) * 2
        pnl -= fees
        pnl_pct = (pnl / (notional / max(trade.get("leverage") or 1, 1))) * 100.0 if notional else 0.0
        return pnl, pnl_pct, fees

    def account_equity(self):
        starting = self.cfg.get("paper.starting_equity", 10000.0)
        closed = self.db.all_closed_trades()
        realized = sum(t["pnl"] or 0.0 for t in closed)
        unrealized = 0.0
        open_trades = self.db.open_trades()
        if open_trades:
            pairs = list({t["pair"] for t in open_trades})
            tickers = self.kc.ticker(pairs)
            for t in open_trades:
                px = tickers.get(t["pair"], {}).get("last", t["entry_price"])
                direction = 1 if t["side"] == "buy" else -1
                unrealized += direction * (px - t["entry_price"]) * t["size"]
        return starting + realized + unrealized

    def margin_level(self):
        return None  # no real margin account in paper mode


class LiveBroker:
    mode = "live"

    def __init__(self, db, kraken_client, config):
        self.db = db
        self.kc = kraken_client
        self.cfg = config

    def open_position(self, pair, side, size, entry_price, leverage, stop_price,
                      liquidation_price_est, reasoning_snapshot):
        close_type = "stop-loss"
        result = self.kc.add_order(
            pair, side, size, ordertype="market", leverage=leverage,
            close_ordertype=close_type, close_price=stop_price,
        )
        order_ids = result.get("txid") or []
        order_id = order_ids[0] if order_ids else None
        fill_price = entry_price  # reconciler.py corrects this from actual fills
        trade_id = self.db.open_trade(
            pair=pair, side=side, mode="live", entry_price=fill_price, size=size,
            leverage=leverage, stop_price=stop_price,
            liquidation_price_est=liquidation_price_est,
            reasoning_snapshot=reasoning_snapshot, kraken_order_id=order_id,
        )
        logger.info("LIVE open %s %s size=%.6f lev=%sx stop=%.2f order=%s",
                   side, pair, size, leverage, stop_price, order_id)
        return trade_id, fill_price

    def close_position(self, trade, exit_reason):
        result = self.kc.close_position(trade["pair"], trade["side"], trade["size"],
                                        leverage=trade.get("leverage"))
        order_ids = result.get("txid") or []
        current = self.kc.ticker([trade["pair"]])[trade["pair"]]
        fill_price = current["last"]  # reconciler.py corrects this from actual fills
        pnl, pnl_pct, fees = self._pnl(trade, fill_price)
        self.db.close_trade(trade["id"], fill_price, exit_reason, pnl, pnl_pct, fees)
        logger.info("LIVE close #%s %s reason=%s order=%s", trade["id"], trade["pair"],
                   exit_reason, order_ids)
        return fill_price, pnl

    def check_stop_hit(self, trade, current_price):
        # In live mode the exchange-side stop order handles this; this is only
        # a local sanity check used for dashboard display / logging.
        if trade["side"] == "buy":
            return current_price <= trade["stop_price"]
        return current_price >= trade["stop_price"]

    def update_stop(self, trade, new_stop_price):
        if trade.get("kraken_stop_order_id"):
            try:
                self.kc.cancel_order(trade["kraken_stop_order_id"])
            except Exception:
                logger.exception("Failed to cancel old stop order for trade #%s", trade["id"])
        opposite = "sell" if trade["side"] == "buy" else "buy"
        result = self.kc.add_stop_loss_order(trade["pair"], opposite, trade["size"],
                                             new_stop_price, leverage=trade.get("leverage"))
        order_ids = result.get("txid") or []
        self.db.update_trade_stop(trade["id"], new_stop_price,
                                  order_ids[0] if order_ids else None)

    def reduce_position(self, trade, new_size):
        reduce_by = trade["size"] - new_size
        opposite = "sell" if trade["side"] == "buy" else "buy"
        self.kc.add_order(trade["pair"], opposite, reduce_by, ordertype="market",
                          leverage=trade.get("leverage"), reduce_only=True)
        self.db.update_trade_size(trade["id"], new_size)
        logger.info("LIVE reduce #%s to %.6f", trade["id"], new_size)

    def _pnl(self, trade, exit_price):
        direction = 1 if trade["side"] == "buy" else -1
        pnl = direction * (exit_price - trade["entry_price"]) * trade["size"]
        notional = trade["entry_price"] * trade["size"]
        fees = notional * self.cfg.get("cost_filter.taker_fee_pct", 0.0026) * 2
        pnl -= fees
        pnl_pct = (pnl / (notional / max(trade.get("leverage") or 1, 1))) * 100.0 if notional else 0.0
        return pnl, pnl_pct, fees

    def account_equity(self):
        tb = self.kc.trade_balance()
        return float(tb.get("e", 0.0))

    def margin_level(self):
        tb = self.kc.trade_balance()
        ml = tb.get("ml")
        return float(ml) if ml not in (None, "") else None


def make_broker(mode, db, kraken_client, config):
    return PaperBroker(db, kraken_client, config) if mode == "paper" else LiveBroker(db, kraken_client, config)
