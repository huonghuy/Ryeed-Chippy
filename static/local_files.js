/* Private CSV replay and map overlays run here, so a visitor's files never reach the server.
   Mirrors app.py (parse_packet, import_csv, Store.add) and mission.py (rectangle, parse_kml);
   test_local_files.py fails if the two drift apart. */
(function(root) {
  "use strict";
  const geo = typeof module !== "undefined" && module.exports ? require("./geo.js") : root.MissionGeo;
  const COLUMNS = "rx_uptime_ms,sender,version,packet_seq,hour_UTC,minute_UTC,second_UTC,uptime_ms,temperature_C,pressure_hPa,humidity_pct,gps_fix,latitude_deg,longitude_deg,altitude_m,satellites,radio_at_ok,bme_ok,sd_write_ok,rssi_dBm,snr_dB".split(",");
  const FLIGHT_COLUMNS = COLUMNS.slice(4, 17);
  const INTEGER_FIELDS = new Set(["rx_uptime_ms", "sender", "packet_seq", "hour_UTC", "minute_UTC", "second_UTC", "uptime_ms", "gps_fix", "satellites", "radio_at_ok", "bme_ok", "sd_write_ok"]);
  const FLOAT = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i, NOT_FINITE = /^[+-]?(?:inf(?:inity)?|nan)$/i;
  const LINE_BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/;

  function splitCsvLine(line) {
    const out = []; let field = "", quoted = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (quoted) {
        if (c !== '"') field += c;
        else if (line[i + 1] === '"') { field += '"'; i++; }
        else quoted = false;
      } else if (c === '"' && field === "") quoted = true;
      else if (c === ",") { out.push(field); field = ""; }
      else field += c;
    }
    out.push(field);
    return out;
  }

  function numeric(key, raw) {
    const text = raw.trim();
    const valid = INTEGER_FIELDS.has(key) ? /^[+-]?\d+$/.test(text) : FLOAT.test(text) || NOT_FINITE.test(text);
    if (!valid) throw new Error(`Invalid numeric field: ${key}`);
    const n = Number(text);
    if (!Number.isFinite(n)) throw new Error(`Non-finite field: ${key}`);
    return n;
  }

  function parsePacket(raw) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || line.startsWith("rx_uptime_ms,")) return null;
    const values = splitCsvLine(line);
    if (values.length !== COLUMNS.length || values[2] !== "C1") throw new Error("Expected a 21-column C1 packet from CHIPPY_RECEIVER");
    const p = {};
    COLUMNS.forEach((key, i) => { p[key] = key === "version" ? values[i] : values[i] === "" ? null : numeric(key, values[i]); });
    for (const key of ["rx_uptime_ms", "sender", "packet_seq", "uptime_ms"]) if (p[key] === null || !(p[key] >= 0 && p[key] <= 4294967295)) throw new Error(`Invalid ${key}`);
    for (const key of ["gps_fix", "radio_at_ok", "bme_ok", "sd_write_ok"]) if (p[key] !== 0 && p[key] !== 1) throw new Error(`Invalid flag: ${key}`);
    for (const [key, high] of [["hour_UTC", 23], ["minute_UTC", 59], ["second_UTC", 60]]) if (p[key] !== null && !(p[key] >= 0 && p[key] <= high)) throw new Error(`Invalid ${key}`);
    if (p.latitude_deg !== null && !(p.latitude_deg >= -90 && p.latitude_deg <= 90)) throw new Error("Latitude out of range");
    if (p.longitude_deg !== null && !(p.longitude_deg >= -180 && p.longitude_deg <= 180)) throw new Error("Longitude out of range");
    p.has_position = Boolean(p.gps_fix && p.latitude_deg !== null && p.longitude_deg !== null);
    return p;
  }

  function derive(p, tracks) {
    p.ascent_mps = null; p.ground_speed_mps = null; p.duplicate = false;
    if (!tracks.has(p.sender)) tracks.set(p.sender, {boot: 0, uptime: null, seen: new Set(), alt: null, position: null});
    const t = tracks.get(p.sender), identity = `${p.packet_seq}/${p.uptime_ms}`;
    if (t.seen.has(identity)) p.duplicate = true;
    else {
      if (t.uptime !== null && p.uptime_ms < t.uptime) { t.boot++; t.alt = null; t.position = null; t.seen.clear(); }
      t.seen.add(identity); t.uptime = p.uptime_ms;
      if (p.has_position) {
        const previous = t.position;
        if (previous && p.uptime_ms > previous[0]) p.ground_speed_mps = geo.distance([previous[1], previous[2]], [p.latitude_deg, p.longitude_deg]) / ((p.uptime_ms - previous[0]) / 1000);
        t.position = [p.uptime_ms, p.latitude_deg, p.longitude_deg];
      }
      if (p.has_position && p.altitude_m !== null) {
        const previous = t.alt;
        if (previous && p.uptime_ms > previous[0]) p.ascent_mps = (p.altitude_m - previous[1]) / ((p.uptime_ms - previous[0]) / 1000);
        t.alt = [p.uptime_ms, p.altitude_m];
      }
    }
    p.boot = t.boot;
  }

  function parseFlightCsv(text, now = Date.now()) {
    const lines = text.replace(/^\ufeff+/, "").split(LINE_BREAK).filter(line => line.trim() && !line.trimStart().startsWith("#"));
    if (!lines.length) throw new Error("This CSV is empty.");
    const header = splitCsvLine(lines[0]).map(name => name.trim());
    const missing = FLIGHT_COLUMNS.filter(c => !["hour_UTC", "minute_UTC", "second_UTC"].includes(c) && !header.includes(c)).sort();
    if (missing.length) throw new Error("Missing flight CSV columns: " + missing.join(", "));
    if (new Set(header).size !== header.length) throw new Error("The CSV has duplicate column headers.");
    const rows = [];
    let elapsed = 0, previousUptime = null;
    lines.slice(1).forEach((rowLine, index) => {
      const number = index + 2;
      if (rows.length >= 50000) throw new Error("Limit is 50,000 rows per flight CSV.");
      const cells = splitCsvLine(rowLine);
      if (cells.length !== header.length) throw new Error(`CSV row ${number}: column count does not match the header.`);
      const row = new Map(header.map((name, i) => [name, cells[i]]));
      const value = (key, fallback = "") => (row.get(key) ?? fallback).trim();
      const fields = [value("rx_uptime_ms", "0"), value("sender", "1"), value("version", "C1"), value("packet_seq", String(rows.length)), ...FLIGHT_COLUMNS.map(key => value(key)), value("bme_ok", value("temperature_C") ? "1" : "0"), value("sd_write_ok", "1"), value("rssi_dBm"), value("snr_dB")];
      let packet, timestamp;
      try {
        packet = parsePacket(fields.join(","));
        if (!packet) throw new Error("No packet data");
        timestamp = value("received_at");
        if (timestamp) {
          if (!/(?:z|[+-]\d{2}(?::?\d{2})?)$/i.test(timestamp)) throw new Error("received_at needs a timezone, for example +00:00");
          if (Number.isNaN(Date.parse(timestamp))) throw new Error(`Invalid received_at: ${timestamp}`);
          timestamp = new Date(timestamp).toISOString();
        }
      } catch (e) { throw new Error(`CSV row ${number}: ${e.message}`); }
      if (previousUptime !== null) elapsed += packet.uptime_ms >= previousUptime ? (packet.uptime_ms - previousUptime) / 1000 : 1;
      previousUptime = packet.uptime_ms;
      rows.push({packet, timestamp, kind: !timestamp || value("time_kind") === "estimated" ? "estimated" : "received", offset: elapsed});
    });
    if (!rows.length) throw new Error("No data rows were found in the CSV.");
    const base = now / 1000 - elapsed, tracks = new Map();
    return rows.map(({packet, timestamp, kind, offset}, i) => {
      packet.received_at = timestamp || new Date((base + offset) * 1000).toISOString();
      packet.time_kind = kind;
      derive(packet, tracks);
      packet.id = i + 1;
      return packet;
    });
  }

  function number(value, low, high) {
    if (typeof value === "boolean") throw new Error("Coordinates must be numbers");
    let n = value;
    if (typeof value === "string") {
      const text = value.trim();
      n = FLOAT.test(text) || NOT_FINITE.test(text) ? Number(text) : undefined;
    }
    if (typeof n !== "number") throw new Error("Enter a valid number");
    if (!Number.isFinite(n) || !(n >= low && n <= high)) throw new Error(`Value must be between ${low} and ${high}`);
    return n;
  }

  function rectangle(corners, warningM = 1000) {
    if (!Array.isArray(corners) || corners.length !== 4 || corners.some(p => !Array.isArray(p) || p.length !== 2)) throw new Error("Enter four corner pairs: latitude, longitude");
    const pts = corners.map(p => [number(p[0], -89, 89), number(p[1], -180, 180)]);
    const sorted = i => [...new Set(pts.map(p => p[i]))].sort((a, b) => a - b), lats = sorted(0), lons = sorted(1);
    if (new Set(pts.map(String)).size !== 4 || lats.length !== 2 || lons.length !== 2) throw new Error("The four corners must form a latitude/longitude-aligned rectangle");
    if (lons[1] - lons[0] >= 180 || Math.min(lats[1] - lats[0], lons[1] - lons[0]) < .000001) throw new Error("Use a nonzero rectangle spanning less than 180° longitude");
    return {south: lats[0], north: lats[1], west: lons[0], east: lons[1], warning_m: number(warningM, 0, 100000), corners: [[lats[0], lons[0]], [lats[0], lons[1]], [lats[1], lons[1]], [lats[1], lons[0]]]};
  }

  const kids = (el, name) => [...el.children].filter(c => c.localName === name);
  function* walk(el) { yield el; for (const c of el.children) yield* walk(c); }

  // Separate from parseKml so the Node parity test can pass element-shaped objects.
  function parseKmlRoot(root, name) {
    const nodes = [...walk(root)];
    if (nodes.length > 100000) throw new Error("KML is too complex; simplify the document");
    if (root.localName !== "kml") throw new Error("This is not a KML document");
    const forbidden = new Set(["NetworkLink", "GroundOverlay", "ScreenOverlay", "Model", "Tour", "Track"]);
    if (nodes.some(el => forbidden.has(el.localName))) throw new Error("Use self-contained KML Points, LineStrings, and Polygons. Network links, images, models, and tracks are not supported.");
    const features = [];
    let count = 0;
    const coordinates = (el, minimum) => {
      const node = el ? kids(el, "coordinates")[0] : undefined;
      const text = node ? node.textContent : "";
      if (!text) throw new Error("A KML geometry is missing coordinates");
      const points = text.trim().split(/\s+/).filter(Boolean).map(token => {
        const values = token.split(",");
        if (values.length < 2 || values.length > 3) throw new Error("KML coordinates use longitude,latitude[,altitude]");
        return [number(values[0], -180, 180), number(values[1], -90, 90), values.length === 3 && values[2] ? number(values[2], -12000, 10000000) : 0];
      });
      count += points.length;
      if (count > 20000 || points.length < minimum) throw new Error("KML needs valid geometry and is limited to 20,000 coordinate vertices");
      return points;
    };
    const placemarks = nodes.filter(el => el.localName === "Placemark");
    if (placemarks.length > 1000) throw new Error("Limit is 1,000 KML placemarks");
    for (const placemark of placemarks) {
      const inside = [...walk(placemark)];
      if (inside.slice(1).some(el => el.localName === "Placemark")) throw new Error("Nested KML placemarks are not supported");
      const title = (kids(placemark, "name")[0]?.textContent || "KML feature").slice(0, 150);
      for (const el of inside) {
        if (!["Point", "LineString", "Polygon"].includes(el.localName)) continue;
        const mode = kids(el, "altitudeMode")[0]?.textContent || "clampToGround";
        if (!["absolute", "clampToGround", "relativeToGround"].includes(mode)) throw new Error("Unsupported KML altitude mode");
        let geometry;
        if (el.localName === "Point") {
          const pts = coordinates(el, 1);
          if (pts.length !== 1) throw new Error("A KML Point must have one coordinate");
          geometry = {type: "Point", coordinates: pts[0]};
        } else if (el.localName === "LineString") geometry = {type: "LineString", coordinates: coordinates(el, 2)};
        else {
          const rings = side => kids(el, side).flatMap(b => kids(b, "LinearRing")), outer = rings("outerBoundaryIs");
          if (!outer.length) throw new Error("KML polygon needs an outer boundary");
          geometry = {type: "Polygon", coordinates: [...outer, ...rings("innerBoundaryIs")].map(ring => {
            const pts = coordinates(ring, 4);
            if (String(pts[0]) !== String(pts.at(-1)) || new Set(pts.map(p => `${p[0]},${p[1]}`)).size < 3) throw new Error("KML polygon rings must be closed with at least three distinct vertices");
            return pts;
          })};
        }
        features.push({type: "Feature", geometry, properties: {name: title, altitude_mode: mode}});
        if (features.length > 1000) throw new Error("Limit is 1,000 KML features");
      }
    }
    if (!features.length) throw new Error("No supported KML points, lines, or polygons were found");
    return {name: String(name).replace(/\\/g, "/").split("/").at(-1).slice(0, 100), type: "FeatureCollection", features};
  }

  function parseKml(text, name) {
    if (/<!(?:DOCTYPE|ENTITY)/i.test(text)) throw new Error("KML document types and entities are not supported");
    const doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.querySelector("parsererror")) throw new Error("Invalid KML XML");
    return parseKmlRoot(doc.documentElement, name);
  }

  function applyOverlay(config, body) {
    let value = null;
    if (body.action === "geofence") value = rectangle(body.corners, body.warning_m ?? 1000);
    else if (body.action === "kml") value = parseKml(body.kml, body.name ?? "overlay.kml");
    else if (body.action !== "clear_geofence" && body.action !== "clear_kml") throw new Error("Unknown map action");
    return {...config, [body.action.replace(/^clear_/, "")]: value, revision: new Date().toISOString()};
  }

  const api = {parseFlightCsv, rectangle, parseKml, parseKmlRoot, applyOverlay};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LocalFiles = api;
})(typeof window !== "undefined" ? window : globalThis);
