#!/usr/bin/env bash
# Deploy/update the bot on the droplet.
# Usage: DROPLET_HOST=root@your-droplet-ip ./deploy/deploy.sh
set -euo pipefail

: "${DROPLET_HOST:?Set DROPLET_HOST=user@host before running (e.g. root@1.2.3.4)}"
REMOTE_DIR="/opt/kraken-margin-bot"

echo "==> Syncing project to $DROPLET_HOST:$REMOTE_DIR"
rsync -avz --delete \
  --exclude='.venv' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='.git' --exclude='__pycache__' --exclude='config.json' \
  ./ "$DROPLET_HOST:$REMOTE_DIR/"

echo "==> Setting up remote venv and installing dependencies"
ssh "$DROPLET_HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/kraken-margin-bot
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f /etc/kraken-margin-bot.env ]; then
  echo "MASTER_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" > /etc/kraken-margin-bot.env
  chmod 600 /etc/kraken-margin-bot.env
  echo "Generated a new /etc/kraken-margin-bot.env with a fresh MASTER_KEY."
fi

if ! id -u kraken-bot >/dev/null 2>&1; then
  useradd --system --home /opt/kraken-margin-bot --shell /usr/sbin/nologin kraken-bot
fi
chown -R kraken-bot:kraken-bot /opt/kraken-margin-bot

cp deploy/kraken-margin-bot.service /etc/systemd/system/kraken-margin-bot.service
systemctl daemon-reload
systemctl enable kraken-margin-bot
systemctl restart kraken-margin-bot
sleep 2
systemctl status kraken-margin-bot --no-pager
REMOTE

echo "==> Done. Check logs with: ssh $DROPLET_HOST journalctl -u kraken-margin-bot -f"
