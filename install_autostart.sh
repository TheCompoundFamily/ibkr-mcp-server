#!/bin/bash
# =============================================================
# IBKR Bridge — Auto-start installer for macOS
# The Compound Family
#
# This script sets up ibkr_bridge.py to start automatically
# every time your Mac boots, using macOS LaunchAgent.
#
# Run once:  bash ~/ibkr-mcp-server/install_autostart.sh
# =============================================================

set -e

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.thecompoundfamily.ibkr-bridge.plist"
LOG_DIR="$HOME/Library/Logs/TCF"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  IBKR Bridge — Auto-start Setup                 ║"
echo "║  The Compound Family                             ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Check venv exists
if [ ! -f "$BRIDGE_DIR/venv/bin/python3" ]; then
  echo "❌  Virtual environment not found at $BRIDGE_DIR/venv"
  echo "    Please run: cd $BRIDGE_DIR && python3 -m venv venv && source venv/bin/activate && pip install -e ."
  exit 1
fi

# Create log directory
mkdir -p "$LOG_DIR"

# Write LaunchAgent plist
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.thecompoundfamily.ibkr-bridge</string>

  <key>ProgramArguments</key>
  <array>
    <string>$BRIDGE_DIR/venv/bin/python3</string>
    <string>$BRIDGE_DIR/ibkr_bridge.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$BRIDGE_DIR</string>

  <!-- Start automatically on login -->
  <key>RunAtLoad</key>
  <true/>

  <!-- Restart if it crashes -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <!-- Wait 10 sec after boot (let TWS connect first) -->
  <key>StartInterval</key>
  <integer>0</integer>

  <key>ThrottleInterval</key>
  <integer>30</integer>

  <!-- Logs -->
  <key>StandardOutPath</key>
  <string>$LOG_DIR/ibkr-bridge.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/ibkr-bridge-error.log</string>

  <!-- Only run when user is logged in -->
  <key>SessionCreate</key>
  <true/>
</dict>
</plist>
EOF

# Load it now (no reboot needed)
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "✅  Auto-start installed!"
echo ""
echo "The IBKR Bridge will now:"
echo "  • Start automatically every time your Mac boots"
echo "  • Restart automatically if it crashes"
echo "  • Listen on http://localhost:7499"
echo ""
echo "Logs: $LOG_DIR/ibkr-bridge.log"
echo ""
echo "To check status:   curl http://localhost:7499/health"
echo "To stop:           launchctl unload $PLIST"
echo "To uninstall:      launchctl unload $PLIST && rm $PLIST"
echo ""
