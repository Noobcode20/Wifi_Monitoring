"""Deterministic, in-memory presence lifecycle checks; never sends UDP packets."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import presence_detector as detector


def frame(node, seq, subcarriers, occupied):
    amps = []
    for index in range(subcarriers):
        band = index * detector.FEATURE_BANDS // subcarriers
        value = (14.0 if band % 2 else 6.0) if occupied else 10.0
        amps.append(value)
    return {"node_id": node, "csi_sequence": seq, "frequency_mhz": 2412,
            "antennas": 1, "subcarriers": subcarriers, "ppdu_type": 0,
            "flags": 0, "amplitude": amps}


class PresenceFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 0.0
        self.engine = detector.PresenceEngine(Path(self.tmp.name) / "profile.json",
                                               clock=lambda: self.now, wall_clock=lambda: 1234.0)
        self.seq = {node: 0 for node in detector.EXPECTED_NODES}
        for node in detector.EXPECTED_NODES:
            self.push(node, 0.0, occupied=False)

    def tearDown(self):
        self.tmp.cleanup()

    def push(self, node, now, occupied, subcarriers=None):
        self.now = now
        self.seq[node] += 1
        sc = subcarriers or (64 if self.seq[node] % 2 else 128)
        return self.engine.observe_csi(frame(node, self.seq[node], sc, occupied),
                                       f"192.168.1.{node}", now)

    def capture(self, action, occupied, start):
        self.now = start
        self.engine.start_capture(action, start)
        began = start + detector.START_DELAY_SECONDS
        self.now = began
        self.engine.snapshot(began)
        duration = detector.CAPTURE_SECONDS[action]
        for second in range(int(duration)):
            for sample in range(10):
                stamp = began + second + 0.05 + sample * 0.08
                for node in sorted(detector.EXPECTED_NODES):
                    self.push(node, stamp, occupied, 64 if sample % 2 == 0 else 128)
        self.now = began + duration + 0.1
        return self.engine.snapshot(self.now)

    def test_training_prediction_verification_and_private_persistence(self):
        overrides = {"MIN_FRAMES": {"empty": 30, "occupied": 30},
                     "MIN_LAYOUT_FRAMES": 10, "MIN_WINDOWS": 2,
                     "MIN_VERIFY_WINDOWS": 2, "MIN_WINDOW_FRAMES": 5,
                     "START_DELAY_SECONDS": 1,
                     "CAPTURE_SECONDS": {"empty": 3, "occupied": 3,
                                         "verify_empty": 3, "verify_occupied": 3}}
        with patch.multiple(detector, **overrides):
            empty = self.capture("empty", False, 0.1)
            self.assertEqual(empty["status"], "awaiting_occupied")
            occupied = self.capture("occupied", True, self.now + 0.1)
            self.assertEqual(occupied["status"], "trained_unverified", occupied["error"])
            self.assertFalse(occupied["presence_validated"])
            self.assertEqual(len(occupied["model"]["nodes"]), 4)
            self.assertTrue(all(layout["accepted"] for layout in occupied["model"]["layouts"]))

            profile = Path(self.tmp.name) / "profile.json"
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            restarted = detector.PresenceEngine(profile, clock=lambda: self.now,
                                                 wall_clock=lambda: 1234.0)
            self.assertEqual(restarted.state, "trained_unverified")
            self.engine = restarted
            inference_start = self.now
            for node in sorted(detector.EXPECTED_NODES):
                for index in range(7):
                    self.push(node, inference_start + 0.05 + index * 0.05, occupied=True, subcarriers=64)
            self.now = inference_start + 1.2
            prediction = self.engine.snapshot(self.now)
            self.assertFalse(prediction["presence_validated"])
            self.assertEqual(prediction["presence"], "occupied")

            self.capture("verify_empty", False, self.now + 0.1)
            checked = self.capture("verify_occupied", True, self.now + 0.1)
            self.assertIn(checked["status"], {"room_check_passed", "room_check_failed"})
            self.assertFalse(checked["presence_validated"])
            self.assertEqual(set(checked["model"]["room_checks"]), {"empty", "occupied"})

    def test_duplicate_sequence_and_staleness_fail_closed(self):
        node, stamp = 1, self.now + 0.1
        self.assertFalse(self.engine.observe_csi(frame(node, 1, 64, False), "192.168.1.1", stamp))
        self.now = stamp + detector.FRESH_SECONDS + 0.1
        state = self.engine.snapshot(self.now)
        self.assertEqual(state["presence"], "unknown")

    def test_identical_room_captures_are_rejected_as_unseparated(self):
        overrides = {"MIN_FRAMES": {"empty": 30, "occupied": 30},
                     "MIN_LAYOUT_FRAMES": 10, "MIN_WINDOWS": 2,
                     "MIN_WINDOW_FRAMES": 5, "START_DELAY_SECONDS": 1,
                     "CAPTURE_SECONDS": {"empty": 3, "occupied": 3,
                                         "verify_empty": 3, "verify_occupied": 3}}
        with patch.multiple(detector, **overrides):
            self.assertEqual(self.capture("empty", False, 0.1)["status"], "awaiting_occupied")
            failed = self.capture("occupied", False, self.now + 0.1)
        self.assertEqual(failed["status"], "training_failed")
        self.assertTrue(failed["model"]["training_accuracy_is_not_validation"])
        self.assertTrue(any(reason == "poor_training_separation"
                            for reason in failed["model"]["rejected"].values()))

    def test_training_gate_holds_out_the_chronological_capture_tail(self):
        empty = [[0.0] * detector.FEATURE_BANDS for _ in range(8)]
        occupied = [[0.0] * detector.FEATURE_BANDS for _ in range(8)]
        for row in empty:
            row[0] = 0.0
        for row in occupied:
            row[0] = 1.0
        # The last two seconds of each capture reverse the original separation.
        empty[-2:] = [[1.0] + [0.0] * (detector.FEATURE_BANDS - 1) for _ in range(2)]
        occupied[-2:] = [[0.0] * detector.FEATURE_BANDS for _ in range(2)]
        self.assertEqual(detector._temporal_holdout_accuracy(empty, occupied), 0.0)


if __name__ == "__main__":
    unittest.main()
