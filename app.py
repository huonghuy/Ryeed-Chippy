"""CHIPPY local receiver dashboard. Python 3.10+, no web framework required."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
try:
    import serial
    from serial.tools import list_ports
    SERIAL_OK = hasattr(serial, "Serial")
except ImportError:
    SERIAL_OK = False

COLUMNS = "rx_uptime_ms,sender,version,packet_seq,hour_UTC,minute_UTC,second_UTC,uptime_ms,temperature_C,pressure_hPa,humidity_pct,gps_fix,latitude_deg,longitude_deg,altitude_m,satellites,radio_at_ok,bme_ok,sd_write_ok,rssi_dBm,snr_dB".split(",")
FLIGHT_COLUMNS = COLUMNS[4:17]
INTEGER_FIELDS = {"rx_uptime_ms", "sender", "packet_seq", "hour_UTC", "minute_UTC", "second_UTC", "uptime_ms", "gps_fix", "satellites", "radio_at_ok", "bme_ok", "sd_write_ok"}


def parse_packet(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("rx_uptime_ms,"):
        return None
    values = next(csv.reader([line]))
    if len(values) != len(COLUMNS) or values[2] != "C1":
        raise ValueError("Expected a 21-column C1 packet from CHIPPY_RECEIVER")
    packet = {}
    for key, value in zip(COLUMNS, values):
        if key == "version":
            packet[key] = value
        elif value == "":
            packet[key] = None
        else:
            try:
                number = int(value) if key in INTEGER_FIELDS else float(value)
            except ValueError as exc:
                raise ValueError(f"Invalid numeric field: {key}") from exc
            if not math.isfinite(number):
                raise ValueError(f"Non-finite field: {key}")
            packet[key] = number
    for key in ("rx_uptime_ms", "sender", "packet_seq", "uptime_ms"):
        if packet[key] is None or not 0 <= packet[key] <= 4294967295:
            raise ValueError(f"Invalid {key}")
    for key in ("gps_fix", "radio_at_ok", "bme_ok", "sd_write_ok"):
        if packet[key] not in (0, 1):
            raise ValueError(f"Invalid flag: {key}")
    for key, high in (("hour_UTC", 23), ("minute_UTC", 59), ("second_UTC", 60)):
        if packet[key] is not None and not 0 <= packet[key] <= high:
            raise ValueError(f"Invalid {key}")
    if packet["latitude_deg"] is not None and not -90 <= packet["latitude_deg"] <= 90:
        raise ValueError("Latitude out of range")
    if packet["longitude_deg"] is not None and not -180 <= packet["longitude_deg"] <= 180:
        raise ValueError("Longitude out of range")
    packet["has_position"] = bool(packet["gps_fix"] and packet["latitude_deg"] is not None and packet["longitude_deg"] is not None)
    return packet


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created TEXT, source TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS packets (id INTEGER PRIMARY KEY, session TEXT, body TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS packets_session ON packets(session,id)")
        self.db.commit()
        self.tracks = {}

    def create_session(self, source):
        sid = uuid.uuid4().hex
        with self.lock:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, datetime.now(timezone.utc).isoformat(), source))
            self.db.commit()
        return sid

    def sessions(self):
        with self.lock:
            return [dict(zip(("id", "created", "source", "count"), r)) for r in self.db.execute("SELECT s.id,s.created,s.source,COUNT(p.id) FROM sessions s LEFT JOIN packets p ON p.session=s.id GROUP BY s.id ORDER BY s.created DESC")]

    def add(self, sid, line, received_at=None, time_kind="received", commit=True):
        p = parse_packet(line)
        if p is None:
            return None
        p["received_at"] = received_at or datetime.now(timezone.utc).isoformat()
        p["time_kind"] = time_kind
        p["ascent_mps"] = None
        p["duplicate"] = False
        with self.lock:
            key = (sid, p["sender"])
            track = self.tracks.setdefault(key, {"boot": 0, "uptime": None, "seen": set(), "alt": None})
            identity = (p["packet_seq"], p["uptime_ms"])
            if identity in track["seen"]:
                p["duplicate"] = True
            else:
                if track["uptime"] is not None and p["uptime_ms"] < track["uptime"]:
                    # A reset/wrap/out-of-order sample must not produce an ascent spike.
                    track["boot"] += 1
                    track["alt"] = None
                    track["seen"].clear()
                track["seen"].add(identity)
                track["uptime"] = p["uptime_ms"]
                if p["has_position"] and p["altitude_m"] is not None:
                    previous = track["alt"]
                    if previous and p["uptime_ms"] > previous[0]:
                        p["ascent_mps"] = (p["altitude_m"] - previous[1]) / ((p["uptime_ms"] - previous[0]) / 1000)
                    track["alt"] = (p["uptime_ms"], p["altitude_m"])
            p["boot"] = track["boot"]
            cursor = self.db.execute("INSERT INTO packets(session,body) VALUES (?,?)", (sid, json.dumps(p, allow_nan=False)))
            if commit:
                self.db.commit()
            p["id"] = cursor.lastrowid
        return p

    def packets(self, sid, after=0, limit=2000):
        with self.lock:
            rows = self.db.execute("SELECT id,body FROM packets WHERE session=? AND id>? ORDER BY id LIMIT ?", (sid, after, limit)).fetchall()
        return [dict(json.loads(body), id=i) for i, body in rows]

    def import_csv(self, text, name):
        # Validate the whole upload before creating a session; no partial imports.
        lines = [line for line in text.lstrip("\ufeff").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines:
            raise ValueError("This CSV is empty.")
        reader = csv.DictReader(lines)
        reader.fieldnames = [name.strip() for name in (reader.fieldnames or [])]
        required = set(FLIGHT_COLUMNS) - {"hour_UTC", "minute_UTC", "second_UTC"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError("Missing flight CSV columns: " + ", ".join(sorted(missing)))
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("The CSV has duplicate column headers.")
        validated = []
        elapsed = 0.0
        previous_uptime = None
        for number, row in enumerate(reader, 2):
            if len(validated) >= 50000:
                raise ValueError("Limit is 50,000 rows per flight CSV.")
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"CSV row {number}: column count does not match the header.")
            def value(key, default=""):
                return row.get(key, default).strip()
            fields = [value("rx_uptime_ms", "0"), value("sender", "1"), value("version", "C1"), value("packet_seq", str(len(validated)))]
            fields += [value(key) for key in FLIGHT_COLUMNS]
            fields += [value("bme_ok", "1" if value("temperature_C") else "0"), value("sd_write_ok", "1"), value("rssi_dBm"), value("snr_dB")]
            line = ",".join(fields)
            try:
                packet = parse_packet(line)
                if packet is None:
                    raise ValueError("No packet data")
                timestamp = value("received_at")
                if timestamp:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("received_at needs a timezone, for example +00:00")
                    timestamp = parsed.astimezone(timezone.utc).isoformat()
                kind = "estimated" if not timestamp or value("time_kind") == "estimated" else "received"
            except (ValueError, TypeError) as exc:
                raise ValueError(f"CSV row {number}: {exc}") from exc
            uptime = packet["uptime_ms"]
            if previous_uptime is not None:
                elapsed += (uptime - previous_uptime) / 1000 if uptime >= previous_uptime else 1.0
            previous_uptime = uptime
            validated.append((line, timestamp, kind, elapsed))
        if not validated:
            raise ValueError("No data rows were found in the CSV.")
        base = time.time() - elapsed
        sid = uuid.uuid4().hex
        filename = str(name).replace("\\", "/").split("/")[-1][:100] or "flight.csv"
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, datetime.now(timezone.utc).isoformat(), "CSV · " + filename))
            for line, stamp, kind, offset in validated:
                stamp = stamp or datetime.fromtimestamp(base + offset, timezone.utc).isoformat()
                self.add(sid, line, stamp, time_kind=kind, commit=False)
        return sid, len(validated)


class Receiver:
    def __init__(self, store):
        self.store = store
        self.control = threading.Lock()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.state = {"mode": "disconnected", "port": None, "session": None, "error": None, "message": "Select your Uno port or start a demo.", "invalid_lines": 0}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise RuntimeError("Previous serial reader is still stopping. Try again shortly.")
        self.thread = None
        self.update(mode="disconnected", port=None, message="Disconnected. Saved packets remain available.")

    def start(self, mode, port=None):
        with self.control:
            self.stop()
            if mode == "serial":
                if not SERIAL_OK:
                    raise ValueError("pySerial unavailable. Run: python -m pip install pyserial==3.5")
                available = {p.device for p in list_ports.comports()}
                if port not in available:
                    raise ValueError("Select an available serial port and refresh the list if necessary.")
                # Opening the Uno normally resets it. Let setup messages pass through.
                try:
                    connection = serial.Serial(port, 115200, timeout=0.25)
                except Exception as exc:
                    raise ValueError(f"Cannot open {port}. Close Arduino Serial Monitor and other port users. {exc}") from exc
            else:
                connection = None
            sid = self.store.create_session("Demo" if mode == "demo" else f"Uno · {port}")
            self.stop_event = threading.Event()
            self.update(mode=mode, port=port, session=sid, error=None, invalid_lines=0, message="Demo data — not a real flight." if mode == "demo" else "Connected at 115200 baud. Waiting for receiver packets…")
            self.thread = threading.Thread(target=self.run_demo if mode == "demo" else self.run_serial, args=(sid, connection), daemon=True)
            self.thread.start()
            return sid

    def accept(self, sid, line):
        try:
            p = self.store.add(sid, line)
            if p:
                self.update(message="Receiving packets.")
            elif line.strip().startswith("#"):
                self.update(message=line.strip()[1:].strip()[:300])
        except (ValueError, csv.Error) as exc:
            with self.lock:
                self.state["invalid_lines"] += 1
                self.state["message"] = f"Ignored malformed line: {exc}"

    def run_serial(self, sid, connection):
        buffer = bytearray()
        discard = False
        try:
            while not self.stop_event.is_set():
                chunk = connection.read(min(max(connection.in_waiting, 1), 4096))
                for byte in chunk:
                    if byte == 10:
                        if not discard:
                            try:
                                self.accept(sid, buffer.decode("ascii"))
                            except UnicodeDecodeError:
                                self.update(message="Ignored non-ASCII serial data; check baud rate.")
                        buffer.clear()
                        discard = False
                    elif not discard:
                        buffer.append(byte)
                        if len(buffer) > 2048:
                            buffer.clear()
                            discard = True
                            self.update(message="Ignored oversized serial line.")
        except Exception as exc:
            self.update(mode="disconnected", error=f"Serial connection lost: {exc}", message="Reconnect the Uno and select Connect.")
        finally:
            connection.close()

    def run_demo(self, sid, _connection):
        # Clearly synthetic track near College Park, MD; never mixed with real data.
        for seq in range(100000):
            if self.stop_event.is_set():
                return
            seconds = seq * 5
            utc = datetime.now(timezone.utc)
            altitude = 80 + seconds * 4.2
            line = f"{seconds*1000},1,C1,{seq},{utc.hour},{utc.minute},{utc.second},{seconds*1000},{21-seq*0.03:.2f},1000,50,1,{38.985+seq*0.0006:.6f},{-76.942+seq*0.0009:.6f},{altitude:.2f},10,1,1,1,-65,9"
            self.accept(sid, line)
            if self.stop_event.wait(1):
                return


def make_handler(store, receiver):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, data, content_type="application/json", status=200, extra=None):
            body = json.dumps(data, allow_nan=False).encode() if content_type == "application/json" else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.local_host():
                self.reply({"error": "Local host required"}, status=403)
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/state":
                    self.reply({**receiver.snapshot(), "sessions": store.sessions(), "serial_available": SERIAL_OK})
                elif parsed.path == "/api/ports":
                    self.reply([{"device": p.device, "description": p.description} for p in list_ports.comports()] if SERIAL_OK else [])
                elif parsed.path == "/api/packets":
                    after = max(0, int(query.get("after", [0])[0]))
                    self.reply(store.packets(query.get("session", [""])[0], after))
                elif parsed.path == "/api/export":
                    sid = query.get("session", [""])[0]
                    output = io.StringIO(newline="")
                    fields = ["received_at", *COLUMNS, "ascent_mps", "boot", "duplicate", "time_kind"]
                    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader()
                    after = 0
                    while batch := store.packets(sid, after):
                        writer.writerows(batch)
                        after = batch[-1]["id"]
                    self.reply(output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="chippy-received.csv"'})
                else:
                    files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript"), "/style.css": ("style.css", "text/css"), "/vendor/leaflet.js": ("vendor/leaflet.js", "application/javascript"), "/vendor/leaflet.css": ("vendor/leaflet.css", "text/css")}
                    if parsed.path not in files:
                        self.reply({"error": "Not found"}, status=404)
                        return
                    name, mime = files[parsed.path]
                    self.reply((ROOT / "static" / name).read_bytes(), mime)
            except (ValueError, KeyError) as exc:
                self.reply({"error": str(exc)}, status=400)

        def do_POST(self):
            if not self.local_host():
                self.reply({"error": "Local host required"}, status=403)
                return
            # Browser requests must originate from this same loopback app.
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Origin") not in (None, expected, expected.replace("127.0.0.1", "localhost")):
                self.reply({"error": "Origin not allowed"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                maximum = 10 * 1024 * 1024 if self.path == "/api/import" else 2048
                if not 0 <= length <= maximum:
                    raise ValueError("Request too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/api/import":
                    if not isinstance(body.get("csv"), str):
                        raise ValueError("Choose a CSV file first.")
                    sid, count = store.import_csv(body["csv"], body.get("name", "flight.csv"))
                    self.reply({"session": sid, "count": count})
                elif self.path == "/api/connect":
                    mode = body.get("mode")
                    if mode not in ("serial", "demo"):
                        raise ValueError("Invalid mode")
                    sid = receiver.start(mode, body.get("port"))
                    self.reply({"session": sid})
                elif self.path == "/api/disconnect":
                    with receiver.control:
                        receiver.stop()
                    self.reply({"ok": True})
                else:
                    self.reply({"error": "Not found"}, status=404)
            except (ValueError, RuntimeError, TypeError) as exc:
                self.reply({"error": str(exc)}, status=400)

        def local_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Website port (not the Arduino COM port)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    store = Store(args.data_dir / "chippy.sqlite3")
    receiver = Receiver(store)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, receiver))
    except OSError as exc:
        raise SystemExit(f"Could not start dashboard: {exc}. Try --port 8766")
    if args.demo:
        receiver.start("demo")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"CHIPPY dashboard: {url}\nSaved packets: {args.data_dir.resolve()}\nClose Arduino Serial Monitor before connecting. Ctrl+C stops the app.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        receiver.stop()
        server.server_close()


if __name__ == "__main__":
    main()
