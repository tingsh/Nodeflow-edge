#!/bin/bash
# Novena Gateway — Installation Script
# Run as root on Raspberry Pi or industrial gateway hardware.
#
# Usage: sudo bash install.sh

set -e

INSTALL_DIR="/opt/novena-gateway"
SERVICE_NAME="novena-gateway"

echo "======================================"
echo "  Novena Gateway — Installer"
echo "======================================"

# 1. Create installation directory
echo "[1/6] Creating installation directory..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/certs"
mkdir -p "$INSTALL_DIR/logs"

# 2. Copy files
echo "[2/6] Copying files..."
cp -r novena_gateway "$INSTALL_DIR/"
cp config.json "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"

# Copy CA certificate if present in installer bundle
if [ -f "certs/ca.crt" ]; then
    cp certs/ca.crt "$INSTALL_DIR/certs/"
    echo "  → CA certificate installed."
fi

# 3. Create virtual environment
echo "[3/6] Setting up Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# 3.5 Create unprivileged user and assign permissions
echo "[3.5/6] Creating unprivileged user..."
useradd -r -s /bin/false -G dialout novena || true
chown -R novena:novena "$INSTALL_DIR"

# 3.9 Pre-flight configuration validation check
echo "[3.9/6] Running configuration validation check..."
if "$INSTALL_DIR/venv/bin/python" -m novena_gateway.main --config "$INSTALL_DIR/config.json" --validate-only; then
    echo "  ✓ Configuration file is valid."
else
    echo ""
    echo "  ⚠ WARNING: Configuration validation failed!"
    echo "  Novena Gateway may fail to run or connect until configured."
    echo "  Please inspect and edit $INSTALL_DIR/config.json."
    echo ""
fi

# 4. Install systemd service
echo "[4/6] Installing systemd service..."
cp novena-gateway.service /etc/systemd/system/
systemctl daemon-reload

# 5. Enable service
echo "[5/6] Enabling service..."
systemctl enable "$SERVICE_NAME"

# 6. Start service
echo "[6/6] Starting Novena Gateway..."
systemctl start "$SERVICE_NAME"

echo ""
echo "======================================"
echo "  Installation complete!"
echo "======================================"
echo ""
echo "  Status:  sudo systemctl status $SERVICE_NAME"
echo "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "  Config:  $INSTALL_DIR/config.json"
echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
echo "  Restart: sudo systemctl restart $SERVICE_NAME"
echo ""
