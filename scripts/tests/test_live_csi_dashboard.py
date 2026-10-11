"""Offline protocol/freshness checks. Fixtures never reach the live UDP port."""
from collections import Counter, deque
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest


spec = importlib.util.spec_from_file_location(
    "live_csi_dashboard", Path(__file__).resolve().parents[1] / "live-csi-dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


def packet(**changes):
    header = dict(magic=dashboard.CSI_MAGIC, node=1, antennas=1,
                  subcarriers=2, freq=2412, seq=17, rssi=-52,
                  noise=-90, ppdu=0, flags=0)
    header.update(changes)
    return dashboard.HEADER.pack(*header.values()) + bytes([3, 4, 253, 4])


class ProtocolTests(unittest.TestCase):
    def test_signed_iq_and_measured_header(self):
        frame = dashboard.decode_csi(packet())
        self.assertEqual(frame["amplitude"], [5.0, 5.0])
        self.assertEqual(frame["rssi_dbm"], -52)
        self.assertEqual(frame["csi_sequence"], 17)

    def test_malformed_frames_never_become_measurements(self):
        invalid = [b"", packet()[:-1], packet() + b"\0", packet(magic=1),
                   packet(node=5), packet(antennas=0), packet(subcarriers=0),
                   packet(subcarriers=257), packet(freq=2413), packet(rssi=1)]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    dashboard.decode_csi(data)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        # Create only the state; these tests never open a network socket.
        self.receiver = dashboard.Receiver.__new__(dashboard.Receiver)
        self.receiver.lock = threading.Lock()
        self.receiver.nodes = {}
        self.receiver.rejected = Counter()
        self.receiver.auxiliary = Counter()
        self.receiver.started = time.monotonic() - 20
        self.receiver.lan_ip, self.receiver.udp_port = "127.0.0.1", 0
        self.receiver.clock = time.monotonic
        self.receiver.presence_tmp = tempfile.TemporaryDirectory()
        self.receiver.presence = dashboard.PresenceEngine(
            profile_path=Path(self.receiver.presence_tmp.name) / "profile.json")
        self.receiver.presence_ignored_packets = 0

    def tearDown(self):
        self.receiver.presence_tmp.cleanup()

    def add_node(self, seconds_ago):
        stamp = time.monotonic() - seconds_ago
        frame = dashboard.decode_csi(packet())
        frame["signal_change_pct"] = 12.5
        self.receiver.nodes[1] = dict(frame=frame, received_at=stamp,
                                     source_ip="127.0.0.1", packet_count=2,
                                     arrivals=deque([stamp]), duplicate_sequences=0)

    def test_startup_has_no_fabricated_frames(self):
        snapshot = self.receiver.snapshot()
        self.assertEqual(snapshot["status"]["source_state"], "disconnected")
        self.assertEqual(snapshot["latest"]["nodes"], [])
        self.assertIsNone(snapshot["latest"]["measured_signal_change_pct"])

    def test_fresh_packets_never_assert_verified_presence(self):
        self.add_node(0.2)
        snapshot = self.receiver.snapshot()
        self.assertEqual(snapshot["status"]["source_state"], "live_unverified")
        self.assertFalse(snapshot["status"]["presence_validated"])
        self.assertFalse(snapshot["status"]["simulation_enabled"])
        self.assertEqual(len(snapshot["latest"]["nodes"]), 1)
        self.assertEqual(snapshot["presence"]["presence"], "unknown")
        self.assertFalse(snapshot["presence"]["presence_validated"])

    def test_stale_packets_leave_plot_and_current_signal_measurement(self):
        self.add_node(6)
        snapshot = self.receiver.snapshot()
        self.assertEqual(snapshot["status"]["source_state"], "stale")
        self.assertEqual(snapshot["nodes"]["nodes"][0]["csi_status"], "stale")
        self.assertEqual(snapshot["nodes"]["nodes"][0]["packet_rate_hz"], 0)
        self.assertEqual(snapshot["latest"]["nodes"], [])
        self.assertIsNone(snapshot["latest"]["measured_signal_change_pct"])
        self.assertEqual(snapshot["presence"]["presence"], "unknown")


if __name__ == "__main__":
    unittest.main()
