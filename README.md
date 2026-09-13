# Kraken Margin Trading Bot

Continuously-running leveraged margin trading bot for Kraken, gated on Claude
as a second-stage approval/leverage check, controlled from a browser
dashboard. Starts in **paper mode** and stays there until you explicitly flip
the switch.

## Safety notes (read before going live)

- The Kraken API key must have **trade + query permissions only**. Never
  enable withdrawal or funding permissions on it.
- Exposure caps (5-10% per position, 20-30% total) and the 20%
  drawdown circuit breaker are hard-coded in `config.py` (`HARD_LIMITS`) and
  cannot be changed from the dashboard.
- The bot never deploys more than 50% of a pair's own max leverage, regardless
  of what Claude recommends.
- Displayed liquidation prices are an **estimate** (see `bot/risk.py`
  docstring) — treat Kraken's own account margin display as authoritative.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# .venv/bin/pip install -r requirements.txt         # Linux/macOS

# Generate a master key for encrypting secrets at rest, then set it:
python -c "import secrets;print(secrets.token_urlsafe(32))"
export MASTER_KEY=...   # or on Windows: $env:MASTER_KEY="..."

.venv/Scripts/python.exe server.py                 # Windows
# .venv/bin/python server.py                        # Linux/macOS
```

Open `http://localhost:8000`. On first boot, if no dashboard password is set
yet, one is generated and printed to the console log — log in with it, then
change it (and set your Kraken/Anthropic API keys) from the Settings panel.

Run the unit tests: `.venv/Scripts/python.exe -m pytest tests/ -q`

## Deploying to the droplet

1. Point your domain's DNS (e.g. a DuckDNS record) at the droplet's IP.
2. On the droplet, install Caddy (`https://caddyserver.com/docs/install`) and
   copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile` (already set to
   `botnclaude.duckdns.org`) — `systemctl reload caddy`.
3. From your machine: `DROPLET_HOST=root@your-droplet-ip ./deploy/deploy.sh`
   — this syncs the code, creates a venv, generates `/etc/kraken-margin-bot.env`
   with a fresh `MASTER_KEY` on first run, and starts the systemd service.
4. Visit `https://botnclaude.duckdns.org`, log in with the generated password
   from `journalctl -u kraken-margin-bot`, and configure your API keys.
5. Confirm the dashboard shows **PAPER** mode and let it run for a while
   before ever switching to live.

To re-deploy after code changes, just re-run `deploy/deploy.sh` — it restarts
the service. `config.json` and the SQLite database are excluded from the sync
so they're never overwritten by a deploy.

## Architecture

See `bot/` for the trading logic (signals, risk, cost filter, Claude gate,
broker, reconciler, engine), `server.py` for the API/dashboard, and
`config.py`/`secrets_store.py`/`db.py` for configuration, encrypted secrets,
and persistence.
