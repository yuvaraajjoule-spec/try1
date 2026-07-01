# 📈 dYdX SMC BTC/USD Trading Bot

An automated perpetuals trading bot for [dYdX v4](https://dydx.exchange) that uses **Smart Money Concepts (SMC)** — Break of Structure (BOS), Change of Character (CHoCH), Fair Value Gaps (FVG), and Order Blocks — to generate BTC/USD trade signals and execute them 24/7 on an Oracle Cloud VM.

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
├── .env                 # 🔒 Your secrets (never committed)
├── .env.example         # Template — copy this to .env
├── .gitignore
├── requirements.txt
├── logic.py             # SMC Indicator + generate_signal()
├── dydx_client.py       # dYdX v4 API wrapper
├── risk.py              # Position sizing, SL/TP, loss limits
├── bot.py               # Main async trading loop
├── setup_oracle.sh      # One-command Oracle VM deploy script
└── cryptotrade.service  # systemd service unit
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
nano .env   # Fill in your dYdX mnemonic and settings
```

**Key settings in `.env`:**
```
DYDX_MNEMONIC="your 24 word mnemonic here"
DYDX_NETWORK=mainnet
POSITION_SIZE_USDC=50
LEVERAGE=1
DRY_RUN=true    # ← Start with this! Change to false for real trading
```

### 4. Test with dry run
```bash
python bot.py --dry-run
```

You should see live price output + signals without any real orders.

---

## ☁️ Oracle VM Deployment (24/7)

### Prerequisites
- Oracle Cloud Free Tier account
- Ubuntu 22.04 VM (Ampere A1 — always free)
- SSH access to the VM

### Step 1: SSH into your Oracle VM
```bash
ssh ubuntu@YOUR_VM_IP
```

### Step 2: Clone your repo
```bash
git clone https://github.com/yourusername/cryptotrade.git
cd cryptotrade
```

### Step 3: Create your .env file
```bash
cp .env.example .env
nano .env    # Add your real mnemonic and settings
```

### Step 4: Run the setup script
```bash
bash setup_oracle.sh
```

This will:
- Install Python 3.11
- Create a virtual environment
- Install all dependencies
- Register the bot as a `systemd` service
- Start it automatically

### Step 5: Monitor the bot
```bash
# Live logs
journalctl -u cryptotrade -f

# Bot status
sudo systemctl status cryptotrade

# Restart
sudo systemctl restart cryptotrade

# Stop
sudo systemctl stop cryptotrade
```

---

## 🔒 Security

- **`.env` is gitignored** — your mnemonic is never pushed to GitHub
- The `state.json` and `open_trade.json` files (runtime state) are also gitignored
- Store your mnemonic securely — anyone with it can control your dYdX account

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `DYDX_MNEMONIC` | (required) | Your 24-word dYdX wallet mnemonic |
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
| `DRY_RUN` | `false` | `true` = no real orders |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## ⚠️ Risk Disclaimer

This is an automated trading bot for cryptocurrency perpetuals — a high-risk financial instrument. You can lose your entire balance. Use at your own risk. Start with small position sizes and always test with `DRY_RUN=true` first.

---

## 📜 License

MIT
