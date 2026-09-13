"""Kraken REST client — public market data + private (signed) margin trading
endpoints. Adapted from the sibling kraken-scalper project's client, extended
with leverage-aware order placement (including an exchange-side attached
stop-loss), position closing, and the margin-specific balance/position calls
this bot needs for margin-level and liquidation-risk monitoring.

Public calls retry with exponential backoff. Private calls regenerate the
nonce on every attempt and only retry network-level failures, never API
rejections (never retry into a duplicate order).
"""

import base64
import hashlib
import hmac
import threading
import time
import urllib.parse

import requests

API_URL = "https://api.kraken.com"
USER_AGENT = "KrakenMarginBot/0.1 (personal research bot)"

TRANSIENT_ERRORS = ("EAPI:Rate limit", "EService:Unavailable", "EService:Busy",
                    "EGeneral:Temporary")

_DISPLAY_FIX = {"XBT": "BTC", "XDG": "DOGE"}


class KrakenError(Exception):
    """Base error for all Kraken client failures."""


class KrakenAPIError(KrakenError):
    """Kraken accepted the request but returned an error (bad params, auth...)."""


class KrakenNetworkError(KrakenError):
    """Transport failure / 5xx / transient service error after retries."""


class KrakenClient:
    def __init__(self, api_key="", api_secret="", timeout=15):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.timeout = timeout
        self._sess = requests.Session()
        self._sess.headers["User-Agent"] = USER_AGENT
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def set_credentials(self, api_key, api_secret):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""

    # ------------------------------------------------------------------
    def public(self, method, params=None, retries=3):
        url = f"{API_URL}/0/public/{method}"
        delay = 1.0
        last = None
        for attempt in range(retries):
            try:
                r = self._sess.get(url, params=params or {}, timeout=self.timeout)
                if r.status_code >= 500:
                    raise KrakenNetworkError(f"HTTP {r.status_code}")
                payload = r.json()
                errs = payload.get("error") or []
                if errs:
                    msg = "; ".join(errs)
                    if any(t in msg for t in TRANSIENT_ERRORS):
                        raise KrakenNetworkError(msg)
                    raise KrakenAPIError(msg)
                return payload["result"]
            except (requests.RequestException, ValueError, KrakenNetworkError) as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2
        raise KrakenNetworkError(f"{method} failed after {retries} attempts: {last}")

    def _nonce(self):
        with self._nonce_lock:
            n = int(time.time() * 1000)
            if n <= self._last_nonce:
                n = self._last_nonce + 1
            self._last_nonce = n
            return n

    def _sign(self, path, data):
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = path.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def private(self, method, data=None, retries=2):
        if not (self.api_key and self.api_secret):
            raise KrakenAPIError("Kraken API key/secret not configured in settings")
        path = f"/0/private/{method}"
        delay = 1.0
        last = None
        for attempt in range(retries):
            d = dict(data or {})
            d["nonce"] = self._nonce()
            headers = {"API-Key": self.api_key, "API-Sign": self._sign(path, d)}
            try:
                r = self._sess.post(API_URL + path, data=d, headers=headers,
                                    timeout=self.timeout)
                if r.status_code >= 500:
                    raise KrakenNetworkError(f"HTTP {r.status_code}")
                payload = r.json()
                errs = payload.get("error") or []
                if errs:
                    msg = "; ".join(errs)
                    if any(t in msg for t in TRANSIENT_ERRORS):
                        raise KrakenNetworkError(msg)
                    raise KrakenAPIError(msg)
                return payload["result"]
            except (requests.RequestException, ValueError, KrakenNetworkError) as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2
        raise KrakenNetworkError(f"{method} failed after {retries} attempts: {last}")

    # ---- public market data ------------------------------------------
    def ohlc(self, pair, interval):
        """List of dicts oldest->newest. Last row is the still-forming candle."""
        res = self.public("OHLC", {"pair": pair, "interval": interval})
        key = next(k for k in res if k != "last")
        return [
            {"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
             "l": float(row[3]), "c": float(row[4]), "vwap": float(row[5]),
             "v": float(row[6])}
            for row in res[key]
        ]

    def ticker(self, pairs):
        if isinstance(pairs, str):
            pairs = [pairs]
        res = self.public("Ticker", {"pair": ",".join(pairs)})
        out = {}
        for name, t in res.items():
            bid = float(t["b"][0])
            ask = float(t["a"][0])
            last = float(t["c"][0])
            mid = (bid + ask) / 2.0 if (bid and ask) else last
            out[name] = {
                "last": last, "bid": bid, "ask": ask, "mid": mid,
                "vol24h_base": float(t["v"][1]),
            }
        return out

    def asset_pairs(self, names=None):
        params = {"pair": ",".join(names)} if names else None
        res = self.public("AssetPairs", params)
        pairs = {}
        for name, info in res.items():
            wsname = info.get("wsname", "")
            base_disp = wsname.split("/")[0] if "/" in wsname else info.get("base", "")
            base_disp = _DISPLAY_FIX.get(base_disp, base_disp)
            pairs[name] = {
                "name": name,
                "altname": info.get("altname", name),
                "wsname": wsname,
                "display": f"{base_disp}/{info.get('quote', '')}",
                "pair_decimals": int(info.get("pair_decimals", 5)),
                "lot_decimals": int(info.get("lot_decimals", 8)),
                "ordermin": float(info.get("ordermin", 0) or 0),
                "costmin": float(info.get("costmin", 0) or 0),
                "leverage_buy": [int(x) for x in (info.get("leverage_buy") or [])],
                "leverage_sell": [int(x) for x in (info.get("leverage_sell") or [])],
                "margin_call": float(info.get("margin_call", 80) or 80),
                "margin_stop": float(info.get("margin_stop", 40) or 40),
            }
        return pairs

    def max_leverage(self, pair, side):
        info = self.asset_pairs([pair]).get(pair, {})
        levs = info.get("leverage_buy" if side == "buy" else "leverage_sell") or [1]
        return max(levs) if levs else 1

    # ---- private: account -----------------------------------------------
    def balance(self):
        return self.private("Balance")

    def trade_balance(self, asset="ZUSD"):
        """Margin account summary. Key fields: eb=equivalent balance, e=equity,
        m=margin used, mf=free margin, ml=margin level %, n=unrealized P&L."""
        return self.private("TradeBalance", {"asset": asset})

    def open_positions(self, docalcs=True):
        return self.private("OpenPositions", {"docalcs": "true" if docalcs else "false"})

    def open_orders(self):
        return self.private("OpenOrders")

    def closed_orders(self, start=None):
        data = {}
        if start:
            data["start"] = str(start)
        return self.private("ClosedOrders", data)

    def trades_history(self, start=None):
        data = {}
        if start:
            data["start"] = str(start)
        return self.private("TradesHistory", data)

    def query_orders(self, txids):
        return self.private("QueryOrders", {"txid": ",".join(txids)})

    # ---- private: order placement ----------------------------------------
    def add_order(self, pair, side, volume, ordertype="market", price=None,
                  leverage=None, validate=False, close_ordertype=None, close_price=None,
                  reduce_only=False):
        """Place an order. `leverage` opens/adds to a margin position.

        `close_ordertype`/`close_price` attach a conditional close order (e.g.
        ordertype="stop-loss", price=<stop>) that Kraken triggers automatically
        when this order fills — this is how the exchange-side stop-loss for a
        new position is guaranteed to exist even if this process crashes right
        after the entry fills.
        """
        data = {
            "pair": pair,
            "type": side,               # "buy" | "sell"
            "ordertype": ordertype,
            "volume": f"{volume:.8f}",
        }
        if leverage and int(leverage) > 1:
            data["leverage"] = str(int(leverage))
        if price is not None:
            data["price"] = f"{price:.8f}"
        if close_ordertype:
            data["close[ordertype]"] = close_ordertype
            data["close[price]"] = f"{close_price:.8f}"
        if reduce_only:
            data["reduce_only"] = "true"
        if validate:
            data["validate"] = "true"
        return self.private("AddOrder", data)

    def add_stop_loss_order(self, pair, side, volume, stop_price, leverage=None):
        """Stand-alone stop-loss order (opposing side, triggers a market close)."""
        return self.add_order(pair, side, volume, ordertype="stop-loss",
                              price=stop_price, leverage=leverage, reduce_only=True)

    def cancel_order(self, txid):
        return self.private("CancelOrder", {"txid": txid})

    def close_position(self, pair, side, volume, leverage=None):
        """Close (or reduce) a margin position by placing an opposing market
        order for the same pair — Kraken nets this against the open position."""
        opposite = "sell" if side == "buy" else "buy"
        return self.add_order(pair, opposite, volume, ordertype="market",
                              leverage=leverage, reduce_only=True)
