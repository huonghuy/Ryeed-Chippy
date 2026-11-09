import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import contextlib
import io
import sqlite3
import time
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request

from app import Store, Receiver, make_handler, COLUMNS
from shared_feed import Access, FeedError
from test_app import sample


def position(seq, uptime, lat, lon, altitude=100):
    fields = sample(seq, uptime, altitude).split(",")
    fields[12:14] = [str(lat), str(lon)]
    return ",".join(fields)


class GroundSpeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path)
        self.sid = self.store.create_session("speed")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_horizontal_distance_ignores_altitude_and_uses_sample_time(self):
        first = self.store.add(self.sid, position(0, 1000, 0, 0, 100))
        second = self.store.add(self.sid, position(1, 11000, 0, .001, 30480))
        self.assertIsNone(first["ground_speed_mps"])
        self.assertAlmostEqual(second["ground_speed_mps"], 11.119508, places=5)
        third = self.store.add(self.sid, position(2, 21000, 0, .001, 40000))
        self.assertEqual(third["ground_speed_mps"], 0)

    def test_dateline_no_fix_duplicate_restart_and_multiple_senders(self):
        self.store.add(self.sid, position(0, 1000, 0, 179.999))
        line = position(1, 11000, 0, -179.999)
        self.assertAlmostEqual(self.store.add(self.sid, line)["ground_speed_mps"], 22.239016, places=5)
        self.assertIsNone(self.store.add(self.sid, line)["ground_speed_mps"])
        self.assertIsNone(self.store.add(self.sid, sample(2, 16000, fix=0))["ground_speed_mps"])
        self.assertIsNone(self.store.add(self.sid, position(0, 500, 1, 1))["ground_speed_mps"])
        other_sender = position(1, 1000, 0, 0).split(",")
        other_sender[1] = "2"
        self.assertIsNone(self.store.add(self.sid, ",".join(other_sender))["ground_speed_mps"])

    def test_reopened_store_recovers_baseline_and_duplicates(self):
        self.store.add(self.sid, position(0, 1000, 0, 0))
        self.store.db.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.add(self.sid, position(0, 1000, 0, 0))["ground_speed_mps"])
        next_p = self.store.add(self.sid, position(1, 11000, 0, .001))
        self.assertAlmostEqual(next_p["ground_speed_mps"], 11.119508, places=5)


class SharedFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, Receiver(self.store)))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.publisher = self.client()
        self.viewer = self.client()
        self.other = self.client()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.temp.cleanup()

    @staticmethod
    def client():
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        with client.open(request, timeout=5) as response:
            return json.load(response)

    def assert_status(self, code, client, path, body=None, headers=None):
        with self.assertRaises(HTTPError) as caught:
            self.request(client, path, body, headers)
        self.assertEqual(caught.exception.code, code)

    def login(self, client=None, password="bpp"):
        return self.request(client or self.publisher, "/api/login", {"password": password})

    def test_private_input_and_csv_are_hidden_from_other_viewers(self):
        self.login()
        sid = self.request(self.publisher, "/api/input/start", {"shared": False})["session"]
        self.request(self.publisher, "/api/input/append", {"shared": False, "session": sid, "batch": "a", "lines": [sample()]})
        self.assertEqual(len(self.request(self.publisher, f"/api/packets?session={sid}")), 1)
        self.assertEqual(self.request(self.viewer, "/api/state")["sessions"], [])
        for endpoint in ("packets", "raw", "export"):
            self.assert_status(404, self.viewer, f"/api/{endpoint}?session={sid}")
        csv = ",".join(COLUMNS) + "\n" + sample()
        imported = self.request(self.publisher, "/api/import", {"name": "private.csv", "csv": csv})["session"]
        self.assert_status(404, self.viewer, f"/api/packets?session={imported}")
        self.assertIsNone(self.request(self.viewer, "/api/state")["live_session"])

    def test_admin_required_for_all_publishing_and_password_routes(self):
        csv = ",".join(COLUMNS) + "\n" + sample()
        for path, body in (("/api/input/start", {"shared": True}), ("/api/input/start", {"shared": False}), ("/api/input/start", {"shared": False, "demo": True}), ("/api/input/append", {"shared": True}), ("/api/input/append", {"shared": False}), ("/api/import", {"shared": True, "name": "x.csv", "csv": csv}), ("/api/import", {"name": "x.csv", "csv": csv}), ("/api/map-config", {"action": "clear_kml", "shared": True}), ("/api/map-config", {"action": "clear_kml"}), ("/api/password", {"current": "bpp", "password": "new"})):
            self.assert_status(401, self.viewer, path, body)
        self.assertEqual(self.store.sessions(), [])

    def test_one_visitor_cannot_lock_out_the_admin_behind_cloudflare(self):
        visitor = {"CF-Connecting-IP": "198.51.100.7"}
        for _ in range(5):
            self.assert_status(401, self.viewer, "/api/login", {"password": "wrong"}, visitor)
        self.assert_status(429, self.viewer, "/api/login", {"password": "bpp"}, visitor)
        self.request(self.publisher, "/api/login", {"password": "bpp"}, {"CF-Connecting-IP": "192.0.2.50"})
        self.assertTrue(self.request(self.publisher, "/api/state")["admin"])

    def test_login_failures_are_capped_across_addresses(self):
        auth = Access(self.store)
        with patch.object(auth, "check_password", return_value=False):
            for address in range(50):
                with self.assertRaises(FeedError) as caught:
                    auth.login("wrong", str(address))
                self.assertEqual(caught.exception.status, 401)
            with self.assertRaises(FeedError) as caught:
                auth.login("wrong", "fresh")
        self.assertEqual(caught.exception.status, 429)

    def test_only_the_browser_id_cookie_is_lax(self):
        request = Request(self.base + "/api/login", data=json.dumps({"password": "bpp"}).encode(), headers={"Content-Type": "application/json"})
        with self.viewer.open(request, timeout=5) as response:
            cookies = response.headers.get_all("Set-Cookie")
        self.assertIn("SameSite=Lax", next(c for c in cookies if c.startswith("chippy_browser=")))
        self.assertIn("SameSite=Strict", next(c for c in cookies if c.startswith("chippy_admin=")))

    def test_shared_feed_updates_both_viewers_and_retry_is_idempotent(self):
        self.login()
        sid = self.request(self.publisher, "/api/input/start", {"shared": True})["session"]
        body = {"shared": True, "session": sid, "batch": "retry-1", "lines": ["# Ready", sample(), "malformed", position(1, 6000, 39, -76.94, 30480)]}
        result = self.request(self.publisher, "/api/input/append", body)
        self.assertEqual((result["accepted"], result["invalid"]), (2, 1))
        self.assertTrue(self.request(self.publisher, "/api/input/append", body)["repeated"])
        for viewer in (self.viewer, self.other):
            state = self.request(viewer, "/api/state")
            self.assertFalse(state["admin"])
            self.assertEqual(state["live_session"], sid)
            packets = self.request(viewer, f"/api/packets?session={sid}")
            self.assertEqual(len(packets), 2)
            self.assertEqual(packets[-1]["altitude_m"], 30480)
            self.assertEqual(len(self.request(viewer, f"/api/raw?session={sid}")), 4)
            self.assertEqual(len(self.request(viewer, f"/api/packets?session={sid}&after={packets[0]['id']}")), 1)

    def test_password_change_persists_and_revokes_other_sessions(self):
        self.login(); self.login(self.other)
        self.assert_status(403, self.publisher, "/api/password", {"current": "wrong", "password": "new-password"})
        self.request(self.publisher, "/api/password", {"current": "bpp", "password": "new-password"})
        self.assertTrue(self.request(self.publisher, "/api/state")["admin"])
        self.assertFalse(self.request(self.other, "/api/state")["admin"])
        self.assert_status(401, self.viewer, "/api/login", {"password": "bpp"})
        self.login(self.viewer, "new-password")
        reopened = Store(self.path)
        try:
            auth = Access(reopened)
            self.assertTrue(auth.check_password("new-password"))
            self.assertFalse(auth.check_password("bpp"))
            self.assertNotIn("new-password", auth.setting("password"))
        finally:
            reopened.db.close()

    def test_another_admin_can_replace_feed_but_cannot_append_to_previous_owner(self):
        self.login(); self.login(self.other)
        sid = self.request(self.publisher, "/api/input/start", {"shared": True})["session"]
        body = {"shared": True, "session": sid, "batch": "a", "lines": [sample()]}
        self.assert_status(403, self.other, "/api/input/append", body)
        replacement = self.request(self.other, "/api/input/start", {"shared": True})["session"]
        self.assert_status(409, self.publisher, "/api/input/append", body)
        self.assertEqual(self.request(self.viewer, "/api/state")["live_session"], replacement)

    def test_publish_csv_replaces_live_feed_and_survives_restart(self):
        self.login()
        self.request(self.publisher, "/api/input/start", {"shared": True})
        csv = ",".join(COLUMNS) + "\n" + sample()
        sid = self.request(self.publisher, "/api/import", {"name": "published.csv", "csv": csv, "shared": True})["session"]
        self.assertEqual(self.request(self.viewer, "/api/state")["live_session"], sid)
        self.assertEqual(len(self.request(self.viewer, f"/api/packets?session={sid}")), 1)
        reopened = Store(self.path)
        try:
            self.assertEqual(Access(reopened).setting("live_session"), sid)
        finally:
            reopened.db.close()

    def test_bad_origin_host_login_throttle_and_logout(self):
        self.assert_status(403, self.viewer, "/api/login", {"password": "bpp"}, {"Origin": "https://evil.example"})
        self.assert_status(403, self.viewer, "/api/state", headers={"Host": "evil.example"})
        self.assert_status(415, self.viewer, "/api/login", {"password": "bpp"}, {"Content-Type": "text/plain"})
        self.login()
        self.request(self.publisher, "/api/logout", {})
        self.assertFalse(self.request(self.publisher, "/api/state")["admin"])
        for _ in range(5):
            self.assert_status(401, self.viewer, "/api/login", {"password": "wrong"})
        self.assert_status(429, self.viewer, "/api/login", {"password": "bpp"})

    def test_malformed_batch_rejects_without_partial_data(self):
        self.login()
        sid = self.request(self.publisher, "/api/input/start", {"shared": False})["session"]
        self.assert_status(400, self.publisher, "/api/input/append", {"session": sid, "batch": "x", "lines": [sample(), "bad\nline"]})
        self.assertEqual(self.request(self.publisher, f"/api/packets?session={sid}"), [])

    def test_python_uploader_reads_usb_and_drains_saved_backlog(self):
        import receiver_uploader
        queue = Path(self.temp.name) / "queue.db"
        db = sqlite3.connect(queue)
        db.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY,line TEXT NOT NULL)")
        db.execute("INSERT INTO outbox(line) VALUES (?)", ("# Saved backlog",))
        db.commit(); db.close()
        store = self.store

        class FakeSerial:
            def __init__(self, *_args, **_kwargs):
                self.chunks = [("# Ready\r\n" + sample()[:35]).encode(), (sample()[35:] + "\r\n").encode()]
                self.deadline = time.monotonic() + 4
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            @property
            def in_waiting(self):
                return 1
            def read(self, _length):
                if self.chunks:
                    return self.chunks.pop(0)
                if any(s["count"] for s in store.sessions()) or time.monotonic() > self.deadline:
                    raise KeyboardInterrupt
                time.sleep(.02)
                return b""

        argv = ["receiver_uploader.py", "--server", self.base, "--port", "FAKE", "--queue", str(queue)]
        with patch.object(receiver_uploader.sys, "argv", argv), patch.object(receiver_uploader.getpass, "getpass", return_value="bpp"), patch.object(receiver_uploader.serial, "Serial", FakeSerial), contextlib.redirect_stdout(io.StringIO()):
            receiver_uploader.main()
        sid = self.request(self.viewer, "/api/state")["live_session"]
        self.assertEqual(len(self.request(self.viewer, f"/api/packets?session={sid}")), 1)
        lines = self.request(self.viewer, f"/api/raw?session={sid}")
        self.assertEqual([line["line"] for line in lines], ["# Saved backlog", "# Ready", sample()])
        db = sqlite3.connect(queue)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        finally:
            db.close()

    def test_uploader_stops_only_for_permission_errors_from_the_app(self):
        from receiver_uploader import permission_ended

        def error(code, kind):
            headers = Message()
            headers["Content-Type"] = kind
            return HTTPError("https://x", code, "error", headers, io.BytesIO())

        for code in (401, 403, 404, 409):
            self.assertTrue(permission_ended(error(code, "application/json")))
        for code, kind in ((403, "text/html; charset=UTF-8"), (404, "text/plain"), (502, "application/json")):
            self.assertFalse(permission_ended(error(code, kind)))

    def test_uploader_retries_through_cloudflare_403_and_truncated_response(self):
        import receiver_uploader
        batches = []

        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def answer(self, code, body, kind="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/input/append":
                    return self.answer(200, b'{"session": "s"}')
                batches.append(body["batch"])
                if len(batches) == 1:
                    self.answer(403, b"error code: 1020", "text/plain")
                elif len(batches) == 2:
                    self.send_response(200)
                    self.send_header("Content-Length", "99")
                    self.end_headers()
                    self.wfile.write(b"{")
                else:
                    self.answer(200, b'{"ok": true}')

        class FakeSerial:
            in_waiting = 1

            def __init__(self, *_args, **_kwargs):
                self.chunks = [(sample() + "\n").encode()]
                self.deadline = time.monotonic() + 15

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _length):
                if self.chunks:
                    return self.chunks.pop(0)
                if len(batches) >= 3 or time.monotonic() > self.deadline:
                    raise KeyboardInterrupt
                time.sleep(.02)
                return b""

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        queue = Path(self.temp.name) / "retry.db"
        argv = ["receiver_uploader.py", "--server", f"http://127.0.0.1:{origin.server_port}", "--port", "FAKE", "--queue", str(queue)]
        output = io.StringIO()
        try:
            with patch.object(receiver_uploader.sys, "argv", argv), patch.object(receiver_uploader.getpass, "getpass", return_value="bpp"), patch.object(receiver_uploader.serial, "Serial", FakeSerial), contextlib.redirect_stdout(output):
                receiver_uploader.main()
        finally:
            origin.shutdown()
            origin.server_close()
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(set(batches)), 1)
        self.assertNotIn("permission ended", output.getvalue())
        db = sqlite3.connect(queue)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
