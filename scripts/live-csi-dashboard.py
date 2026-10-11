#!/usr/bin/env python3
"""Local RuView dashboard for validated ESP32 CSI and experimental presence.

No simulation or raw disk capture. Host room estimates require explicit live
captures; firmware presence is shown separately as an experimental heuristic.
"""

import argparse
from collections import Counter, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path
import socket
import sqlite3
import struct
import threading
import time
from urllib.parse import urlsplit

from presence_detector import PresenceEngine, decode_vitals


HEADER = struct.Struct("<IBBHIIbbBB")
CSI_MAGIC = 0xC5110001
KNOWN_AUXILIARY = {0xC5110002, 0xC5110003, 0xC5110004, 0xC5110005,
                   0xC5110006, 0xC5110007, 0xC5118100, 0xC511A110}
FRESH_SECONDS = 5
EXPECTED_NODES = {1, 2, 3, 4}
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path.home() / ".local" / "share" / "ruview" / "ruview.sqlite3"


class EventStore:
    """Local SQLite journal for system notifications shared with the app."""
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        db = self.connect()
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS system_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, source TEXT NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS system_events_created ON system_events(id DESC)")
                db.execute("CREATE TABLE IF NOT EXISTS node_latest (node_id INTEGER PRIMARY KEY, updated_at TEXT NOT NULL, source_ip TEXT NOT NULL, csi_sequence INTEGER NOT NULL, rssi_dbm INTEGER NOT NULL, frequency_mhz INTEGER NOT NULL, antennas INTEGER NOT NULL, subcarriers INTEGER NOT NULL, packet_count INTEGER NOT NULL)")
        finally:
            db.close()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def add(self, severity, title, message):
        with self.lock:
            db = self.connect()
            try:
                with db:
                    db.execute("INSERT INTO system_events(created_at,severity,title,message,source) VALUES(?,?,?,?,?)",
                               (datetime.now(timezone.utc).isoformat(), severity, title, message, "receiver"))
            finally:
                db.close()

    def latest(self, limit=50):
        with self.lock:
            db = self.connect()
            try:
                rows = db.execute("SELECT id,created_at,severity,title,message,source FROM system_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [dict(row) for row in rows]
            finally:
                db.close()

    def update_node(self, frame, source_ip, packet_count):
        with self.lock:
            db = self.connect()
            try:
                with db:
                    db.execute("INSERT INTO node_latest(node_id,updated_at,source_ip,csi_sequence,rssi_dbm,frequency_mhz,antennas,subcarriers,packet_count) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET updated_at=excluded.updated_at,source_ip=excluded.source_ip,csi_sequence=excluded.csi_sequence,rssi_dbm=excluded.rssi_dbm,frequency_mhz=excluded.frequency_mhz,antennas=excluded.antennas,subcarriers=excluded.subcarriers,packet_count=excluded.packet_count",
                               (frame["node_id"], datetime.now(timezone.utc).isoformat(), source_ip,
                                frame["csi_sequence"], frame["rssi_dbm"], frame["frequency_mhz"],
                                frame["antennas"], frame["subcarriers"], packet_count))
            finally:
                db.close()


def decode_csi(data):
    """Validate the exact firmware header/payload before decoding signed I/Q."""
    if len(data) < HEADER.size:
        raise ValueError("short_header")
    magic, node, antennas, subcarriers, freq, seq, rssi, noise, ppdu, flags = HEADER.unpack_from(data)
    if magic != CSI_MAGIC:
        raise ValueError("unknown_magic")
    if node not in EXPECTED_NODES:
        raise ValueError("unexpected_node")
    if not 1 <= antennas <= 4 or not 1 <= subcarriers <= 256:
        raise ValueError("invalid_dimensions")
    if len(data) != HEADER.size + 2 * antennas * subcarriers:
        raise ValueError("payload_length")
    if not (freq == 2484 or 2412 <= freq <= 2472 and (freq - 2412) % 5 == 0):
        raise ValueError("unexpected_frequency")
    if not -127 <= rssi <= -1:
        raise ValueError("invalid_rssi")
    iq = struct.unpack(f"{antennas * subcarriers * 2}b", data[HEADER.size:])
    amplitude = [math.hypot(iq[i], iq[i + 1]) for i in range(0, len(iq), 2)]
    return dict(node_id=node, csi_sequence=seq, rssi_dbm=rssi,
                noise_floor_dbm=noise, frequency_mhz=freq, antennas=antennas,
                subcarriers=subcarriers, ppdu_type=ppdu, flags=flags,
                amplitude=amplitude)


class Receiver:
    def __init__(self, lan_ip, udp_port, network, profile_path=None, clock=time.monotonic,
                 http_port=3002, event_store=None, http_host="0.0.0.0"):
        self.lock = threading.Lock()
        self.nodes = {}
        self.rejected = Counter()
        self.auxiliary = Counter()
        self.clock = clock
        self.http_host, self.http_port = http_host, http_port
        self.started = clock()
        self.presence = PresenceEngine(profile_path=profile_path, clock=clock)
        self.presence_ignored_packets = 0
        self.events = event_store or EventStore()
        self.node_states = {}
        self.events.add("warning", "Waiting for CSI nodes", "The receiver is running but has not received a validated CSI packet yet.")
        self.network = ipaddress.ip_network(network, strict=False)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((lan_ip, udp_port))
        self.lan_ip, self.udp_port = lan_ip, udp_port

    def receive(self):
        while True:
            data, peer = self.socket.recvfrom(4096)
            if ipaddress.ip_address(peer[0]) not in self.network:
                with self.lock:
                    self.rejected["source_outside_lan"] += 1
                continue
            magic = struct.unpack_from("<I", data)[0] if len(data) >= 4 else None
            if magic == 0xC5110002:
                try:
                    packet = decode_vitals(data)
                except ValueError as exc:
                    with self.lock:
                        self.rejected[str(exc)] += 1
                    continue
                if not self.presence.observe_vitals(packet, peer[0], self.clock()):
                    with self.lock:
                        self.presence_ignored_packets += 1
                with self.lock:
                    self.auxiliary["0xc5110002"] += 1
                continue
            if magic in KNOWN_AUXILIARY:
                with self.lock:
                    self.auxiliary[f"0x{magic:08x}"] += 1
                continue
            try:
                frame = decode_csi(data)
            except ValueError as exc:
                with self.lock:
                    self.rejected[str(exc)] += 1
                continue
            now = self.clock()
            presence_accepted = self.presence.observe_csi(frame, peer[0], now)
            with self.lock:
                if not presence_accepted:
                    self.presence_ignored_packets += 1
                previous_state = self.node_states.get(frame["node_id"])
                if previous_state == "stale":
                    self.events.add("info", f"Node {frame['node_id']} reconnected", "Validated CSI packets resumed.")
                self.node_states[frame["node_id"]] = "active"
                node = self.nodes.setdefault(frame["node_id"], {
                    "packet_count": 0, "arrivals": deque(), "previous": {},
                    "duplicate_sequences": 0, "last_sequence": None,
                })
                # Keep packet rates measured over a rolling five-second window.
                node["arrivals"].append(now)
                while node["arrivals"] and now - node["arrivals"][0] > FRESH_SECONDS:
                    node["arrivals"].popleft()
                node["packet_count"] += 1
                if node["last_sequence"] == frame["csi_sequence"]:
                    node["duplicate_sequences"] += 1
                node["last_sequence"] = frame["csi_sequence"]
                layout = (frame["frequency_mhz"], frame["antennas"],
                          frame["subcarriers"], frame["ppdu_type"], frame["flags"] & 1)
                previous = node["previous"].get(layout)
                frame["signal_change_pct"] = None
                if previous and now - previous[0] < FRESH_SECONDS:
                    old = previous[1]
                    scale = math.sqrt(sum(v * v for v in old) / len(old))
                    if scale > 0:
                        difference = math.sqrt(sum((a - b) ** 2 for a, b in zip(frame["amplitude"], old)) / len(old))
                        frame["signal_change_pct"] = round(100 * difference / scale, 2)
                node["previous"][layout] = (now, frame["amplitude"])
                # Bound memory even if a sender changes its frame layout.
                if len(node["previous"]) > 32:
                    node["previous"].pop(next(iter(node["previous"])))
                node.update(frame=frame, received_at=now, source_ip=peer[0])
                if now - node.get("db_updated_at", 0) >= 1:
                    self.events.update_node(frame, peer[0], node["packet_count"])
                    node["db_updated_at"] = now

    def snapshot(self):
        now = self.clock()
        if not hasattr(self, "node_states"):
            self.node_states = {}
        with self.lock:
            nodes, frames = [], []
            for node_id, node in sorted(self.nodes.items()):
                frame = node["frame"]
                age_ms = round((now - node["received_at"]) * 1000)
                fresh = age_ms < FRESH_SECONDS * 1000
                previous_state = self.node_states.get(node_id)
                next_state = "active" if fresh else "stale"
                if previous_state == "active" and next_state == "stale":
                    self.events.add("warning", f"Node {node_id} signal stale", "No validated CSI packet arrived for 5 seconds.")
                self.node_states[node_id] = next_state
                arrivals = sum(now - stamp < FRESH_SECONDS for stamp in node["arrivals"])
                rate_window = min(FRESH_SECONDS, max(now - self.started, 0.001))
                nodes.append(dict(node_id=node_id, csi_status="active" if fresh else "stale",
                                  csi_last_seen_ms=age_ms, csi_sequence=frame["csi_sequence"],
                                  rssi_dbm=frame["rssi_dbm"], source_ip=node["source_ip"],
                                  packet_count=node["packet_count"], packet_rate_hz=round(arrivals / rate_window, 1),
                                  subcarriers=frame["subcarriers"], duplicate_sequences=node["duplicate_sequences"]))
                if fresh:
                    frames.append(dict(frame, csi_last_seen_ms=age_ms))
            state = "live_unverified" if frames else "stale" if nodes else "disconnected"
            changes = [f["signal_change_pct"] for f in frames if f["signal_change_pct"] is not None]
            return {
                "status": dict(source="esp32", source_state=state, simulation_enabled=False,
                               backend="validated-adr018-receiver", pose_model_loaded=False,
                               presence_validated=False, udp_target=f"{self.lan_ip}:{self.udp_port}",
                               uptime_seconds=round(now - self.started, 1),
                               accepted_csi_packets=sum(n["packet_count"] for n in nodes),
                               rejected_packets=sum(self.rejected.values()), rejection_reasons=dict(self.rejected),
                               auxiliary_packets=dict(self.auxiliary),
                               presence_ignored_packets=self.presence_ignored_packets),
                "nodes": dict(nodes=nodes, total=len(nodes)),
                "latest": dict(source="esp32", source_state=state, nodes=frames,
                               timestamp=datetime.now(timezone.utc).isoformat(),
                               measured_signal_change_pct=round(sum(changes) / len(changes), 2) if changes else None,
                               calibrated_presence_evidence=None),
                "presence": self.presence.snapshot(now),
            }


def make_handler(receiver):
    class Handler(BaseHTTPRequestHandler):
        def _allowed_web_origin(self, origin):
            if not origin:
                return False
            try:
                parsed = urlsplit(origin)
                if parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port != 8081:
                    return False
                host = parsed.hostname
                if host in {"localhost", "127.0.0.1"}:
                    return True
                return ipaddress.ip_address(host) in receiver.network
            except (ValueError, TypeError):
                return False

        def _json(self, status, value):
            body = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            origin = self.headers.get("Origin", "")
            requested = self.headers.get("Access-Control-Request-Method", "")
            if not self._allowed_web_origin(origin) or requested != "GET" or not urlsplit(self.path).path.startswith("/api/v1/"):
                self.send_error(403)
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
            self.send_header("Access-Control-Max-Age", "300")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def _local_json_body(self):
            port = receiver.http_port
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise ValueError("presence controls are local-only")
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                raise ValueError("request host must match the local dashboard")
            origin = self.headers.get("Origin")
            allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            if origin is not None and origin not in allowed_origins:
                raise ValueError("request origin must exactly match the local dashboard")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("content type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise ValueError("content length is required") from exc
            if not 1 <= length <= 4096:
                raise ValueError("request body must be between 1 and 4096 bytes")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("request body must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self):
            try:
                peer = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                self.send_error(403)
                return
            if not peer.is_loopback and peer not in receiver.network:
                self.send_error(403)
                return
            allowed_hosts = {f"127.0.0.1:{receiver.http_port}", f"localhost:{receiver.http_port}",
                             f"{receiver.lan_ip}:{receiver.http_port}"}
            if self.headers.get("Host", "") not in allowed_hosts:
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            snapshot = receiver.snapshot()
            routes = {"/api/v1/status": snapshot["status"],
                      "/api/v1/nodes": snapshot["nodes"],
                      "/api/v1/sensing/latest": snapshot["latest"],
                      "/api/v1/presence": snapshot["presence"],
                      "/api/v1/events": {"events": receiver.events.latest()},
                      "/health": {"status": "healthy", "service": "RuView live CSI receiver"}}
            if path in routes:
                body = json.dumps(routes[path], allow_nan=False).encode()
                content_type = "application/json; charset=utf-8"
            elif path in {"/", "/ui", "/ui/", "/ui/index.html", "/ui/real-csi.html"}:
                body = (ROOT / "ui" / "real-csi.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin", "")
            if content_type.startswith("application/json") and self._allowed_web_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                payload = self._local_json_body()
                if path == "/api/v1/presence/capture":
                    if set(payload) != {"action"} or not isinstance(payload["action"], str):
                        raise ValueError("capture body must contain only an action string")
                    result = receiver.presence.start_capture(payload["action"], receiver.clock())
                elif path == "/api/v1/presence/reset":
                    if payload:
                        raise ValueError("reset body must be an empty object")
                    result = receiver.presence.reset()
                else:
                    self._json(404, {"error": "not_found"})
                    return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, result)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lan-ip", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--allow-cidr", required=True)
    parser.add_argument("--udp-port", type=int, default=5005)
    parser.add_argument("--http-port", type=int, default=3002)
    parser.add_argument("--http-host", default="0.0.0.0", help="LAN access for the paired mobile app")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--profile-path", type=Path, help="private profile path for isolated testing")
    args = parser.parse_args()
    receiver = Receiver(str(args.lan_ip), args.udp_port, args.allow_cidr,
                        profile_path=args.profile_path, http_port=args.http_port,
                        event_store=EventStore(args.database), http_host=args.http_host)
    server = ThreadingHTTPServer((args.http_host, args.http_port), make_handler(receiver))
    threading.Thread(target=receiver.receive, daemon=True).start()
    print(f"RuView real CSI: http://{args.lan_ip}:{args.http_port}/ | UDP {args.lan_ip}:{args.udp_port} | DB {args.database}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
