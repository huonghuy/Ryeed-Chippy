"""Forward a USB receiver to a CHIPPY server, keeping an on-disk retry queue."""
import argparse
import getpass
from http.client import HTTPException
import http.cookiejar
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.request import build_opener, HTTPCookieProcessor, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import serial
from serial.tools import list_ports


def permission_ended(exc):
    # Only the app answers in JSON; a 403 or 404 from Cloudflare or the tunnel is worth retrying.
    return exc.code in (401, 403, 404, 409) and exc.headers.get_content_type() == "application/json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Dashboard origin, e.g. https://chippy.example.com")
    parser.add_argument("--port", help="Arduino USB port, for example COM4")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--queue", type=Path, default=Path(__file__).resolve().parent / "data" / "upload-queue.sqlite3")
    args = parser.parse_args()
    if args.list_ports:
        for port in list_ports.comports():
            print(f"{port.device}: {port.description}")
        return
    if not args.port:
        parser.error("--port is required; use --list-ports to find your USB receiver")
    parsed = urlparse(args.server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/") or parsed.username or parsed.query or parsed.fragment:
        parser.error("--server must be a website origin, without a path or credentials")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("Use HTTPS for a remote server so the admin password is encrypted in transit")
    base = args.server.rstrip("/")
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(path, body):
        # Cloudflare bans the default Python-urllib User-Agent (error 1010).
        request = Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "CHIPPY-Uploader/1.0"})
        with opener.open(request, timeout=15) as response:
            return json.load(response)

    try:
        post("/api/login", {"password": getpass.getpass("Admin password: ")})
    except (URLError, ValueError) as exc:
        raise SystemExit(f"Could not log in or start the feed: {exc}")
    args.queue.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.queue, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, line TEXT NOT NULL)")
    db.commit()
    lock = threading.Lock()
    stopping = threading.Event()
    fatal = threading.Event()

    def transmit():
        pending = None
        while not stopping.is_set():
            if pending is None:
                with lock:
                    rows = db.execute("SELECT id,line FROM outbox ORDER BY id LIMIT 200").fetchall()
                if not rows:
                    stopping.wait(.3)
                    continue
                pending = (uuid.uuid4().hex, rows)
            batch, rows = pending
            try:
                result = post("/api/input/append", {"session": sid, "shared": True, "batch": batch, "lines": [line for _, line in rows]})
                with lock, db:
                    db.executemany("DELETE FROM outbox WHERE id=?", [(i,) for i, _ in rows])
                print(f"Uploaded {len(rows)} serial lines ({result.get('invalid', 0)} malformed telemetry lines).", flush=True)
                pending = None
            except HTTPError as exc:
                if permission_ended(exc):
                    print("Publishing permission ended or another admin replaced the feed. Capture is stopping; queued lines remain on disk. Restart and log in again.", flush=True)
                    fatal.set()
                    return
                print(f"Server returned {exc.code}; data remains queued. Retrying…", flush=True)
                stopping.wait(3)
            except (URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
                print(f"Upload interrupted; data remains queued. Retrying… {exc}", flush=True)
                stopping.wait(3)

    worker = threading.Thread(target=transmit, daemon=True)
    print(f"Publishing {args.port} at 115200 baud to {base}. Close Arduino Serial Monitor first.")
    print(f"Retry queue: {args.queue.resolve()}. Ctrl+C stops capture; unsent lines remain for the next run.")
    try:
        with serial.Serial(args.port, 115200, timeout=.5) as connection:
            sid = post("/api/input/start", {"shared": True})["session"]
            worker.start()
            buffer = bytearray()
            discard = False
            while not fatal.is_set():
                chunk = connection.read(min(max(connection.in_waiting, 1), 4096))
                for byte in chunk:
                    if byte == 10:
                        if not discard:
                            line = buffer.decode("ascii", errors="replace").rstrip("\r")
                            with lock, db:
                                db.execute("INSERT INTO outbox(line) VALUES (?)", (line,))
                        buffer.clear()
                        discard = False
                    elif not discard:
                        buffer.append(byte)
                        if len(buffer) > 2048:
                            buffer.clear()
                            discard = True
                            print("Ignored oversized serial line.", flush=True)
    except KeyboardInterrupt:
        print("Stopping receiver capture.")
    except serial.SerialException as exc:
        print(f"USB connection failed: {exc}. Queued lines remain on disk.")
    except (URLError, ValueError) as exc:
        print(f"Could not start the shared feed: {exc}. Queued lines remain on disk.")
    finally:
        stopping.set()
        if worker.is_alive():
            worker.join(timeout=20)
        if not worker.is_alive():
            with lock:
                remaining = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                db.close()
            print(f"{remaining} unsent lines retained in the retry queue.")


if __name__ == "__main__":
    main()
