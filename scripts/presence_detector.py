"""Live-only, room-specific presence training for the ESP32 CSI receiver.

The profile contains centroids and noise scales only. Raw CSI and capture
windows stay in memory. Training scores describe separation of labeled samples;
they never assert ground-truth validation.
"""

from collections import defaultdict
import ipaddress
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

PROFILE_VERSION = 1
FEATURE_BANDS = 16
EXPECTED_NODES = {1, 2, 3, 4}
MIN_FRAMES = {"empty": 600, "occupied": 300}
MIN_LAYOUT_FRAMES = 50
MIN_WINDOWS = 10
MIN_WINDOW_FRAMES = 5
MIN_VERIFY_WINDOWS = 10
MIN_TRAINING_ACCURACY = 0.80
MIN_ROOM_NODES = 2
CAPTURE_SECONDS = {"empty": 60, "occupied": 30, "verify_empty": 20, "verify_occupied": 20}
START_DELAY_SECONDS = 15
FRESH_SECONDS = 5


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _layout_tuple(frame):
    return (int(frame["frequency_mhz"]), int(frame["antennas"]), int(frame["subcarriers"]),
            int(frame["ppdu_type"]), int(frame["flags"]) & 0x5)


def _layout_key(layout):
    return ":".join(str(int(value)) for value in layout)


def _valid_layout(key):
    try:
        parts = [int(part) for part in key.split(":")]
    except (AttributeError, ValueError):
        return False
    return (len(parts) == 5 and 2412 <= parts[0] <= 2484 and (parts[0] == 2484 or (parts[0] - 2412) % 5 == 0)
            and 1 <= parts[1] <= 4 and 1 <= parts[2] <= 256 and 0 <= parts[3] <= 255
            and parts[4] in {0, 1, 4, 5})


def _frame_features(frame):
    """Return fixed positional normalized band amplitudes, or None if unusable."""
    amps = frame["amplitude"]
    antennas, subcarriers = frame["antennas"], frame["subcarriers"]
    if len(amps) != antennas * subcarriers:
        return None
    bins = [[] for _ in range(FEATURE_BANDS)]
    valid = []
    total = antennas * subcarriers
    for ant in range(antennas):
        for sc in range(2, subcarriers):  # omit the first two firmware-invalid CSI bins
            index = ant * subcarriers + sc
            amp = amps[index]
            if _finite(amp) and amp > 0:
                valid.append(float(amp))
                bins[index * FEATURE_BANDS // total].append(float(amp))
    if len(valid) < FEATURE_BANDS or any(not band for band in bins):
        return None
    rms = math.sqrt(sum(value * value for value in valid) / len(valid))
    if not math.isfinite(rms) or rms <= 1e-9:
        return None
    result = [sum(band) / len(band) / rms for band in bins]
    return result if all(math.isfinite(value) for value in result) else None


def decode_vitals(data):
    """Validate and decode exactly one firmware 32-byte vitals packet."""
    import struct
    wire = struct.Struct("<IBBHIbBHffII")
    if len(data) != wire.size:
        raise ValueError("vitals_length")
    magic, node, flags, breathing, heart, rssi, persons, reserved, motion, score, timestamp, tail = wire.unpack(data)
    if magic != 0xC5110002:
        raise ValueError("vitals_magic")
    if node not in EXPECTED_NODES:
        raise ValueError("vitals_node")
    if flags & ~0x07:
        raise ValueError("vitals_flags")
    if persons > 4:
        raise ValueError("vitals_person_count")
    if not flags & 0x01 and persons != 0:
        raise ValueError("vitals_absent_person_count")
    if not -127 <= rssi <= -1:
        raise ValueError("vitals_rssi")
    if reserved or tail:
        raise ValueError("vitals_reserved")
    if breathing > 12000 or heart > 3000000:
        raise ValueError("vitals_rate")
    if not (_finite(motion) and 0 <= motion <= 1_000_000 and _finite(score) and 0 <= score <= 1_000_000):
        raise ValueError("vitals_score")
    return {"node_id": node, "flags": flags, "presence_flag": bool(flags & 1),
            "motion_flag": bool(flags & 4), "presence_score": float(score), "motion_energy": float(motion),
            "person_count": persons, "rssi_dbm": rssi, "breathing_bpm": breathing / 100,
            "heart_bpm": heart / 10000, "timestamp_ms": timestamp}


def _mean(rows):
    return [sum(row[i] for row in rows) / len(rows) for i in range(FEATURE_BANDS)]


def _scale(empty_rows, occupied_rows):
    rows = empty_rows + occupied_rows
    center = _mean(rows)
    std = [math.sqrt(sum((row[i] - center[i]) ** 2 for row in rows) / max(1, len(rows) - 1))
           for i in range(len(center))]
    floor = max(0.025, sum(std) / FEATURE_BANDS * 0.1)
    return [max(value, floor) for value in std]


def _distance(row, center, scale):
    return sum(((row[i] - center[i]) / scale[i]) ** 2 for i in range(len(center))) / len(center)


def _distance_quantile(rows, center, scale):
    values = sorted(_distance(row, center, scale) for row in rows)
    return max(0.02, values[min(len(values) - 1, math.ceil(len(values) * 0.95) - 1)])


def _temporal_holdout_accuracy(empty_rows, occupied_rows):
    """Balanced accuracy on the final 20% of each capture's ordered windows.

    Windows are appended in capture-time order. Holding out each capture's tail
    avoids the optimistic leakage from randomly/individually withholding highly
    correlated adjacent windows. This is still an in-capture diagnostic, not
    independent room validation.
    """
    if len(empty_rows) < 2 or len(occupied_rows) < 2:
        return 0.0
    empty_split = max(1, min(len(empty_rows) - 1, int(len(empty_rows) * 0.8)))
    occupied_split = max(1, min(len(occupied_rows) - 1, int(len(occupied_rows) * 0.8)))
    # Leave one window between train and test so adjacent windows do not sit on
    # opposite sides of the blocked boundary.
    train_empty, test_empty = empty_rows[:max(1, empty_split - 1)], empty_rows[empty_split:]
    train_occupied, test_occupied = occupied_rows[:max(1, occupied_split - 1)], occupied_rows[occupied_split:]
    if not test_empty or not test_occupied:
        return 0.0
    empty_center, occupied_center = _mean(train_empty), _mean(train_occupied)
    scale = _scale(train_empty, train_occupied)
    correct = {"empty": 0, "occupied": 0}
    for label, rows in (("empty", test_empty), ("occupied", test_occupied)):
        for row in rows:
            guess = "empty" if _distance(row, empty_center, scale) <= _distance(row, occupied_center, scale) else "occupied"
            correct[label] += guess == label
    return (correct["empty"] / len(test_empty) + correct["occupied"] / len(test_occupied)) / 2


class PresenceEngine:
    """Thread-safe capture state machine, PHY-specific classifier and diagnostics."""

    def __init__(self, profile_path=None, clock=time.monotonic, wall_clock=time.time):
        self.lock = threading.RLock()
        self.clock, self.wall_clock = clock, wall_clock
        self.profile_path = Path(profile_path or Path.home() / ".local/state/ruview/presence-profile.json")
        self.profile = self._load_profile()
        checks = self.profile.get("room_checks", {}) if self.profile else {}
        self.state = ("room_check_passed" if set(checks) == {"empty", "occupied"} and all(checks[k]["accuracy"] >= 0.80 and checks[k].get("known_windows", 0) >= MIN_VERIFY_WINDOWS for k in checks)
                      else "room_check_failed" if set(checks) == {"empty", "occupied"}
                      else "room_check_partial" if checks else "trained_unverified" if self.profile else "not_trained")
        self.error = None
        self.capture = None
        self.training_buffer = None
        self.sources = {}
        self.current = {}
        # Every PHY layout gets its own (node, layout, second) nonoverlapping window.
        self.window_second = {}
        self.window_values = defaultdict(list)
        self.window_frames = defaultdict(int)
        self.window_last = {}
        self.edge = {}
        self.last_csi_sequence = {}
        self.last_csi_seen = {}
        self.last_vitals_timestamp = {}
        self.last_vitals_seen = {}
        self.last_layout_seen = {}
        self.last_rejected = {}
        self.last_capture_counts = {}

    def _load_profile(self):
        try:
            if not self.profile_path.exists() or self.profile_path.stat().st_size > 65536:
                return None
            if self.profile_path.stat().st_mode & 0o077:
                return None
            raw = json.loads(self.profile_path.read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            models, sources = raw.get("models"), raw.get("sources")
            if raw.get("version") != PROFILE_VERSION or not isinstance(models, dict) or len(models) > 32 or not isinstance(sources, dict):
                return None
            checked = {}
            for key, model in models.items():
                node, sep, layout = key.partition("|")
                if not sep or not node.isdigit() or int(node) not in EXPECTED_NODES or not _valid_layout(layout) or not isinstance(model, dict):
                    return None
                for field in ("empty", "occupied"):
                    values = model.get(field)
                    if not isinstance(values, list) or len(values) != FEATURE_BANDS or not all(_finite(v) and 0 < float(v) < 100 for v in values):
                        return None
                values = model.get("scale")
                if not isinstance(values, list) or len(values) != FEATURE_BANDS or not all(_finite(v) and 0 < float(v) < 100 for v in values):
                    return None
                accuracy = model.get("training_accuracy")
                if not _finite(accuracy) or not MIN_TRAINING_ACCURACY <= accuracy <= 1:
                    return None
                for field in ("training_frames", "training_windows"):
                    if not isinstance(model.get(field), dict):
                        return None
                for field in ("empty_radius", "occupied_radius"):
                    if not _finite(model.get(field)) or not 0 < model[field] < 1_000_000:
                        return None
                checked[key] = model
            if len({key.split("|", 1)[0] for key in checked}) < MIN_ROOM_NODES:
                return None
            checked_sources = {}
            for key, source in sources.items():
                if key not in {"1", "2", "3", "4"} or not isinstance(source, str) or len(source) > 64:
                    return None
                ipaddress.ip_address(source)
                checked_sources[key] = source
            if set(checked_sources) != {"1", "2", "3", "4"}:
                return None
            room_checks = raw.get("room_checks", {})
            if not isinstance(room_checks, dict) or set(room_checks) - {"empty", "occupied"}:
                return None
            for check in room_checks.values():
                if (not isinstance(check, dict) or not _finite(check.get("accuracy")) or not 0 <= check["accuracy"] <= 1
                        or not _finite(check.get("correct_rate")) or not 0 <= check["correct_rate"] <= 1
                        or not _finite(check.get("incorrect_rate")) or not 0 <= check["incorrect_rate"] <= 1
                        or not _finite(check.get("unknown_rate")) or not 0 <= check["unknown_rate"] <= 1
                        or not _finite(check.get("completed_at"))):
                    return None
                if (not isinstance(check.get("windows"), int) or not 0 <= check["windows"] <= 20
                        or not isinstance(check.get("known_windows"), int) or not 0 <= check["known_windows"] <= check["windows"]):
                    return None
                outcomes = check.get("outcomes")
                if not isinstance(outcomes, dict) or set(outcomes) != {"correct", "incorrect", "unknown"}:
                    return None
                if any(not isinstance(count, int) or count < 0 for count in outcomes.values()) or sum(outcomes.values()) != check["windows"]:
                    return None
            training = raw.get("training", {})
            rejection = training.get("rejected", {}) if isinstance(training, dict) else None
            if (not isinstance(training, dict) or not _finite(training.get("completed_at"))
                    or not isinstance(rejection, dict) or len(rejection) > 64
                    or any(not isinstance(k, str) or len(k) > 48 or not isinstance(v, str) or len(v) > 96
                           for k, v in rejection.items())):
                return None
            return {"version": PROFILE_VERSION, "models": checked, "sources": checked_sources,
                    "room_checks": room_checks, "training": training}
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save_profile(self):
        self.profile_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.profile_path.parent, 0o700)
        data = json.dumps(self.profile, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > 65536:
            raise ValueError("presence profile exceeds size limit")
        fd, temp = tempfile.mkstemp(prefix=".presence-profile-", dir=self.profile_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.profile_path)
            os.chmod(self.profile_path, 0o600)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass

    def _start_phase_locked(self, phase, capture_at):
        cap = self.capture
        cap.update(phase=phase, began_at=capture_at, ends_at=capture_at + CAPTURE_SECONDS[phase],
                   windows={}, frame_totals={node: 0 for node in EXPECTED_NODES},
                   layout_frames={node: {} for node in EXPECTED_NODES},
                   invalid_frames={node: 0 for node in EXPECTED_NODES})
        self.window_second.clear()
        self.window_values.clear()
        self.window_frames.clear()
        self.window_last.clear()

    def _close_window_locked(self, key, now):
        node, layout, second = key
        count = self.window_frames.pop(key, 0)
        rows = self.window_values.pop(key, [])
        last_sample = self.window_last.pop(key, now)
        if count < MIN_WINDOW_FRAMES or not rows:
            return
        vector = _mean(rows)
        self.current[(node, layout)] = {"feature": vector, "layout": layout,
                                        "window_frames": count, "updated_at": last_sample}
        cap = self.capture
        if cap and cap["phase"] in {"empty", "occupied", "verify_empty", "verify_occupied"}:
            cap["windows"].setdefault(node, {}).setdefault(layout, []).append(vector)
            if cap["phase"].startswith("verify"):
                pred = self._predict_locked(node, layout, vector)
                offset = max(0, int(second - math.floor(cap["began_at"])))
                cap["verification_windows"].setdefault(offset, {}).setdefault(node, {})[layout] = pred["label"]

    def _advance_locked(self, now):
        cap = self.capture
        if cap and cap["phase"].startswith("arming_") and now >= cap["capture_at"]:
            phase = cap["phase"].removeprefix("arming_")
            self._start_phase_locked(phase, cap["capture_at"])
        cap = self.capture
        expired = [key for key in list(self.window_frames)
                   if now >= key[2] + 1 or (cap and cap["phase"] in CAPTURE_SECONDS and now >= cap["ends_at"])]
        for key in expired:
            self._close_window_locked(key, now)
            active = (key[0], key[1])
            if self.window_second.get(active) == key[2]:
                self.window_second.pop(active, None)
        if self.capture and self.capture["phase"] in CAPTURE_SECONDS and now >= self.capture["ends_at"]:
            self._finish_capture_locked()

    def observe_vitals(self, packet, source_ip, now):
        with self.lock:
            self._advance_locked(now)
            node, timestamp = packet["node_id"], packet["timestamp_ms"]
            source = str(source_ip)
            if self.profile and source != self.profile["sources"].get(str(node)):
                return False
            if node in self.sources and source != self.sources[node]:
                return False
            if now - self.last_vitals_seen.get(node, now) > FRESH_SECONDS:
                self.last_vitals_timestamp.pop(node, None)
            prev = self.last_vitals_timestamp.get(node)
            if prev is not None:
                delta = (timestamp - prev) & 0xFFFFFFFF
                if delta == 0 or delta >= 0x80000000:
                    return False
            self.last_vitals_timestamp[node] = timestamp
            self.last_vitals_seen[node] = now
            self.edge[node] = dict(packet, source_ip=source, received_at=now)
            return True

    def observe_csi(self, frame, source_ip, now):
        node = frame["node_id"]
        feature = _frame_features(frame)
        layout = _layout_key(_layout_tuple(frame))
        with self.lock:
            self._advance_locked(now)
            seq = frame["csi_sequence"]
            if now - self.last_csi_seen.get(node, now) > FRESH_SECONDS:
                self.last_csi_sequence.pop(node, None)
            prev = self.last_csi_sequence.get(node)
            if prev is not None:
                delta = (seq - prev) & 0xFFFFFFFF
                if delta == 0 or delta >= 0x80000000:
                    return False
            self.last_csi_sequence[node] = seq
            self.last_csi_seen[node] = now
            source = str(source_ip)
            if self.profile and source != self.profile["sources"].get(str(node)):
                self.current.pop((node, layout), None)
                if self.capture:
                    self.state, self.error, self.capture = "capture_failed", f"Node {node} sender IP changed during capture.", None
                return False
            cap = self.capture
            if cap and cap["phase"] in CAPTURE_SECONDS:
                captured_source = cap["sources"].get(node)
                if captured_source is None:
                    cap["sources"][node] = source
                elif captured_source != source:
                    cap["source_changes"].add(node)
                    self.state = "capture_failed"
                    self.error = f"Node {node} sender IP changed during capture; reset and repeat it."
                    self.capture = None
                    return False
            self.sources[node] = source
            self.last_layout_seen[(node, layout)] = now
            if feature is None:
                if cap and cap["phase"] in CAPTURE_SECONDS:
                    cap["invalid_frames"][node] += 1
                return False
            cap = self.capture
            if cap and cap["phase"] in CAPTURE_SECONDS:
                cap["frame_totals"][node] += 1
                counts = cap["layout_frames"][node]
                counts[layout] = counts.get(layout, 0) + 1
            second = math.floor(now)
            key = (node, layout, second)
            active_key = (node, layout)
            previous_second = self.window_second.get(active_key)
            if previous_second is not None and previous_second != second:
                old_key = (node, layout, previous_second)
                self._close_window_locked(old_key, now)
            self.window_second[active_key] = second
            self.window_values[key].append(feature)
            self.window_frames[key] += 1
            self.window_last[key] = now
            self._advance_locked(now)
            return True

    def _fit_locked(self):
        cap = self.capture
        models, rejected = {}, {}
        for node in sorted(EXPECTED_NODES):
            if cap["frame_totals"]["empty"][node] < MIN_FRAMES["empty"] or cap["frame_totals"]["occupied"][node] < MIN_FRAMES["occupied"]:
                rejected[str(node)] = "insufficient_node_frames"
                continue
            for layout in set(cap["training_empty"].get(node, {})) | set(cap["training_occupied"].get(node, {})):
                e = cap["training_empty"].get(node, {}).get(layout, [])
                o = cap["training_occupied"].get(node, {}).get(layout, [])
                empty_count = cap["empty_layout_frames"].get(node, {}).get(layout, 0)
                occupied_count = cap["occupied_layout_frames"].get(node, {}).get(layout, 0)
                model_key = f"{node}|{layout}"
                if min(len(e), len(o)) < MIN_WINDOWS or min(empty_count, occupied_count) < MIN_LAYOUT_FRAMES:
                    rejected[model_key] = "insufficient_layout_samples"
                    continue
                accuracy = _temporal_holdout_accuracy(e, o)
                if accuracy < MIN_TRAINING_ACCURACY:
                    rejected[model_key] = "poor_training_separation"
                    continue
                models[model_key] = {"empty": _mean(e), "occupied": _mean(o), "scale": _scale(e, o),
                                     "training_accuracy": accuracy,
                                     "training_accuracy_method": "chronological_tail_20_percent_one_window_embargo",
                                     "training_frames": {"empty": empty_count, "occupied": occupied_count},
                                     "training_windows": {"empty": len(e), "occupied": len(o)},
                                     "empty_radius": _distance_quantile(e, _mean(e), _scale(e, o)),
                                     "occupied_radius": _distance_quantile(o, _mean(o), _scale(e, o))}
        usable = {key.split("|", 1)[0] for key in models}
        for node in sorted(EXPECTED_NODES):
            if not any(key.startswith(f"{node}|") for key in models) and str(node) not in rejected:
                rejected[str(node)] = "no_usable_layout_model"
        self.last_rejected = rejected
        if len(usable) < MIN_ROOM_NODES:
            self.state = "training_failed"
            self.error = "Training met frame coverage but fewer than two nodes separated empty from occupied. Check rejected layouts and repeat both captures."
            self.capture = None
            return
        self.profile = {"version": PROFILE_VERSION, "models": models,
                        "sources": {str(n): ip for n, ip in cap["sources"].items()},
                        "room_checks": {}, "training": {"completed_at": self.wall_clock(), "rejected": rejected}}
        self._save_profile()
        self.state, self.error, self.capture = "trained_unverified", None, None

    def _finish_capture_locked(self):
        cap = self.capture
        if not cap:
            return
        phase = cap["phase"]
        if phase in {"empty", "occupied"}:
            missing = [n for n in sorted(EXPECTED_NODES) if cap["frame_totals"][n] < MIN_FRAMES[phase]]
            if missing:
                self.state = "training_failed"
                self.error = f"{phase.title()} capture lacked the required valid CSI frames for node(s): {', '.join(map(str, missing))}."
                self.last_capture_counts = dict(cap["frame_totals"])
                self.last_rejected = {str(node): f"insufficient_{phase}_frames" for node in missing}
                self.capture = None
                self.training_buffer = None
                return
            if phase == "empty":
                self.training_buffer = {"training_empty": cap["windows"],
                                        "empty_layout_frames": cap["layout_frames"],
                                        "empty_frames": cap["frame_totals"], "sources": cap["sources"]}
                self.capture = None
                self.state = "awaiting_occupied"
                return
            buf = self.training_buffer or {}
            cap["training_empty"] = buf.get("training_empty", {})
            cap["training_occupied"] = cap["windows"]
            cap["empty_layout_frames"] = buf.get("empty_layout_frames", {})
            cap["frame_totals"] = {"empty": buf.get("empty_frames", {}), "occupied": cap["frame_totals"]}
            cap["occupied_layout_frames"] = cap["layout_frames"]
            if any(cap["sources"].get(n) != buf.get("sources", {}).get(n) for n in EXPECTED_NODES):
                self.state, self.error, self.capture = "training_failed", "Node sender addresses changed between empty and occupied captures; repeat both.", None
                return
            self._fit_locked()
            return
        if phase.startswith("verify_"):
            expected = phase.removeprefix("verify_")
            room_counts = {"correct": 0, "incorrect": 0, "unknown": 0}
            duration = CAPTURE_SECONDS[phase]
            for second in range(duration):
                predictions = []
                grouped = cap["verification_windows"].get(second, {})
                for node in sorted(EXPECTED_NODES):
                    labels = list(grouped.get(node, {}).values())
                    if labels:
                        known = [label for label in labels if label in {"empty", "occupied"}]
                        if known and known.count("empty") != known.count("occupied"):
                            node_label = "empty" if known.count("empty") > known.count("occupied") else "occupied"
                            predictions.append(node_label)
                        else:
                            predictions.append("unknown")
                if len([p for p in predictions if p != "unknown"]) < MIN_ROOM_NODES:
                    observed = "unknown"
                elif predictions.count("empty") == predictions.count("occupied"):
                    observed = "unknown"
                else:
                    observed = "empty" if predictions.count("empty") > predictions.count("occupied") else "occupied"
                kind = "unknown" if observed == "unknown" else "correct" if observed == expected else "incorrect"
                room_counts[kind] += 1
            total = sum(room_counts.values())
            known_total = room_counts["correct"] + room_counts["incorrect"]
            accuracy = room_counts["correct"] / total if total else 0.0
            checks = self.profile.setdefault("room_checks", {})
            checks[expected] = {"completed_at": self.wall_clock(), "accuracy": accuracy,
                                "correct_rate": room_counts["correct"] / total if total else 0.0,
                                "incorrect_rate": room_counts["incorrect"] / total if total else 0.0,
                                "unknown_rate": room_counts["unknown"] / total if total else 1.0,
                                "outcomes": room_counts, "windows": total, "known_windows": known_total}
            self.capture = None
            if set(checks) == {"empty", "occupied"}:
                passed = all(checks[label]["accuracy"] >= 0.80 and checks[label]["known_windows"] >= MIN_VERIFY_WINDOWS
                             for label in ("empty", "occupied"))
                self.state = "room_check_passed" if passed else "room_check_failed"
                self.profile["room_check_passed"] = passed
            else:
                self.state = "room_check_partial"
            self._save_profile()

    def start_capture(self, action, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            self._advance_locked(now)
            if action == "cancel":
                self.capture = None
                self.training_buffer = None
                checks = self.profile.get("room_checks", {}) if self.profile else {}
                self.state = ("room_check_passed" if set(checks) == {"empty", "occupied"} and all(checks[k]["accuracy"] >= 0.80 and checks[k].get("known_windows", 0) >= MIN_VERIFY_WINDOWS for k in checks)
                              else "room_check_failed" if set(checks) == {"empty", "occupied"}
                              else "room_check_partial" if checks else "trained_unverified" if self.profile else "not_trained")
                self.error = "Capture cancelled."
                return self._status_locked(now)
            if action not in {"empty", "occupied", "verify_empty", "verify_occupied"}:
                raise ValueError("unsupported capture action")
            if self.capture:
                raise ValueError("a capture is already active")
            if action == "occupied" and (self.state != "awaiting_occupied" or not self.training_buffer):
                raise ValueError("complete the empty-room capture first")
            if action.startswith("verify_") and not self.profile:
                raise ValueError("train a room profile before verification")
            missing = [node for node in sorted(EXPECTED_NODES)
                       if now - self.last_csi_seen.get(node, float("-inf")) >= FRESH_SECONDS]
            if missing:
                raise ValueError("all four ESP32 nodes must be sending fresh CSI before capture starts")
            if action == "empty":
                self.profile = None
                self.training_buffer = None
                try:
                    self.profile_path.unlink()
                except FileNotFoundError:
                    pass
            phase = "arming_" + action
            sources = dict(self.sources) if action == "empty" else {}
            if action.startswith("verify_"):
                sources = {int(n): ip for n, ip in self.profile["sources"].items()}
            elif action == "occupied" and self.training_buffer:
                sources = dict(self.training_buffer["sources"])
            self.capture = {"phase": phase, "capture_at": now + START_DELAY_SECONDS,
                            "sources": sources, "source_changes": set(), "verification_windows": {}}
            self.state, self.error = phase, None
            return self._status_locked(now)

    def _predict_locked(self, node, layout, vector):
        if not self.profile:
            return {"label": "unknown", "reason": "not_trained", "confidence": None}
        if self.sources.get(node) != self.profile["sources"].get(str(node)):
            return {"label": "unknown", "reason": "source_changed", "confidence": None}
        model = self.profile["models"].get(f"{node}|{layout}")
        if not model:
            return {"label": "unknown", "reason": "untrained_layout", "confidence": None}
        empty_d = _distance(vector, model["empty"], model["scale"])
        occupied_d = _distance(vector, model["occupied"], model["scale"])
        confidence = abs(empty_d - occupied_d) / max(empty_d + occupied_d, 1e-9)
        nearest = min(empty_d, occupied_d)
        radius = model["empty_radius"] if empty_d <= occupied_d else model["occupied_radius"]
        if nearest > max(0.05, radius) * 2.5:
            return {"label": "unknown", "reason": "outside_training_range", "confidence": confidence}
        if confidence < 0.12:
            return {"label": "unknown", "reason": "low_confidence", "confidence": confidence}
        return {"label": "empty" if empty_d < occupied_d else "occupied", "reason": None, "confidence": confidence}

    def _status_locked(self, now):
        nodes, votes = [], []
        for node in sorted(EXPECTED_NODES):
            active_layouts = [layout for (node_id, layout), seen in self.last_layout_seen.items()
                              if node_id == node and now - seen < FRESH_SECONDS]
            predictions = []
            frame_counts = []
            for layout in active_layouts:
                current = self.current.get((node, layout))
                if not current or now - current["updated_at"] >= FRESH_SECONDS:
                    predictions.append({"label": "unknown", "reason": "no_fresh_window", "confidence": None})
                else:
                    predictions.append(self._predict_locked(node, layout, current["feature"]))
                    frame_counts.append(current["window_frames"])
            known = [p["label"] for p in predictions if p["label"] in {"empty", "occupied"}]
            if not predictions:
                pred = {"label": "unknown", "reason": "no_fresh_window", "confidence": None}
            elif len(known) != len(predictions):
                pred = next(p for p in predictions if p["label"] == "unknown")
            elif known.count("empty") == known.count("occupied"):
                pred = {"label": "unknown", "reason": "layout_disagreement", "confidence": None}
            else:
                pred = {"label": "empty" if known.count("empty") > known.count("occupied") else "occupied",
                        "reason": None, "confidence": min(p["confidence"] for p in predictions)}
            edge = self.edge.get(node)
            if pred["label"] in {"empty", "occupied"}:
                votes.append(pred["label"])
            nodes.append({"node_id": node, "presence": pred, "source_ip": self.sources.get(node),
                          "window_frames": min(frame_counts, default=0),
                          "edge": ({"presence_flag": edge["presence_flag"], "presence_score": edge["presence_score"],
                                    "motion_flag": edge["motion_flag"], "motion_energy": edge["motion_energy"],
                                    "received_age_ms": max(0, round((now - edge["received_at"]) * 1000)),
                                    "fresh": now - edge["received_at"] < FRESH_SECONDS} if edge else None)})
        room = "unknown"
        if len(votes) >= MIN_ROOM_NODES and votes.count("empty") != votes.count("occupied"):
            room = "empty" if votes.count("empty") > votes.count("occupied") else "occupied"
        cap = self.capture
        phase = cap["phase"] if cap else None
        remain = None
        if cap:
            deadline = cap["capture_at"] if phase.startswith("arming_") else cap.get("ends_at", cap["capture_at"])
            remain = max(0, math.ceil(deadline - now))
        models = self.profile.get("models", {}) if self.profile else {}
        training = self.profile.get("training", {}) if self.profile else {}
        model_nodes = sorted({int(key.split("|", 1)[0]) for key in models})
        model_layouts = [{"node_id": int(key.split("|", 1)[0]), "layout": key.split("|", 1)[1],
                          "accepted": True, "training_accuracy": model["training_accuracy"],
                          "accuracy_method": model.get("training_accuracy_method", "legacy_leave_one_window_out")}
                         for key, model in sorted(models.items())]
        return {"status": self.state, "error": self.error, "presence": room,
                "presence_validated": False, "room_check_passed": self.state == "room_check_passed",
                "eligible_nodes": len(votes), "nodes": nodes,
                "capture": {"phase": phase, "seconds_remaining": remain,
                            "duration_seconds": CAPTURE_SECONDS.get(phase.removeprefix("arming_") if phase else ""),
                            "start_delay_seconds": START_DELAY_SECONDS if phase and phase.startswith("arming_") else None,
                            "frames": dict(cap.get("frame_totals", {n: 0 for n in EXPECTED_NODES})) if cap else {},
                            "completed_frames": (self.training_buffer.get("empty_frames", {}) if self.training_buffer and not cap else {}),
                            "invalid_frames": dict(cap.get("invalid_frames", {})) if cap else {}},
                "model": {"nodes": model_nodes, "layouts": model_layouts,
                          "rejected": training.get("rejected", self.last_rejected),
                          "room_checks": self.profile.get("room_checks", {}) if self.profile else {},
                          "training_accuracy_is_not_validation": True}}

    def snapshot(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            self._advance_locked(now)
            return self._status_locked(now)

    def reset(self):
        with self.lock:
            self.capture = self.training_buffer = None
            self.current.clear()
            self.window_second.clear()
            self.window_values.clear()
            self.window_frames.clear()
            self.window_last.clear()
            self.last_layout_seen.clear()
            self.profile = None
            self.state, self.error = "not_trained", None
            try:
                self.profile_path.unlink()
            except FileNotFoundError:
                pass
            return self._status_locked(self.clock())
