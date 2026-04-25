#!/bin/bash
# Nodeflow Edge — Installation Script
# Run as root on Raspberry Pi or industrial gateway hardware.
#
# Usage: sudo bash install.sh

set -e

INSTALL_DIR="/opt/nodeflow-edge"
SERVICE_NAME="nodeflow-edge"

echo "======================================"
echo "  Nodeflow Edge — Installer"
echo "======================================"

# 1. Create installation directory
echo "[1/6] Creating installation directory..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/certs"
mkdir -p "$INSTALL_DIR/logs"

# 2. Copy files
echo "[2/6] Copying files..."
cp -r nodeflow_edge "$INSTALL_DIR/"
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

# 4. Install systemd service
echo "[4/6] Installing systemd service..."
cp nodeflow-edge.service /etc/systemd/system/
systemctl daemon-reload

# 5. Enable service
echo "[5/6] Enabling service..."
systemctl enable "$SERVICE_NAME"

# 6. Start service
echo "[6/6] Starting Nodeflow Edge..."
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
