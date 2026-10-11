# RuView CSI dashboard

The supported local monitor is served at `http://127.0.0.1:3002/` by `../scripts/live-csi-dashboard.py`. Start or inspect the local setup with `../start-presence.sh` and see `../PRESENCE_SETUP.md` for the receiver and ESP32 details.

## What this setup measures

The direct receiver validates incoming ESP32 CSI packets and displays their node ID, source address, sequence, packet age, measured RSSI, receive rate, and raw I/Q amplitude magnitude. Stale or intermittent node streams remain visible as stale; they are not treated as active presence.

The primary dashboard also has a guided, live-only room calibration: 60 seconds empty, 30 seconds occupied, then separate 20-second empty and occupied checks. It requires all four fresh node links, keeps raw capture data in memory, and stores only a compact room profile in the user's private state directory. A passed check applies to those room checks only. Firmware presence and score remain experimental diagnostics.

The experimental multi-tab UI at `/ui/index.html?experimental=1` can show connection provenance, RSSI history, derived signal variance/band/spectral values, and an illustrative 3D signal visualization when its sensing WebSocket provides data. These derived values are uncalibrated signal features. Any simulated fallback is explicitly labeled by its data-source banner. They do not establish human presence, location, or person count.

## Claims that are not implemented or validated here

The UI lists fall/activity monitoring, intrusion alerts, and multiple-person tracking as potential ideas. This local setup does not implement or validate those capabilities. It also does not provide pose estimation, limb tracking, a trained pose model, or pose zones; pose/demo/training navigation has been removed.

The older Rust and Python backend documentation describes endpoints and modes from other configurations. Those descriptions do not mean those services are running in this local setup. Check `PRESENCE_SETUP.md` for the actual receiver process and port.
