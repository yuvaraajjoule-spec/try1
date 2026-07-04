# 📈 dYdX SMC BTC/USD Trading Bot

An automated perpetuals trading bot for [dYdX v4](https://dydx.exchange) that uses **Smart Money Concepts (SMC)** — Break of Structure (BOS), Change of Character (CHoCH), Fair Value Gaps (FVG), and Order Blocks — to generate BTC/USD trade signals and execute them 24/7.

**Deployed on Render.com (free tier) · Kept awake by UptimeRobot**

---

## 🧠 Strategy Overview

| Signal Component | Bullish (BUY) | Bearish (SELL) |
|---|---|---|
| Market Structure | BOS or CHoCH up | BOS or CHoCH down |
| Imbalance | Bullish FVG | Bearish FVG |
| Confirmation | Bullish Order Block | Bearish Order Block |

All three must confluence for a full signal. Two out of three gives a partial signal.

---

## 📁 Project Structure

```
cryptotrade/
├── .env.example         # Template — copy this to .env
├── .gitignore
├── requirements.txt
├── render.yaml          # Render Blueprint (auto-deploy config)
├── logic.py             # SMC Indicator + generate_signal()
├── dydx_client.py       # dYdX v4 API wrapper
├── risk.py              # Position sizing, SL/TP, loss limits
├── config.py            # Live-reloadable config (Telegram-editable)
├── telegram_bot.py      # Telegram control panel
└── bot.py               # Main async trading loop + Flask keep-alive
```

---

## 🚀 Local Setup (Test First!)

### 1. Clone and enter the project
```bash
git clone https://github.com/yourusername/cryptotrade.git
cd cryptotrade
```

### 2. Create a virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure your secrets
```bash
cp .env.example .env
nano .env   # Fill in your dYdX credentials and settings
```

**Key settings in `.env`:**
```
DYDX_PRIVATE_KEY=your_private_key_hex
DYDX_WALLET_ADDRESS=dydx1...
DYDX_NETWORK=mainnet
POSITION_SIZE_USDC=50
LEVERAGE=1
DRY_RUN=true    # ← Start with this! Change to false for real trading
```

### 4. Test with dry run
```bash
python bot.py --dry-run
```

You should see live price output + signals without any real orders, and the Flask server responding at `http://localhost:8080/health`.

---

## ☁️ Render Deployment (24/7 Free Hosting)

> Render's free tier sleeps after **15 minutes of inactivity**.
> We solve this by running a Flask web server and using UptimeRobot to ping it every 10 minutes.

### Step 1 — Push to GitHub

Make sure your project is pushed to a GitHub repository.
```bash
git add .
git commit -m "feat: add Render + UptimeRobot keep-alive"
git push origin main
```

> ⚠️ **Never push your `.env` file.** It is listed in `.gitignore` already.

---

### Step 2 — Create a Render Web Service

1. Go to **[render.com](https://render.com)** and sign up / log in.
2. Click **"New +"** → **"Web Service"**.
3. Connect your **GitHub account** and select the `cryptotrade` repository.
4. Render will auto-detect `render.yaml` and pre-fill the settings.
5. Set the following:
   | Field | Value |
   |---|---|
   | Name | `cryptotrade-bot` |
   | Runtime | `Python 3` |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `python bot.py` |
   | Plan | **Free** |

6. Click **"Advanced"** → **"Add Environment Variable"** and add all secrets from `.env.example`:
   - `DYDX_PRIVATE_KEY` → your private key
   - `DYDX_WALLET_ADDRESS` → your wallet address
   - `TELEGRAM_BOT_TOKEN` → your bot token
   - `TELEGRAM_CHAT_ID` → your chat ID
   - All other variables (copy from `.env.example`)

   > **Do NOT add `PORT`** — Render injects it automatically.

7. Click **"Create Web Service"**. Render will build and deploy.

8. Copy your **public URL** — looks like:
   ```
   https://cryptotrade-bot.onrender.com
   ```

---

### Step 3 — Set Up UptimeRobot (Keep-Alive Ping)

Render free tier sleeps after 15 minutes. UptimeRobot will ping your `/health` endpoint every **10 minutes** to keep it awake.

1. Go to **[uptimerobot.com](https://uptimerobot.com)** and create a free account.
2. Click **"+ Add New Monitor"**.
3. Fill in:
   | Field | Value |
   |---|---|
   | Monitor Type | `HTTP(s)` |
   | Friendly Name | `CryptoTrade Bot` |
   | URL | `https://cryptotrade-bot.onrender.com/health` |
   | Monitoring Interval | **10 minutes** |
4. Click **"Create Monitor"**.

✅ That's it! UptimeRobot will ping `/health` every 10 minutes. The endpoint returns a JSON response with status, uptime, and settings — confirming the bot is alive.

**Example response from `/health`:**
```json
{
  "status": "ok",
  "uptime": "2h 34m 12s",
  "service": "dYdX SMC Trading Bot",
  "network": "mainnet",
  "dry_run": "false"
}
```

---

### Step 4 — Monitor Your Bot

- **Render Logs**: Render Dashboard → your service → **"Logs"** tab
- **Telegram**: Send `/status` to your bot to get a live status report
- **UptimeRobot**: You'll get email alerts if the bot goes offline

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `DYDX_PRIVATE_KEY` | (required) | Your dYdX wallet private key (hex) |
| `DYDX_WALLET_ADDRESS` | (required) | Your dYdX wallet address (dydx1...) |
| `DYDX_NETWORK` | `mainnet` | `mainnet` or `testnet` |
| `TRADE_SYMBOL` | `BTC-USD` | Market to trade |
| `CANDLE_RESOLUTION` | `15MINS` | Candle timeframe |
| `CANDLE_LIMIT` | `100` | Candles to fetch per cycle |
| `POLL_INTERVAL_SECONDS` | `60` | How often the bot runs (seconds) |
| `POSITION_SIZE_USDC` | `50` | USDC collateral per trade |
| `LEVERAGE` | `1` | Leverage multiplier |
| `STOP_LOSS_PCT` | `0.015` | Stop loss (1.5%) |
| `TAKE_PROFIT_PCT` | `0.030` | Take profit (3.0%) |
| `MAX_DAILY_LOSS_USDC` | `100` | Max daily loss before bot pauses |
| `DRY_RUN` | `true` | `true` = no real orders |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PORT` | auto (Render) | HTTP port — **Render sets this automatically** |

---

## 🔒 Security

- **`.env` is gitignored** — your private key is never pushed to GitHub
- All secrets live in **Render's environment variables** panel (encrypted at rest)
- The `state.json` and `open_trade.json` files (runtime state) are also gitignored
- Store your private key securely — anyone with it can control your dYdX account

---

## ⚠️ Risk Disclaimer

This is an automated trading bot for cryptocurrency perpetuals — a high-risk financial instrument. You can lose your entire balance. Use at your own risk. Start with small position sizes and always test with `DRY_RUN=true` first.

---

## 📜 License

MIT
