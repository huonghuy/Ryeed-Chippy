"""Map-only geofences, safe local KML parsing, and FAA reference overlays.

This module has no receiver write or cutdown command capability.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


def number(value, low, high):
    if isinstance(value, bool):
        raise ValueError("Coordinates must be numbers")
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError("Enter a valid number") from None
    if not math.isfinite(n) or not low <= n <= high:
        raise ValueError(f"Value must be between {low} and {high}")
    return n


def rectangle(corners, warning_m=1000):
    if not isinstance(corners, list) or len(corners) != 4 or any(not isinstance(p, list) or len(p) != 2 for p in corners):
        raise ValueError("Enter four corner pairs: latitude, longitude")
    pts = [(number(p[0], -89, 89), number(p[1], -180, 180)) for p in corners]
    lats, lons = sorted({p[0] for p in pts}), sorted({p[1] for p in pts})
    if len(set(pts)) != 4 or len(lats) != 2 or len(lons) != 2:
        raise ValueError("The four corners must form a latitude/longitude-aligned rectangle")
    if lons[1] - lons[0] >= 180 or min(lats[1] - lats[0], lons[1] - lons[0]) < .000001:
        raise ValueError("Use a nonzero rectangle spanning less than 180° longitude")
    return {"south": lats[0], "north": lats[1], "west": lons[0], "east": lons[1], "warning_m": number(warning_m, 0, 100000), "corners": [[lats[0], lons[0]], [lats[0], lons[1]], [lats[1], lons[1]], [lats[1], lons[0]]]}


def parse_kml(text, name):
    if not isinstance(text, str) or len(text.encode()) > 4 * 1024 * 1024:
        raise ValueError("Choose a KML file up to 4 MB")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("KML document types and entities are not supported")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML XML: {exc}") from None
    nodes = list(root.iter())
    if len(nodes) > 100000:
        raise ValueError("KML is too complex; simplify the document")
    for el in nodes:
        el.tag = el.tag.split("}")[-1]
    if root.tag != "kml":
        raise ValueError("This is not a KML document")
    forbidden = {"NetworkLink", "GroundOverlay", "ScreenOverlay", "Model", "Tour", "Track"}
    if any(el.tag in forbidden for el in root.iter()):
        raise ValueError("Use self-contained KML Points, LineStrings, and Polygons. Network links, images, models, and tracks are not supported.")
    features = []
    count = 0

    def coordinates(el, minimum):
        nonlocal count
        node = el.find("coordinates") if el is not None else None
        if node is None or not node.text:
            raise ValueError("A KML geometry is missing coordinates")
        points = []
        for token in node.text.split():
            values = token.split(",")
            if not 2 <= len(values) <= 3:
                raise ValueError("KML coordinates use longitude,latitude[,altitude]")
            points.append([number(values[0], -180, 180), number(values[1], -90, 90), number(values[2], -12000, 10000000) if len(values) == 3 and values[2] else 0])
        count += len(points)
        if count > 20000 or len(points) < minimum:
            raise ValueError("KML needs valid geometry and is limited to 20,000 coordinate vertices")
        return points

    placemarks = list(root.iter("Placemark"))
    if len(placemarks) > 1000:
        raise ValueError("Limit is 1,000 KML placemarks")
    for placemark in placemarks:
        if any(el.tag == "Placemark" for el in list(placemark.iter())[1:]):
            raise ValueError("Nested KML placemarks are not supported")
        title = (placemark.findtext("name") or "KML feature")[:150]
        for el in placemark.iter():
            if el.tag not in ("Point", "LineString", "Polygon"):
                continue
            mode = el.findtext("altitudeMode") or "clampToGround"
            if mode not in ("absolute", "clampToGround", "relativeToGround"):
                raise ValueError("Unsupported KML altitude mode")
            if el.tag == "Point":
                pts = coordinates(el, 1)
                if len(pts) != 1:
                    raise ValueError("A KML Point must have one coordinate")
                geometry = {"type": "Point", "coordinates": pts[0]}
            elif el.tag == "LineString":
                geometry = {"type": "LineString", "coordinates": coordinates(el, 2)}
            else:
                rings = []
                boundaries = el.findall("outerBoundaryIs/LinearRing") + el.findall("innerBoundaryIs/LinearRing")
                if not el.findall("outerBoundaryIs/LinearRing"):
                    raise ValueError("KML polygon needs an outer boundary")
                for ring in boundaries:
                    pts = coordinates(ring, 4)
                    if pts[0] != pts[-1] or len({tuple(p[:2]) for p in pts}) < 3:
                        raise ValueError("KML polygon rings must be closed with at least three distinct vertices")
                    rings.append(pts)
                geometry = {"type": "Polygon", "coordinates": rings}
            features.append({"type": "Feature", "geometry": geometry, "properties": {"name": title, "altitude_mode": mode}})
            if len(features) > 1000:
                raise ValueError("Limit is 1,000 KML features")
    if not features:
        raise ValueError("No supported KML points, lines, or polygons were found")
    filename = str(name).replace("\\", "/").split("/")[-1][:100]
    return {"name": filename, "type": "FeatureCollection", "features": features}


class MapConfig:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS map_configs (owner TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def get(self, owner):
        with self.store.lock:
            row = self.store.db.execute("SELECT body FROM map_configs WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else {"revision": "", "geofence": None, "kml": None}

    def update(self, owner, body):
        action = body.get("action")
        if action == "geofence":
            value = rectangle(body.get("corners"), body.get("warning_m", 1000))
        elif action == "kml":
            value = parse_kml(body.get("kml"), body.get("name", "overlay.kml"))
        elif action in ("clear_geofence", "clear_kml"):
            value = None
        else:
            raise ValueError("Unknown map action")
        field = action.removeprefix("clear_")
        with self.store.lock, self.store.db:
            config = self.get(owner)
            config[field] = value
            config["revision"] = datetime.now(timezone.utc).isoformat()
            self.store.db.execute("INSERT OR REPLACE INTO map_configs VALUES (?,?)", (owner, json.dumps(config, allow_nan=False)))
        return config


def altitude(properties, prefix):
    code = str(properties.get(prefix + "_CODE", "")).upper()
    uom = str(properties.get(prefix + "_UOM", "")).upper()
    if code in ("SFC", "GND"):
        return 0.0
    if code != "MSL":
        return None  # Do not turn AGL/pressure/unlimited values into MSL volumes.
    try:
        val = float(properties[prefix + "_VAL"])
        if uom == "FT":
            val *= .3048
        elif uom not in ("M", "METERS"):
            return None
        return val if math.isfinite(val) else None
    except (KeyError, ValueError, TypeError):
        return None


class Airspace:
    BASE = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    TFR = "https://tfr.faa.gov/geoserver/TFR/ows"
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    @staticmethod
    def fetch(url):
        request = Request(url, headers={"User-Agent": "CHIPPY-Reference-Map/1.0", "Accept": "application/json"})
        with urlopen(request, timeout=12) as response:
            content = response.read(12 * 1024 * 1024 + 1)
        if len(content) > 12 * 1024 * 1024:
            raise ValueError("Airspace response exceeds map limit; zoom in")
        data = json.loads(content)
        if "error" in data or data.get("type") != "FeatureCollection":
            raise ValueError("FAA source did not return map geometry")
        return data

    def layer(self, kind, bounds):
        key = (kind, tuple(bounds))
        ttl = 60 if kind == "tfr" else 300
        with self.lock:
            cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        west, south, east, north = bounds
        params = dict(f="geojson", where="CLASS IN ('B','C','D')" if kind == "controlled" else "1=1", geometry=f"{west},{south},{east},{north}", geometryType="esriGeometryEnvelope", inSR=4326, outSR=4326, spatialRel="esriSpatialRelIntersects", outFields="*", returnGeometry="true", resultRecordCount=2000)
        if kind == "tfr":
            params = dict(service="WFS", version="1.0.0", request="GetFeature", typeName="TFR:V_TFR_LOC", maxFeatures=2000, outputFormat="application/json", srsName="EPSG:4326", bbox=f"{west},{south},{east},{north},EPSG:4326")
            url = self.TFR
        else:
            url = self.BASE + ("Class_Airspace" if kind == "controlled" else "Special_Use_Airspace") + "/FeatureServer/0/query"
        try:
            data = self.fetch(url + "?" + urlencode(params))
            features = data.get("features", [])
            for feature in features:
                props = feature.setdefault("properties", {})
                props.update(layer=kind, name=props.get("NAME") or props.get("TITLE") or "FAA airspace", floor_m=altitude(props, "LOWER"), ceiling_m=altitude(props, "UPPER"))
            result = {"kind": kind, "features": features, "fetched_at": datetime.now(timezone.utc).isoformat(), "stale": False, "truncated": bool(data.get("exceededTransferLimit")) or len(features) >= 2000, "error": None}
            with self.lock:
                if len(self.cache) > 64:
                    self.cache.clear()
                self.cache[key] = (time.monotonic(), result)
            return result
        except Exception as exc:
            if cached:
                return {**cached[1], "stale": True, "error": str(exc)}
            return {"kind": kind, "features": [], "fetched_at": None, "stale": True, "error": str(exc), "truncated": False}

    def query(self, bounds, kinds):
        if len(bounds) != 4:
            raise ValueError("Airspace needs west,south,east,north bounds")
        west, south, east, north = [number(v, -180 if i % 2 == 0 else -90, 180 if i % 2 == 0 else 90) for i, v in enumerate(bounds)]
        if not 0 < east - west <= 8 or not 0 < north - south <= 8:
            raise ValueError("Zoom in: airspace queries cover at most 8° latitude/longitude and cannot cross the dateline")
        if not kinds or any(k not in ("controlled", "sua", "tfr") for k in kinds):
            raise ValueError("Choose controlled, sua, or tfr airspace")
        bounds = [round(v, 3) for v in (west, south, east, north)]
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError("Airspace region is too small; zoom out slightly")
        with ThreadPoolExecutor(max_workers=3) as pool:
            layers = list(pool.map(lambda k: self.layer(k, bounds), sorted(set(kinds))))
        return {"bounds": bounds, "layers": layers, "source": "FAA AIS / FAA TFR", "reference_only": True}
