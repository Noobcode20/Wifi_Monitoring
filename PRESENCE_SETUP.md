# ESP32 Human Presence Setup

This checkout is configured for four ESP32-S3 nodes (IDs 1–4). They use RuView's
tier-2 CSI firmware. The primary dashboard runs a local measurement receiver
that decodes the firmware's ADR-018 raw CSI packets directly.

## Board and firmware

- Board at setup: ESP32-S3 QFN56 revision 0.2, 16 MB flash, 8 MB PSRAM.
- USB serial at setup: `/dev/cu.usbmodem3101` (confirm with
  `node harness/ruview/bin/cli.js devices`).
- Firmware: RuView 0.8.12, built with ESP-IDF 6.1 and the display-less
  `sdkconfig.defaults.devkitc` overlay. The source's 16 MB defaults file is
  C6-specific, so this S3 build uses its supported 8 MB partition layout on
  the larger flash chip.
- The local firmware image is
  `firmware/esp32-csi-node/build/esp32-csi-node.bin`.

Rebuild and flash this board from the checkout root:

```bash
source /Users/udbhavp/.espressif/tools/activate_idf_v6.1.sh
idf.py -C firmware/esp32-csi-node \
  -D SDKCONFIG_DEFAULTS='sdkconfig.defaults;sdkconfig.defaults.devkitc' \
  build
idf.py -C firmware/esp32-csi-node -p /dev/cu.usbmodem3101 flash
```

## Provision and run

Connect both the Mac and ESP32 to the same 2.4 GHz network. On a phone hotspot,
enable its 2.4 GHz compatibility setting first. Find the Mac's LAN address with
`ipconfig getifaddr <interface>`; the ESP32 sends CSI to that address on UDP
port 5005.

Provision the node. The script prompts for the Wi-Fi password without echoing
it; it stores provisioning state in the user's private config directory, not
this checkout:

```bash
.venv/bin/python firmware/esp32-csi-node/provision.py \
  --port /dev/cu.usbmodem3101 \
  --ssid 'YOUR_2_4_GHZ_SSID' \
  --target-ip YOUR_MAC_LAN_IP \
  --edge-tier 2
```

Start the dashboard from this checkout. Python 3 is its only runtime dependency.
Its primary page shows per-node CSI freshness, sender IP, sequence, measured
packet rate, RSSI, and raw I/Q magnitude:

```bash
./start-presence.sh
```

Open `http://127.0.0.1:3002/` (the earlier `/ui/real-csi.html` URL also works).
The script installs `local.ruview.real-csi` as a macOS login agent with automatic
restart. HTTP is available on the Mac's LAN interface for the paired phone;
UDP binds to the Mac's current LAN address on port 5005 and accepts only
senders from that LAN subnet. The old
`ruview-presence-local` Docker container is preserved, stopped, and configured
not to restart over the receiver. Other containers are unaffected.

If the hotspot or Mac LAN address changes, run `./start-presence.sh` again and
reprovision the nodes' target IP if necessary. All four nodes currently target
`10.84.146.90:5005`.

The receiver is `scripts/live-csi-dashboard.py`; the page is `ui/real-csi.html`.
The CSI dashboard keeps raw CSI samples in memory and stores the latest compact
per-node readings plus a SQLite event journal at
`~/.local/share/ruview/ruview.sqlite3`.
The mobile app's **Alerts** tab reads `/api/v1/events` from this same receiver;
on a phone, set **Settings → Server** to `http://<Mac-LAN-IP>:3002`. Alerts
refresh while the app is open. A new stale-node warning is also shown as an
in-app alert. The app reports receiver unavailability if the Mac cannot be
reached.
Packets must pass magic, exact payload size, node ID, antenna/subcarrier,
2.4 GHz frequency, source-subnet, and RSSI checks before being counted as CSI.
Non-CSI firmware packets are counted separately and never become radio samples.
Raw measurements remain in memory; launch logs are under
`~/.local/state/ruview/`. `/api/v1/status` exposes accepted/rejected counters.

Nodes become stale after five seconds without CSI. Stale nodes leave the live
mean and amplitude plot; an API failure clears previous readings. No simulation
is available in this receiver. `source_state: live_unverified` means valid
hardware-format packets are arriving, with no validated human-presence result.

To stop the receiver:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.ruview.real-csi.plist"
```

For a terminal stream summary instead, run
`node harness/ruview/bin/cli.js esp32 --watch` in this checkout.

## Guided room calibration

Open the primary dashboard after all four boards are sending fresh packets. The
room presence card enables setup only when the receiver has a real CSI source
and all four node links are fresh. Keep the boards spread around the room and
keep both board positions and the Wi-Fi access point fixed for setup and use.
Keep the fan and other usual room equipment in its normal state.

1. Click **Start empty-room capture**, leave the area covered by all four boards,
   and do not re-enter during the 15-second lead-in or the following 60-second
   capture. The dashboard shows remaining time and valid frame counts for each
   node. Each node must supply enough valid CSI frames.
2. When the dashboard enables the next step, click **Start walk-through
   capture**. During the 15-second lead-in, get ready to enter. Walk the normal
   room paths for the first 15 seconds of the 30-second capture; for the final
   15 seconds, sit or stand still near a typical occupied area.
3. Complete the independent empty-room check while the covered room is empty.
   The separate check lasts 20 seconds after its 15-second lead-in.
4. Complete the independent occupied check during a typical walk-through,
   including some quiet sitting or standing. This separate check also lasts
   20 seconds after its 15-second lead-in. Its quiet portion helps show whether
   the signal responds only to movement.

The check summary reports matching, different, and unknown time windows.
Unknown windows count against passing a check. If the four nodes do not provide
separated signals or enough usable frames, the dashboard reports the affected
nodes/layouts. Keep the boards and access point fixed, then repeat the room
captures along the usual paths and occupied areas; do not lower the separation
requirement to force a result. You can cancel a running capture or reset the
saved room profile from the card.

The compact trained profile is saved privately at
`~/.local/state/ruview/presence-profile.json` and is reused after the receiver
restarts. Raw CSI frames and capture windows stay in memory and are not saved.
The firmware's own presence flag and score are shown separately as experimental
diagnostics. Neither its startup baseline nor this room-specific classifier can
identify a human separately from a pet or other moving object. The system does
not report person counts or pose. Passing the two separate room checks is
evidence for those checks in this setup, not a general accuracy guarantee.

CSI packet transport and frame-change percentages remain measured radio data.
Frame change can come from people, Wi-Fi traffic, interference, or other
movement. The room classifier stays unknown when too few trained nodes have
fresh evidence, and the dashboard does not use synthetic input for calibration.

## Offline public-dataset check

`scripts/evaluate_dp_los_images.py` evaluates the public
[Zenodo ESP32 CSI presence dataset](https://zenodo.org/records/8021099) using
its supplied train, validation, and test splits. Download and extract both
`DP_LOS.zip` and `DP_NLOS.zip`, then run:

```bash
python3 scripts/evaluate_dp_los_images.py \
  --los /path/to/DP_LOS \
  --nlos /path/to/DP_NLOS
```

The evaluator requires Pillow and NumPy. The dataset is licensed for
non-commercial research use. Its inputs are 4-second ESP32 CSI spectrogram
images from a different transmitter/receiver setup, not the raw packets from
these four room nodes. On the dataset's small test splits, normalized 16-band
means scored 1.000 balanced accuracy on LOS and 0.844 on NLOS; transferring an
LOS model to NLOS scored 0.391. Each test split contained only six no-presence
samples. These results support room-specific calibration and do not predict
this room's accuracy. The evaluator is isolated from the live receiver and
never writes a calibration profile.
