import io
import json
from pathlib import Path
import tempfile
import shutil
import uuid
import threading
import time
import unittest
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from app import Store, Receiver, parse_packet, make_handler, FLIGHT_COLUMNS, COLUMNS
import app


def sample(seq=0, uptime=1000, alt=100, fix=1, blank_time=False):
    fields = [0, 1, "C1", seq, "" if blank_time else 12, "" if blank_time else 34, "" if blank_time else 56,
              uptime, 20, 1000, 50, fix, 38.99 if fix else "", -76.94 if fix else "", alt if fix else "", 10, 1, 1, 1, -70, 8]
    return ",".join(map(str, fields))


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp_base = Path(tempfile.gettempdir()).resolve()
        self.temp_path = self.temp_base / ("chippy-test-" + uuid.uuid4().hex)
        self.temp_path.mkdir()
        self.store = Store(self.temp_path / "test.db")
        self.sid = self.store.create_session("test")

    def tearDown(self):
        self.store.db.close()
        assert self.temp_path.resolve().is_relative_to(self.temp_base)
        shutil.rmtree(self.temp_path)

    def test_exact_receiver_format_and_ignored_status(self):
        self.assertEqual(parse_packet(sample())["latitude_deg"], 38.99)
        self.assertIsNone(parse_packet("# Radio: +OK"))
        self.assertIsNone(parse_packet("rx_uptime_ms,sender,version"))
        with self.assertRaises(ValueError):
            parse_packet("+RCV=1,5,hello,-50,8")

    def test_missing_fix_and_time_are_not_zero_coordinates(self):
        p = self.store.add(self.sid, sample(fix=0, blank_time=True))
        self.assertIsNone(p["hour_UTC"])
        self.assertFalse(p["has_position"])
        self.assertIsNone(p["latitude_deg"])
        self.assertIsNone(p["ascent_mps"])

    def test_ascent_uses_sample_time_not_receipt_time(self):
        first = self.store.add(self.sid, sample(), "2026-10-28T10:00:00+00:00")
        next_p = self.store.add(self.sid, sample(1, 6000, 125), "2026-10-28T10:10:00+00:00")
        self.assertIsNone(first["ascent_mps"])
        self.assertEqual(next_p["ascent_mps"], 5)
        # Missed transmissions still produce the correct average over sample time.
        self.assertEqual(self.store.add(self.sid, sample(4, 21000, 200))["ascent_mps"], 5)

    def test_duplicate_restart_and_out_of_order_do_not_spike(self):
        self.store.add(self.sid, sample(10, 10000, 100))
        duplicate = self.store.add(self.sid, sample(10, 10000, 100))
        self.assertTrue(duplicate["duplicate"])
        self.assertIsNone(duplicate["ascent_mps"])
        restarted = self.store.add(self.sid, sample(0, 1000, 5))
        self.assertEqual(restarted["boot"], 1)
        self.assertIsNone(restarted["ascent_mps"])

    def test_invalid_packets_rejected(self):
        for text in [sample().replace("38.99", "999"), sample().replace("20,1000", "nan,1000"), sample() + ",extra"]:
            with self.assertRaises(ValueError):
                self.store.add(self.sid, text)

    def test_sessions_are_isolated_and_saved(self):
        self.store.add(self.sid, sample())
        other = self.store.create_session("demo")
        self.assertEqual(self.store.packets(other), [])
        self.assertEqual(len(self.store.packets(self.sid)), 1)
        reopened = Store(self.temp_path / "test.db")
        self.assertEqual(len(reopened.packets(self.sid)), 1)
        reopened.db.close()

    def test_fragmented_serial_line_survives_read_timeouts(self):
        self.assertTrue(app.SERIAL_OK)
        port = app.serial.serial_for_url("loop://", timeout=0.03)
        receiver = Receiver(self.store)
        receiver.thread = threading.Thread(target=receiver.run_serial, args=(self.sid, port), daemon=True)
        receiver.thread.start()
        try:
            line = sample().encode()
            port.write(line[:30])
            time.sleep(0.08)  # Force serial timeouts in the middle of one packet.
            self.assertEqual(self.store.packets(self.sid), [])
            port.write(line[30:] + b"\r\n")
            deadline = time.monotonic() + 1
            while not self.store.packets(self.sid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(self.store.packets(self.sid)), 1)
        finally:
            receiver.stop()

    def test_import_sd_csv_computes_ascent_and_labels_estimated_time(self):
        rows = [sample(seq=0, uptime=1000, alt=100).split(",")[4:17], sample(seq=1, uptime=3000, alt=110).split(",")[4:17]]
        text = "\ufeff" + ",".join(FLIGHT_COLUMNS) + "\n" + "\n".join(",".join(row) for row in rows)
        sid, count = self.store.import_csv(text, "fulltest.csv")
        data = self.store.packets(sid)
        self.assertEqual(count, 2)
        self.assertEqual(data[1]["ascent_mps"], 5)
        self.assertEqual(data[0]["time_kind"], "estimated")
        self.assertEqual(data[0]["rssi_dBm"], None)
        self.assertEqual(len(self.store.packets(self.sid)), 0)

    def test_import_receiver_export_preserves_original_time(self):
        text = "received_at," + ",".join(COLUMNS) + ",ascent_mps\n2026-10-28T12:00:00+00:00," + sample() + ",999\n"
        sid, _ = self.store.import_csv(text, "received.csv")
        p = self.store.packets(sid)[0]
        self.assertEqual(p["received_at"], "2026-10-28T12:00:00+00:00")
        self.assertEqual(p["time_kind"], "received")
        self.assertIsNone(p["ascent_mps"])  # Recompute; do not trust imported derived values.

    def test_import_invalid_file_is_atomic(self):
        good = sample().split(",")[4:17]
        bad = good.copy()
        bad[8] = "999"  # Latitude.
        text = ",".join(FLIGHT_COLUMNS) + "\n" + ",".join(good) + "\n" + ",".join(bad)
        sessions_before = len(self.store.sessions())
        with self.assertRaisesRegex(ValueError, "row 3"):
            self.store.import_csv(text, "bad.csv")
        self.assertEqual(len(self.store.sessions()), sessions_before)

    def test_import_old_sd_format_without_utc(self):
        names = FLIGHT_COLUMNS[3:]
        row = sample(fix=0).split(",")[7:17]
        sid, _ = self.store.import_csv(",".join(names) + "\n" + ",".join(row), "old.csv")
        p = self.store.packets(sid)[0]
        self.assertIsNone(p["hour_UTC"])
        self.assertFalse(p["has_position"])

    def test_http_packets_export_and_local_access(self):
        self.store.add(self.sid, sample())
        receiver = Receiver(self.store)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, receiver))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            self.assertIn(b"CHIPPY", urlopen(base).read())
            data = json.load(urlopen(base + f"/api/packets?session={self.sid}"))
            self.assertEqual(len(data), 1)
            exported = urlopen(base + f"/api/export?session={self.sid}").read().decode("utf-8-sig")
            self.assertIn("received_at,rx_uptime_ms", exported)
            with self.assertRaises(HTTPError) as rejected:
                urlopen(Request(base + "/api/disconnect", data=b"{}", headers={"Origin": "https://example.com"}))
            self.assertEqual(rejected.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
