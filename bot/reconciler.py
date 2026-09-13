"""Live-mode fill reconciliation. After a live order is placed we only know
the *intended* price; this polls Kraken's own order/trade records to correct
the trade row with the actual average fill price and size, records slippage,
and raises a dashboard-visible alert on any mismatch (cancelled/expired order,
partial fill, or a fill that doesn't match what we placed) — it never retries
or re-places an order itself.
"""

import logging

logger = logging.getLogger("bot.reconciler")


class Reconciler:
    def __init__(self, db, kraken_client):
        self.db = db
        self.kc = kraken_client

    def reconcile_trade(self, trade):
        """Look up the trade's Kraken order and correct entry_price/size from
        the actual fill. Safe to call multiple times (idempotent)."""
        order_id = trade.get("kraken_order_id")
        if not order_id:
            return
        try:
            result = self.kc.query_orders([order_id])
        except Exception:
            logger.exception("Reconcile: failed to query order %s for trade #%s",
                            order_id, trade["id"])
            return

        info = result.get(order_id)
        if not info:
            self.db.insert_event("warning",
                f"Reconcile: order {order_id} for trade #{trade['id']} not found on Kraken")
            return

        status = info.get("status")
        if status in ("canceled", "expired"):
            self.db.insert_event("alert",
                f"Trade #{trade['id']} order {order_id} was {status} on Kraken but is "
                f"tracked locally as open — manual review needed")
            return

        avg_price = float(info.get("price") or 0.0)
        filled_vol = float(info.get("vol_exec") or 0.0)
        if avg_price <= 0 or filled_vol <= 0:
            return  # not filled yet

        intended_price = trade["entry_price"]
        slippage_pct = ((avg_price - intended_price) / intended_price * 100.0
                        if intended_price else 0.0)
        if abs(slippage_pct) > 0.5:
            self.db.insert_event("warning",
                f"Trade #{trade['id']} slippage {slippage_pct:.2f}% "
                f"(intended {intended_price}, filled {avg_price})")

        size_diff_pct = (abs(filled_vol - trade["size"]) / trade["size"] * 100.0
                        if trade["size"] else 0.0)
        if size_diff_pct > 1.0:
            self.db.insert_event("alert",
                f"Trade #{trade['id']} partial/mismatched fill: intended size "
                f"{trade['size']}, filled {filled_vol}")

        self.db._exec("UPDATE trades SET entry_price=?, size=? WHERE id=?",
                      (avg_price, filled_vol, trade["id"]))

    def reconcile_all_open_live(self):
        for trade in self.db.open_trades():
            if trade["mode"] == "live" and trade.get("kraken_order_id"):
                self.reconcile_trade(trade)

    def check_for_untracked_activity(self, since_ts=None):
        """Best-effort check for account activity Kraken shows that we don't
        have a local trade row for (e.g. a manual trade placed outside the
        bot) — logged as an informational alert, never auto-imported."""
        try:
            history = self.kc.trades_history(start=since_ts)
        except Exception:
            logger.exception("Reconcile: failed to fetch trades history")
            return
        trades = history.get("trades", {})
        known_order_ids = {t.get("kraken_order_id") for t in self.db.recent_trades(500)}
        for txid, tinfo in trades.items():
            order_id = tinfo.get("ordertxid")
            if order_id and order_id not in known_order_ids:
                self.db.insert_event("info",
                    f"Kraken trade {txid} (order {order_id}) not tracked locally — "
                    f"check for manual/external activity on this account")
