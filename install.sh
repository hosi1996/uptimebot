#!/usr/bin/env bash
# Install or update uptimebot on Ubuntu/Debian. Safe to re-run.
set -euo pipefail

REPO="https://github.com/hosi1996/uptimebot.git"
DIR="/opt/uptimebot"
SERVICE="uptimebot"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (use sudo)." >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip iputils-ping >/dev/null

echo "==> Fetching code"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --quiet origin
  git -C "$DIR" reset --hard --quiet origin/main
else
  git clone --quiet "$REPO" "$DIR"
fi

echo "==> Installing Python dependencies"
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet --upgrade -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  TOKEN="${BOT_TOKEN:-}"
  ADMINS="${ADMIN_IDS:-}"
  if [ -z "$TOKEN" ] || [ -z "$ADMINS" ]; then
    [ -r /dev/tty ] || { echo "Set BOT_TOKEN and ADMIN_IDS env vars (no terminal to ask)." >&2; exit 1; }
    read -rp "Bot token (from @BotFather): " TOKEN </dev/tty
    read -rp "Your numeric Telegram ID (from @userinfobot): " ADMINS </dev/tty
  fi
  (umask 077; printf 'BOT_TOKEN=%s\nADMIN_IDS=%s\n' "$TOKEN" "$ADMINS" > "$DIR/.env")
fi

echo "==> Configuring service"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Uptime Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -m uptimebot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && echo "==> Done. Bot is running." \
  || { echo "Service failed. Logs: journalctl -u $SERVICE -n 50" >&2; exit 1; }
