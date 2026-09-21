# uptimebot

Telegram bot that monitors your domains and reports problems. Domains, intervals and checks are all managed from inside the bot.

Checks: DNS, Nameserver, MX, Ping, HTTP, HTTPS, HTTP→HTTPS redirect, SSL validity/expiry, response speed, domain expiry (RDAP).

## Install / update on Ubuntu (one line)

```bash
curl -fsSL https://raw.githubusercontent.com/hosi1996/uptimebot/main/install.sh | sudo bash
```

First run asks for the bot token ([@BotFather](https://t.me/BotFather)) and your numeric Telegram ID ([@userinfobot](https://t.me/userinfobot)). Re-running the same line updates the bot and keeps your data.

Non-interactive: `curl -fsSL .../install.sh | sudo BOT_TOKEN=... ADMIN_IDS=... bash`

The bot uses long polling, so no domain, webhook or open port is needed.

## Checks from Iran (optional)

1. Upload `iran-checker/check.php` to your Iran host (PHP 8+), e.g. `https://hmoazzen.ir/uptimebot/check.php`, and set a long random `TOKEN` inside it.
2. On the server add to `/opt/uptimebot/.env`, then `systemctl restart uptimebot`:
   ```
   IRAN_CHECK_URL=https://hmoazzen.ir/uptimebot/check.php
   IRAN_CHECK_TOKEN=<same token>
   ```
3. In the bot, each domain gets a **📍 محل چک** button: both (default) / foreign only / Iran only. Results are tagged 🌍 / 🇮🇷. Domain expiry always runs from the bot server.

On the Iran side "Ping" is a TCP connect to port 443/80 (shared hosts rarely allow ICMP).

## Useful commands

```bash
systemctl status uptimebot
journalctl -u uptimebot -f
```

Data lives in `/opt/uptimebot/data/uptimebot.db`, config in `/opt/uptimebot/.env`.
