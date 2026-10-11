#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || true)"
LABEL="local.ruview.real-csi"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/.local/state/ruview"
USER_ID="$(id -u)"

if [[ -z "$PYTHON" ]]; then
  echo "Python 3 is required to run the live CSI receiver." >&2
  exit 1
fi
IFACE="$(route -n get default | awk '/interface:/{print $2; exit}')"
HOST_IP="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
NETMASK="$(ipconfig getoption "$IFACE" subnet_mask 2>/dev/null || true)"
if [[ -z "$HOST_IP" || -z "$NETMASK" ]]; then
  echo "Connect this Mac to the ESP32's Wi-Fi network, then retry." >&2
  exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR"
chmod 700 "$STATE_DIR"

# Preserve the old container, release its HTTP/UDP ports, and prevent its
# outdated dashboard from restarting over the measurement receiver.
if command -v docker >/dev/null 2>&1 && docker inspect ruview-presence-local >/dev/null 2>&1; then
  docker update --restart=no ruview-presence-local >/dev/null
  docker stop ruview-presence-local >/dev/null
fi
launchctl bootout "gui/$USER_ID" "$PLIST" >/dev/null 2>&1 || true

"$PYTHON" - "$PLIST" "$PYTHON" "$ROOT" "$HOST_IP" "$NETMASK" "$STATE_DIR" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

plist, python, root, ip, mask, state = sys.argv[1:]
for name in ("receiver.log", "receiver-errors.log"):
    path = Path(state) / name
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
config = {
    "Label": "local.ruview.real-csi",
    "ProgramArguments": [python, str(Path(root) / "scripts/live-csi-dashboard.py"),
                         "--lan-ip", ip, "--allow-cidr", f"{ip}/{mask}"],
    "WorkingDirectory": root,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "StandardOutPath": str(Path(state) / "receiver.log"),
    "StandardErrorPath": str(Path(state) / "receiver-errors.log"),
}
temporary = Path(plist + ".tmp")
temporary.write_bytes(plistlib.dumps(config))
temporary.chmod(0o600)
os.replace(temporary, plist)
PY

launchctl bootstrap "gui/$USER_ID" "$PLIST"
launchctl kickstart "gui/$USER_ID/$LABEL"
for attempt in 1 2 3 4 5; do
  if curl --fail --silent http://127.0.0.1:3002/health >/dev/null; then
    echo "Dashboard: http://127.0.0.1:3002/"
    echo "Mobile companion: set its server URL to http://$HOST_IP:3002/"
    echo "Receiving real ESP32 CSI on $HOST_IP:5005/udp ($HOST_IP/$NETMASK)."
    echo "The receiver starts at login and restarts automatically. No simulation is enabled."
    exit 0
  fi
  sleep 1
done
echo "Receiver did not start. See $STATE_DIR/receiver-errors.log" >&2
exit 1
