#!/usr/bin/env bash
# =============================================================
# setup_oracle.sh
# One-command setup for Oracle Cloud Ubuntu 22.04 VM
#
# Run this on your Oracle VM:
#   bash setup_oracle.sh
# =============================================================

set -e  # Exit on any error

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="cryptotrade"
VENV_DIR="$REPO_DIR/venv"
PYTHON_BIN="$VENV_DIR/bin/python"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo ""
echo "=============================================="
echo "  dYdX SMC Bot — Oracle VM Setup"
echo "  Directory: $REPO_DIR"
echo "=============================================="
echo ""

# -----------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip git curl

# -----------------------------------------------------------
# 2. Python virtual environment
# -----------------------------------------------------------
echo "[2/6] Creating Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3.11 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

echo "[3/6] Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r "$REPO_DIR/requirements.txt" -q

# -----------------------------------------------------------
# 3. .env check
# -----------------------------------------------------------
echo "[4/6] Checking .env file..."
if [ ! -f "$REPO_DIR/.env" ]; then
    echo ""
    echo "  ⚠️  No .env file found!"
    echo "  Copy the template and fill in your credentials:"
    echo "    cp $REPO_DIR/.env.example $REPO_DIR/.env"
    echo "    nano $REPO_DIR/.env"
    echo ""
    echo "  Then re-run this script."
    exit 1
fi
echo "  ✅ .env file found."

# -----------------------------------------------------------
# 4. Create logs directory
# -----------------------------------------------------------
mkdir -p "$REPO_DIR/logs"

# -----------------------------------------------------------
# 5. Install systemd service
# -----------------------------------------------------------
echo "[5/6] Installing systemd service..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=dYdX SMC BTC/USD Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN $REPO_DIR/bot.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
# Make sure .env is loaded
EnvironmentFile=$REPO_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# -----------------------------------------------------------
# 6. Start the service
# -----------------------------------------------------------
echo "[6/6] Starting the trading bot service..."
sudo systemctl restart "$SERVICE_NAME"

sleep 3
STATUS=$(systemctl is-active "$SERVICE_NAME")

echo ""
echo "=============================================="
if [ "$STATUS" = "active" ]; then
    echo "  ✅ Bot is RUNNING!"
else
    echo "  ❌ Bot status: $STATUS"
    echo "  Check logs: journalctl -u $SERVICE_NAME -n 50"
fi
echo ""
echo "  Useful commands:"
echo "    View live logs  : journalctl -u $SERVICE_NAME -f"
echo "    Stop the bot    : sudo systemctl stop $SERVICE_NAME"
echo "    Restart the bot : sudo systemctl restart $SERVICE_NAME"
echo "    Bot status      : sudo systemctl status $SERVICE_NAME"
echo "=============================================="
echo ""
