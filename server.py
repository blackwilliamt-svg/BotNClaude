"""FastAPI app: mounts /api/* (JSON), serves static/ (dashboard SPA), and gates
every request behind HTTP Basic Auth via middleware (covers both the API and
the static files — a route-level dependency alone would miss the static
mount). Starts the engine's asyncio loops on startup.
"""

import csv
import io
import json
import logging
import secrets as pysecrets
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import config as config_module
from db import Database
from secrets_store import SecretsStore, SecretsUnavailable
from bot.engine import Engine, CANDLE_INTERVAL_MIN
from bot.backtest import compare_configs
from bot.indicators import sma, rsi as rsi_fn

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("server")

cfg = config_module.load_config()
db = Database()

try:
    secrets_store = SecretsStore(db)
except SecretsUnavailable as e:
    raise SystemExit(f"FATAL: {e}")

generated_password = secrets_store.ensure_dashboard_password()
if generated_password:
    logger.warning("=" * 70)
    logger.warning("No dashboard password was set. Generated one for first boot:")
    logger.warning("  username: %s", secrets_store.dashboard_username())
    logger.warning("  password: %s", generated_password)
    logger.warning("Change it from the Settings panel once logged in.")
    logger.warning("=" * 70)

engine = Engine(db, cfg, secrets_store)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, _, pwd = decoded.partition(":")
            except Exception:
                user, pwd = "", ""
            expected_user = secrets_store.dashboard_username()
            expected_pwd = secrets_store.dashboard_password() or ""
            if pysecrets.compare_digest(user, expected_user) and pysecrets.compare_digest(pwd, expected_pwd):
                return await call_next(request)
        return PlainTextResponse("Authentication required", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="kraken-margin-bot"'})


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.start()
    logger.info("Engine started in %s mode", cfg.mode)
    yield
    await engine.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)


def _secret_field(v):
    return v if v else None


# ---- status / positions / equity -----------------------------------------
@app.get("/api/status")
def api_status():
    return engine.status()


@app.get("/api/positions")
def api_positions():
    out = []
    for t in db.open_trades():
        try:
            ticker = engine.kc.ticker([t["pair"]])[t["pair"]]
            current_price = ticker["last"]
        except Exception:
            current_price = None
        direction = 1 if t["side"] == "buy" else -1
        unrealized = (direction * (current_price - t["entry_price"]) * t["size"]
                     if current_price is not None else None)
        out.append({**t, "current_price": current_price, "unrealized_pnl": unrealized})
    return out


@app.get("/api/equity_curve")
def api_equity_curve(limit: int = 2000):
    return db.equity_curve(limit)


@app.get("/api/chart")
def api_chart(pair: Optional[str] = None, limit: int = 300):
    """Price + the exact indicators the engine trades on, all computed with
    the same `bot/indicators.py` functions and config-driven periods the
    engine itself uses (bot/signals.py) — no separate frontend indicator math.
    Confidence markers come straight from logged entry decisions."""
    pair = pair or (cfg.get("pairs") or ["XBTUSD"])[0]
    try:
        ohlc = engine.kc.ohlc(pair, CANDLE_INTERVAL_MIN)[:-1]  # drop forming candle, same as the engine
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch OHLC data: {e}")
    ohlc = ohlc[-limit:]

    signals_cfg = cfg.get("signals", {})
    closes = [c["c"] for c in ohlc]
    vols = [c["v"] for c in ohlc]
    sma_fast_period = signals_cfg.get("sma_fast", 50)
    sma_slow_period = signals_cfg.get("sma_slow", 200)
    rsi_period = signals_cfg.get("rsi_period", 14)
    vol_period = signals_cfg.get("volume_period", 20)

    sma_fast_series = sma(closes, sma_fast_period)
    sma_slow_series = sma(closes, sma_slow_period)
    rsi_series = rsi_fn(closes, rsi_period)
    vol_avg_series = sma(vols, vol_period)

    def to_points(values):
        return [{"time": ohlc[i]["t"], "value": values[i]}
                for i in range(len(ohlc)) if values[i] is not None]

    interval_sec = CANDLE_INTERVAL_MIN * 60
    decisions = db.entry_decisions_for_pair(pair, since_ts=(ohlc[0]["t"] if ohlc else None))
    markers = []
    for d in decisions:
        if d["confidence"] is None:
            continue
        candle_time = int(d["ts"] // interval_sec * interval_sec)
        markers.append({
            "time": candle_time,
            "confidence": d["confidence"],
            "approved": bool(d["approved"]),
            "summary": d["summary"],
        })

    return {
        "pair": pair,
        "candle_interval_min": CANDLE_INTERVAL_MIN,
        "candles": [{"time": c["t"], "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"]}
                   for c in ohlc],
        "volume": [{"time": c["t"], "value": c["v"]} for c in ohlc],
        "sma_fast": to_points(sma_fast_series),
        "sma_slow": to_points(sma_slow_series),
        "sma_fast_period": sma_fast_period,
        "sma_slow_period": sma_slow_period,
        "rsi": to_points(rsi_series),
        "rsi_period": rsi_period,
        "volume_avg": to_points(vol_avg_series),
        "markers": markers,
    }


# ---- trade history ----------------------------------------------------------
@app.get("/api/trades")
def api_trades(limit: int = 200):
    return db.recent_trades(limit)


@app.get("/api/trades/export.csv")
def api_trades_export():
    trades = db.recent_trades(100000)
    buf = io.StringIO()
    if trades:
        writer = csv.DictWriter(buf, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        writer.writerows(trades)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trades.csv"})


# ---- reasoning feed / events -------------------------------------------------
@app.get("/api/decisions")
def api_decisions(limit: int = 100):
    return db.recent_decisions(limit)


@app.get("/api/events")
def api_events(limit: int = 100):
    return db.recent_events(limit)


@app.get("/api/checkins/{trade_id}")
def api_checkins(trade_id: int):
    return db.checkins_for_trade(trade_id)


# ---- settings ------------------------------------------------------------
class SecretsPatch(BaseModel):
    kraken_api_key: Optional[str] = None
    kraken_api_secret: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    dashboard_username: Optional[str] = None
    dashboard_password: Optional[str] = None


class SettingsPatch(BaseModel):
    config: Optional[dict[str, Any]] = None
    secrets: Optional[SecretsPatch] = None


@app.get("/api/settings")
def api_get_settings():
    return {
        "config": cfg.as_dict(),
        "hard_limits": config_module.HARD_LIMITS,
        "secrets": {
            "kraken_api_key": secrets_store.status("kraken_api_key"),
            "kraken_api_secret": secrets_store.status("kraken_api_secret"),
            "anthropic_api_key": secrets_store.status("anthropic_api_key"),
            "dashboard_username": secrets_store.dashboard_username(),
        },
    }


@app.post("/api/settings")
def api_post_settings(patch: SettingsPatch):
    if patch.config:
        cfg.update(patch.config)
    if patch.secrets:
        s = patch.secrets
        if s.kraken_api_key is not None:
            secrets_store.set_secret("kraken_api_key", s.kraken_api_key)
        if s.kraken_api_secret is not None:
            secrets_store.set_secret("kraken_api_secret", s.kraken_api_secret)
        if s.anthropic_api_key is not None:
            secrets_store.set_anthropic_api_key(s.anthropic_api_key)
        if s.dashboard_username is not None:
            secrets_store.set_dashboard_username(s.dashboard_username)
        if s.dashboard_password is not None:
            secrets_store.set_dashboard_password(s.dashboard_password)
    return api_get_settings()


# ---- settings proposals (Claude's settings-review output) ------------------
@app.get("/api/settings/proposals")
def api_list_proposals():
    return db.all_settings_proposals()


@app.post("/api/settings/proposals/{proposal_id}/approve")
def api_approve_proposal(proposal_id: int):
    proposal = db.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    changes = json.loads(proposal["proposed_json"])
    patch: dict[str, Any] = {}
    for dotted, value in changes.items():
        cur = patch
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    cfg.update(patch)
    db.set_proposal_status(proposal_id, "approved")
    db.insert_event("info", f"Applied settings proposal #{proposal_id}")
    return {"ok": True, "applied": changes}


@app.post("/api/settings/proposals/{proposal_id}/reject")
def api_reject_proposal(proposal_id: int):
    db.set_proposal_status(proposal_id, "rejected")
    return {"ok": True}


# ---- backtest ---------------------------------------------------------------
class BacktestRequest(BaseModel):
    pair: Optional[str] = None
    proposed_signals: Optional[dict[str, Any]] = None
    proposed_risk: Optional[dict[str, Any]] = None


@app.post("/api/backtest")
def api_backtest(req: BacktestRequest):
    pair = req.pair or (cfg.get("pairs") or ["XBTUSD"])[0]
    try:
        ohlc = engine.kc.ohlc(pair, 15)[:-1]
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch historical data: {e}")
    current_signals = cfg.get("signals", {})
    current_risk = cfg.get("risk", {})
    proposed_signals = {**current_signals, **(req.proposed_signals or {})}
    proposed_risk = {**current_risk, **(req.proposed_risk or {})}
    return compare_configs(ohlc, current_signals, current_risk, proposed_signals, proposed_risk)


# ---- mode / kill switch ------------------------------------------------------
class ModeRequest(BaseModel):
    mode: str  # "paper" | "live"


@app.post("/api/mode")
def api_set_mode(req: ModeRequest):
    if req.mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    if req.mode == "live":
        key, secret = secrets_store.kraken_credentials()
        if not (key and secret):
            raise HTTPException(400, "Kraken API key/secret must be configured before going live")
    cfg.set("mode", req.mode)
    db.insert_event("alert", f"Mode switched to {req.mode}")
    return {"ok": True, "mode": req.mode}


class KillRequest(BaseModel):
    flatten: bool = False


@app.post("/api/kill")
def api_kill(req: KillRequest):
    engine.kill(flatten=req.flatten)
    return {"ok": True}


@app.post("/api/resume")
def api_resume():
    engine.resume()
    return {"ok": True}


# ---- static dashboard (mounted last so /api/* above takes priority) --------
app.mount("/", StaticFiles(directory=str(config_module.STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
