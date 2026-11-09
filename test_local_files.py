"""Visitors' files are parsed in the browser; keep that code in step with the server's."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from app import Store, COLUMNS, FLIGHT_COLUMNS
from mission import parse_kml, rectangle
from test_app import sample
from test_mission import KML

ROOT = Path(__file__).resolve().parent
DRIVER = r"""
const L = require(process.argv[1]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const attempt = fn => { try { return {ok: fn()}; } catch (e) { return {error: e.message}; } };
console.log(JSON.stringify({
  csv: cases.csv.map(text => attempt(() => L.parseFlightCsv(text))),
  rectangle: cases.rectangle.map(([corners, warning]) => attempt(() => L.rectangle(corners, warning))),
  kml: cases.kml.map(([tree, name]) => attempt(() => L.parseKmlRoot(tree, name))),
  overlay: [
    attempt(() => L.applyOverlay({revision: "", geofence: null, kml: 1}, {action: "geofence", corners: [[38, -77], [38, -76], [39, -77], [39, -76]]})),
    attempt(() => L.applyOverlay({revision: "", geofence: 1, kml: 1}, {action: "clear_kml"})),
    attempt(() => L.applyOverlay({}, {action: "bad"})),
  ],
}));
"""

HEADER = ",".join(FLIGHT_COLUMNS)
FULL = ",".join(COLUMNS)


def flight(seq, uptime, alt, fix=1):
    return ",".join(sample(seq, uptime, alt, fix).split(",")[4:17])


def other_sender():
    fields = sample(0, 2000, 50).split(",")
    fields[1] = "2"
    return ",".join(fields)


GOOD_ROW = flight(0, 1000, 100)
CSV_CASES = [
    (ROOT / "example_flight.csv").read_text(),
    "\ufeff" + HEADER + "\n" + GOOD_ROW + "\r\n" + flight(1, 3000, 110) + "\n# comment\n\n" + flight(2, 6000, 95),
    "received_at," + FULL + ",ascent_mps\n2026-10-28T12:00:00+00:00," + sample() + ",999\n2026-10-28T12:00:05Z," + sample(1, 6000, 125) + ",999\n",
    FULL + "\n" + sample(10, 10000, 100) + "\n" + sample(10, 10000, 100) + "\n" + sample(0, 1000, 5) + "\n" + sample(1, 6000, 30) + "\n" + other_sender(),
    ",".join(FLIGHT_COLUMNS[3:]) + "\n" + ",".join(sample(fix=0).split(",")[7:17]),
    HEADER + "\n" + ",".join('"' + v + '"' for v in GOOD_ROW.split(",")),
    "time_kind," + HEADER + "\nestimated," + GOOD_ROW,
    "",
    "# only a comment\n",
    "a,b\n1,2",
    HEADER,
    HEADER + "," + FLIGHT_COLUMNS[0] + "\n" + GOOD_ROW + ",1",
    HEADER + "\n1,2",
    HEADER + "\n" + GOOD_ROW + ",extra",
    HEADER + "\n" + GOOD_ROW.replace("38.99", "999"),
    HEADER + "\n" + GOOD_ROW.replace("38.99", "-999"),
    HEADER + "\n" + GOOD_ROW.replace("-76.94", "-200"),
    HEADER + "\n" + GOOD_ROW.replace("20,1000", "nan,1000"),
    HEADER + "\n" + GOOD_ROW.replace("20,1000", "x,1000"),
    HEADER + "\n" + GOOD_ROW.replace("12,34,56", "25,34,56"),
    "received_at," + FULL + "\n2026-10-28T12:00:00," + sample(),
    "sender," + HEADER + "\n-1," + GOOD_ROW,
]
RECTANGLE_CASES = [
    [[[39, -77], [38, -76], [39, -76], [38, -77]], 500],
    [[], 1000],
    [[[1, 1]] * 4, 1000],
    [[[1, 1], [2, 2], [2, 3], [1, 2]], 1000],
    [[[1, -179], [1, 179], [2, -179], [2, 179]], 1000],
    [[[1, 1], [1, 2], [2, 1], [2, 2]], -1],
    [[[100, 1], [100, 2], [2, 1], [2, 2]], 1000],
    [[["a", 1], [1, 2], [2, 1], [2, 2]], 1000],
    [[[True, 1], [1, 2], [2, 1], [2, 2]], 1000],
]
HOLE = '</outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>-76.99,39.01 -76.98,39.01 -76.98,39.02 -76.99,39.01</coordinates></LinearRing></innerBoundaryIs>'
KML_CASES = [
    KML,
    KML.replace("</outerBoundaryIs>", HOLE),
    '<kml><Placemark><MultiGeometry><Point><coordinates>1,2</coordinates></Point><Point><coordinates>3,4</coordinates></Point></MultiGeometry></Placemark></kml>',
    "<kml><NetworkLink/></kml>",
    "<kml><GroundOverlay/></kml>",
    "<kml><Placemark><Placemark><Point><coordinates>1,2</coordinates></Point></Placemark></Placemark></kml>",
    "<bad/>",
    "<kml/>",
    KML.replace("-77,39,30000", "-777,39,30000"),
    KML.replace("-77,39,30000", "NaN,39,30000"),
    KML.replace("-77,39,30000", "1,2,3,4"),
    KML.replace("-77,39 -76.9,39 -76.9,39.1 -77,39.1 -77,39", "-77,39 -76.9,39 -76.9,39.1 -77,39.1 -76,39"),
    KML.replace("<altitudeMode>absolute</altitudeMode>", "<altitudeMode>sideways</altitudeMode>"),
    "<kml><Placemark><Polygon/></Placemark></kml>",
    "<kml><Placemark><Point/></Placemark></kml>",
]


def tree(el):
    return {"localName": el.tag.split("}")[-1], "textContent": "".join(el.itertext()), "children": [tree(c) for c in el]}


def server_csv(text):
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / "db")
        try:
            sid, _ = store.import_csv(text, "x.csv")
            return {"ok": store.packets(sid)}
        except ValueError as exc:
            return {"error": str(exc)}
        finally:
            store.db.close()


def attempt(fn, *args):
    try:
        return {"ok": json.loads(json.dumps(fn(*args)))}
    except ValueError as exc:
        return {"error": str(exc)}


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BrowserParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = {"csv": CSV_CASES, "rectangle": RECTANGLE_CASES, "kml": [[tree(ET.fromstring(text)), "../../flight.kml"] for text in KML_CASES]}
        done = subprocess.run(["node", "-e", DRIVER, str(ROOT / "static" / "local_files.js")], input=json.dumps(cases), capture_output=True, text=True, check=True, timeout=60)
        cls.browser = json.loads(done.stdout)

    def assert_same_packets(self, server, browser):
        self.assertEqual(len(server), len(browser))
        for s, b in zip(server, browser):
            self.assertEqual(set(s), set(b))
            for key, value in s.items():
                if key == "received_at":
                    continue
                if isinstance(value, float):
                    self.assertAlmostEqual(value, b[key], places=9, msg=key)
                else:
                    self.assertEqual(value, b[key], key)
            if s["time_kind"] == "received":
                self.assertAlmostEqual(epoch(s["received_at"]), epoch(b["received_at"]), places=3)
        # Estimated times start at "now", so compare their spacing.
        for i, (s, b) in enumerate(zip(server, browser)):
            if s["time_kind"] == "estimated":
                self.assertAlmostEqual(epoch(s["received_at"]) - epoch(server[0]["received_at"]), epoch(b["received_at"]) - epoch(browser[0]["received_at"]), delta=.01, msg=f"row {i}")

    def test_flight_csv_matches_server(self):
        for text, browser in zip(CSV_CASES, self.browser["csv"]):
            with self.subTest(text[:60]):
                server = server_csv(text)
                self.assertEqual(server.get("error"), browser.get("error"))
                if "ok" in server:
                    self.assert_same_packets(server["ok"], browser["ok"])

    def test_rectangle_matches_server(self):
        for (corners, warning), browser in zip(RECTANGLE_CASES, self.browser["rectangle"]):
            with self.subTest(str(corners)):
                self.assertEqual(attempt(rectangle, corners, warning), browser)

    def test_kml_matches_server(self):
        for text, browser in zip(KML_CASES, self.browser["kml"]):
            with self.subTest(text[:60]):
                self.assertEqual(attempt(parse_kml, text, "../../flight.kml"), browser)

    def test_overlay_updates_only_the_selected_field(self):
        added, cleared, rejected = self.browser["overlay"]
        self.assertEqual(added["ok"]["geofence"]["south"], 38)
        self.assertEqual(added["ok"]["kml"], 1)
        self.assertIsNone(cleared["ok"]["kml"])
        self.assertEqual(cleared["ok"]["geofence"], 1)
        self.assertTrue(added["ok"]["revision"])
        self.assertEqual(rejected["error"], "Unknown map action")


if __name__ == "__main__":
    unittest.main()
