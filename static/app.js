"use strict";
const $ = id => document.getElementById(id);
let sourceMode = "follow", followingLive = true;
let packets = [], session = "", selected = null, autoSelect = true, state = {}, markers = new Map(), paths = new Map(), map = null, lastId = 0, switching = false;
let replayMode = false, flightPackets = [], replayIndex = 0, replayPlaying = false, replayElapsed = 0, replayLastTick = 0;
const fmt = (n, digits = 1) => n == null ? "—" : Number(n).toFixed(digits);
const clock = iso => iso ? new Date(iso).toLocaleTimeString([], {hour12: false}) : "—";
const dateTime = iso => iso ? new Date(iso).toLocaleString() : "—";
const unit = (n, suffix, digits = 1) => n == null ? "—" : `${fmt(n, digits)} ${suffix}`;
const error = msg => { $("error").textContent = msg || ""; $("error").hidden = !msg; };
async function api(path, body) {
  const response = await fetch(path, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}
function setupMap() {
  if (!window.L) { $("mapEmpty").textContent = "Map library unavailable. Packet logging still works."; return; }
  map = L.map("map", {zoomControl: true}).setView([38.985, -76.942], 12);
  const tiles = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {maxZoom: 19, attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, USGS and contributors'}).addTo(map);
  tiles.on("tileerror", () => $("tileStatus").textContent = "Background unavailable · check internet");
  tiles.on("load", () => { if (!navigator.onLine) return; $("tileStatus").textContent = "Esri · online street map"; });
}
function popup(p) {
  const root = document.createElement("div");
  const title = document.createElement("div"); title.className = "popup-title"; title.textContent = `Packet #${p.packet_seq}`;
  const info = document.createElement("div"); info.className = "popup-data";
  for (const text of [`Ground speed ${unit(p.ground_speed_mps, "m/s", 2)}`, `Temperature ${unit(p.temperature_C, "°C")}`, `Altitude ${unit(p.altitude_m, "m")}`, `Ascent ${unit(p.ascent_mps, "m/s", 2)}`, `${p.time_kind === "estimated" ? "Replay time" : "Received"} ${clock(p.received_at)} (local)`]) {
    const row = document.createElement("div"); row.textContent = text; info.append(row);
  }
  root.append(title, info); return root;
}
function addToMap(p) {
  if (!map || !p.has_position || p.duplicate) return;
  const point = [p.latitude_deg, p.longitude_deg];
  const key = `${p.sender}/${p.boot}`;
  if (!paths.has(key)) paths.set(key, L.polyline([], {color: "#28694e", weight: 3, opacity: .75}).addTo(map));
  paths.get(key).addLatLng(point);
  const marker = L.circleMarker(point, {radius: 5, weight: 2, color: "#fff", fillColor: "#28694e", fillOpacity: 1}).addTo(map).bindPopup(popup(p));
  marker.on("click", () => { autoSelect = false; selectPacket(p.id, false); });
  markers.set(p.id, marker);
}
function selectPacket(id, openMap = true) {
  selected = id;
  const p = packets.find(x => x.id === id); if (!p) return;
  $("detailTitle").textContent = `Packet #${p.packet_seq}`;
  $("fixBadge").textContent = p.has_position ? "GPS FIX" : "NO FIX";
  $("detailIntro").textContent = `${p.time_kind === "estimated" ? "Estimated replay time" : "Received"} ${dateTime(p.received_at)} · local time`;
  const utc = [p.hour_UTC, p.minute_UTC, p.second_UTC].every(x => x != null) ? [p.hour_UTC, p.minute_UTC, p.second_UTC].map(x => String(x).padStart(2, "0")).join(":") + " UTC" : "Unavailable";
  const rows = [["Temperature", unit(p.temperature_C, "°C")], ["GPS altitude", unit(p.altitude_m, "m")], ["Ascent rate", unit(p.ascent_mps, "m/s", 2)], ["Ground speed", unit(p.ground_speed_mps, "m/s", 2)], ["Latitude", fmt(p.latitude_deg, 6)], ["Longitude", fmt(p.longitude_deg, 6)], ["GPS time", utc], ["Sample uptime", unit(p.uptime_ms / 1000, "s")], ["Humidity", unit(p.humidity_pct, "%")], ["Pressure", unit(p.pressure_hPa, "hPa")], ["Satellites", p.satellites ?? "—"], ["Signal", `${fmt(p.rssi_dBm, 0)} dBm / ${fmt(p.snr_dB, 0)} dB`], ["Sensor / SD", `${p.bme_ok ? "OK" : "FAIL"} / ${p.sd_write_ok ? "OK" : "FAIL"}`]];
  $("details").replaceChildren(...rows.map(([name, value]) => { const wrap = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = name; dd.textContent = value; wrap.append(dt, dd); return wrap; }));
  $("detailFoot").textContent = p.duplicate ? "Duplicate packet. Excluded from ascent calculations and path." : p.ascent_mps == null ? "Ascent needs two valid altitude samples from the same balloon run." : "Ascent is the altitude change divided by balloon sample time. Negative means descent.";
  if (p.time_kind === "estimated") $("detailFoot").textContent += " SD logs do not contain a ground reception timestamp; replay time is estimated.";
  document.querySelectorAll("#packetRows tr").forEach(row => row.classList.toggle("selected", Number(row.dataset.id) === id));
  markers.forEach((marker, key) => marker.setStyle({radius: key === id ? 8 : 5, fillColor: key === id ? "#b6d95d" : "#28694e", color: key === id ? "#19362e" : "#fff"}));
  window.flightGlobe?.highlight(p, openMap);
  if (openMap && markers.has(id)) { markers.get(id).openPopup(); map.panTo(markers.get(id).getLatLng()); }
}
function averageAscentRate(data, sender) {
  const previousByBoot = new Map();
  let altitudeChange = 0, elapsedSeconds = 0;
  for (const p of data) {
    if (p.sender !== sender || p.duplicate || !p.has_position || !Number.isFinite(p.altitude_m) || !Number.isFinite(p.uptime_ms)) continue;
    const previous = previousByBoot.get(p.boot);
    if (previous && p.uptime_ms > previous.uptime_ms) {
      altitudeChange += p.altitude_m - previous.altitude_m;
      elapsedSeconds += (p.uptime_ms - previous.uptime_ms) / 1000;
    }
    // Ignore equal/out-of-order samples; a new boot gets its own baseline.
    if (!previous || p.uptime_ms > previous.uptime_ms) previousByBoot.set(p.boot, p);
  }
  return elapsedSeconds > 0 ? altitudeChange / elapsedSeconds : null;
}
function renderPackets() {
  const newest = packets.at(-1);
  $("altitude").replaceChildren(document.createTextNode(fmt(newest?.altitude_m) + " "), small("m"));
  $("altitudeFeet").textContent = unit(newest?.altitude_m == null ? null : newest.altitude_m / 0.3048, "ft");
  $("ascent").replaceChildren(document.createTextNode(fmt(newest?.ascent_mps, 2) + " "), small("m/s"));
  const entireFlight = replayMode && !followingLive ? flightPackets : packets;
  $("averageAscent").textContent = unit(newest ? averageAscentRate(entireFlight, newest.sender) : null, "m/s", 2);
  $("groundSpeed").replaceChildren(document.createTextNode(fmt(newest?.ground_speed_mps, 2) + " "), small("m/s"));
  $("temperature").replaceChildren(document.createTextNode(fmt(newest?.temperature_C) + " "), small("°C"));
  $("lastReceived").textContent = newest ? clock(newest.received_at) : "Waiting";
  $("timeHeader").textContent = replayMode && packets.some(p => p.time_kind === "estimated") ? "REPLAY TIME · ESTIMATED" : "RECEIVED · LOCAL";
  $("packetCount").textContent = `${packets.length} ${replayMode ? "shown" : "received"}`;
  $("mapCount").textContent = `${markers.size} positions`;
  $("mapEmpty").hidden = markers.size > 0;
  $("export").disabled = !session || !packets.length;
  const lastPos = [...packets].reverse().find(p => p.has_position);
  $("coordinates").textContent = lastPos ? `${fmt(lastPos.latitude_deg, 6)}°, ${fmt(lastPos.longitude_deg, 6)}°` : "No position yet";
  window.flightGlobe?.render(packets);
  window.missionMap?.updatePosition();
  if (!packets.length) return;
  $("packetRows").replaceChildren(...packets.slice(-500).reverse().map(p => {
    const tr = document.createElement("tr"); tr.dataset.id = p.id; tr.tabIndex = 0; tr.setAttribute("aria-label", `Inspect packet ${p.packet_seq}`);
    const values = [`#${p.packet_seq}${p.duplicate ? " · duplicate" : ""}`, clock(p.received_at), p.has_position ? `${fmt(p.latitude_deg, 5)}, ${fmt(p.longitude_deg, 5)}` : "No GPS fix", unit(p.altitude_m, "m"), unit(p.ascent_mps, "m/s", 2), unit(p.ground_speed_mps, "m/s", 2), unit(p.temperature_C, "°C"), unit(p.rssi_dBm, "dBm", 0)];
    values.forEach(value => { const td = document.createElement("td"); td.textContent = value; tr.append(td); });
    const choose = () => { autoSelect = false; selectPacket(p.id); };
    tr.onclick = choose; tr.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); } }; return tr;
  }));
  $("tableHint").textContent = packets.length > 500 ? "Showing latest 500 rows · CSV includes all packets" : "Newest first · click any row to inspect";
  if (autoSelect || !packets.some(p => p.id === selected)) selectPacket(newest.id, false);
  else selectPacket(selected, false);
}
function small(text) { const e = document.createElement("small"); e.textContent = text; return e; }
function clearView() {
  packets = []; lastId = 0; selected = null; autoSelect = true;
  markers.forEach(m => map?.removeLayer(m)); paths.forEach(p => map?.removeLayer(p)); markers.clear(); paths.clear();
  $("packetRows").replaceChildren();
  const tr = document.createElement("tr"), td = document.createElement("td"); td.colSpan = 8; td.className = "empty-row"; td.textContent = "Waiting for packets in this session."; tr.append(td); $("packetRows").append(tr);
  $("details").replaceChildren(); $("detailTitle").textContent = "Select a packet"; $("detailIntro").textContent = "Choose a map point or a row below."; $("fixBadge").textContent = "—";
  renderPackets();
}
async function switchSession(id) {
  replayPlaying = false;
  window.resetMonitor?.();
  if (state.sessions?.find(s => s.id === id)?.source.startsWith("CSV · ")) return loadReplay(id);
  replayMode = false; $("replayControls").hidden = true;
  session = id; clearView(); await poll();
}
function updateStatus() {
  const latest = packets.at(-1);
  const age = latest ? Math.max(0, (Date.now() - Date.parse(latest.received_at)) / 1000) : null;
  $("lastAge").textContent = age == null ? "No packets in this session" : `${Math.floor(age)} s since receipt · local time`;
  let text = followingLive ? "Shared feed · waiting" : "Private input", style = "";
  if (followingLive && latest) { text = age > 15 ? "Shared feed · packets stale" : "Shared feed · receiving"; style = age > 15 ? "stale" : "live"; }
  if (!followingLive && latest) { text = "Private input · receiving"; style = "live"; }
  if (state.sessions?.find(s => s.id === session)?.source === "Demo · private") { text = "Demo · simulated flight"; style = "demo"; }
  if (replayMode) { text = followingLive ? "Shared flight · recorded CSV" : replayPlaying ? "CSV · replaying" : "CSV · paused"; style = "demo";
    $("lastAge").textContent = latest?.time_kind === "estimated" ? "Estimated replay time · not actual reception" : "Original receipt time · local timezone"; }
  $("connectionBadge").textContent = text; $("connectionBadge").className = `badge ${style}`;
}
let polling = false;
async function poll() {
  if (polling || switching) return;
  polling = true;
  try {
    state = await api("/api/state");
    window.updateAdmin?.(state.admin);
    if (followingLive && session !== (state.live_session || "")) {
      session = state.live_session || ""; replayPlaying = false; replayMode = false; $("replayControls").hidden = true; clearView();
      window.resetMonitor?.();
    }
    $("feedStatus").textContent = state.live_session ? "Following the admin’s latest published input." : "Waiting for an admin to publish.";
    const old = $("session").value;
    $("session").replaceChildren(...state.sessions.map(s => new Option(`${s.source} · ${new Date(s.created).toLocaleString()} (${s.count})`, s.id)));
    $("session").value = session || old;
    const isCsv = state.sessions.find(s => s.id === session)?.source.startsWith("CSV · ");
    $("notice").textContent = followingLive ? (isCsv ? "Shared recorded flight — published by admin." : "Following shared live input. Updates appear automatically.") : replayMode ? "Private CSV replay — use Play or Show full flight." : "Private input — only this browser and administrators can access it.";
    if (state.sessions.find(s => s.id === session)?.source === "Demo · private") $("notice").textContent = "DEMO — simulated flight data. This is a private demonstration, not a real flight.";
    if (session && followingLive && isCsv && !replayMode) {
      // Shared CSV viewers see the complete published track immediately.
      const id = session;
      let after = 0, all = [];
      for (;;) {
        const batch = await api(`/api/packets?session=${encodeURIComponent(id)}&after=${after}`);
        if (!batch.length) break;
        all.push(...batch); after = batch.at(-1).id;
      }
      if (session === id && followingLive) {
        replayMode = true; clearView(); packets = all; packets.forEach(addToMap); renderPackets(); $("fit").click();
      }
    }
    if (session && !replayMode) {
      const requested = session;
      const incoming = await api(`/api/packets?session=${encodeURIComponent(session)}&after=${lastId}`);
      if (requested === session && incoming.length) {
        packets.push(...incoming); lastId = incoming.at(-1).id;
        incoming.forEach(addToMap); renderPackets();
        const p = [...incoming].reverse().find(p => p.has_position);
        if (map && p && $("follow").checked && autoSelect) map.setView([p.latitude_deg, p.longitude_deg], Math.max(map.getZoom(), 12));
      }
    }
    await window.pollMonitor?.(session);
    await window.missionMap?.pollConfig();
    updateStatus();
  } catch (e) { error(`Dashboard connection interrupted: ${e.message}. Check that the Python window is running.`); }
  finally { polling = false; }
}
$("session").onchange = () => { followingLive = false; window.resetMonitor?.(); switchSession($("session").value); };
$("export").onclick = () => { window.location.href = `/api/export?session=${encodeURIComponent(session)}`; };
$("follow").onchange = () => { if ($("follow").checked) autoSelect = true; };
$("fit").onclick = () => { if (window.flightGlobe?.active) { window.flightGlobe.fit(); return; } if (map && markers.size) { $("follow").checked = false; map.fitBounds(L.latLngBounds([...markers.values()].map(m => m.getLatLng())), {padding: [30, 30], maxZoom: 16}); } };
setupMap(); setInterval(poll, 1000); setInterval(() => { updateStatus(); window.missionMap?.updatePosition(); }, 1000);

function sourceTab(which) {
  sourceMode = which;
  for (const [name, tab, panel] of [["user", "liveTab", "liveSource"], ["upload", "uploadTab", "uploadSource"], ["admin", "adminTab", "adminSource"], ["follow", "followTab", "followSource"], ["geofence", "geofenceTab", "geofenceSource"], ["kml", "kmlTab", "kmlSource"]]) {
    $(tab).setAttribute("aria-selected", String(which === name)); $(panel).hidden = which !== name;
  }
}
$("liveTab").onclick = () => { sourceTab("user"); followingLive = false; window.restoreUserInput?.(); };
$("uploadTab").onclick = () => { sourceTab("upload"); followingLive = false; };
$("adminTab").onclick = () => sourceTab("admin");
$("followTab").onclick = () => { sourceTab("follow"); followingLive = true; session = ""; replayPlaying = false; replayMode = false; clearView(); window.resetMonitor?.(); poll(); };
async function loadReplay(id) {
  switching = true;
  replayPlaying = false;
  try {
    let after = 0, all = [];
    for (;;) {
      const batch = await api(`/api/packets?session=${encodeURIComponent(id)}&after=${after}`);
      if (!batch.length) break;
      all.push(...batch); after = batch.at(-1).id;
    }
    if (!all.length) throw new Error("The imported session contains no packets.");
    startReplay(id, all);
  } catch (e) { error(e.message); }
  finally { switching = false; await poll(); }
}
function startReplay(id, all) {
  followingLive = false; session = id; replayMode = true; flightPackets = all; replayIndex = 0;
  $("replayControls").hidden = false; $("replaySeek").max = String(all.length - 1);
  sourceTab("upload"); showReplay(0);
}
function replayLabel() {
  $("replayPlay").textContent = replayPlaying ? "Pause Ⅱ" : "Play ▶";
  $("replaySeek").value = String(replayIndex);
  $("replayProgress").textContent = `${replayIndex + 1} / ${flightPackets.length}`;
  updateStatus();
}
function showReplay(index) {
  replayIndex = Math.max(0, Math.min(flightPackets.length - 1, index));
  clearView();
  packets = flightPackets.slice(0, replayIndex + 1); packets.forEach(addToMap); renderPackets(); replayLabel();
  const p = [...packets].reverse().find(p => p.has_position);
  if (map && p && $("follow").checked) map.setView([p.latitude_deg, p.longitude_deg], Math.max(map.getZoom(), 12));
}
$("importCsv").onclick = async () => {
  const file = $("csvFile").files[0];
  if (!file) return error("Choose a flight CSV file first.");
  if (file.size > 8 * 1024 * 1024) return error("Choose a CSV smaller than 8 MB.");
  $("importCsv").disabled = true; error("");
  try {
    const all = LocalFiles.parseFlightCsv(await file.text());
    $("uploadHelp").textContent = `${file.name} · ${all.length} rows loaded in this browser. Press Play or Show full flight.`;
    window.resetMonitor?.(); startReplay("", all); await poll();
  } catch (e) { error(e.message); }
  finally { $("importCsv").disabled = false; }
};
$("replayStart").onclick = () => { replayPlaying = false; replayElapsed = 0; showReplay(0); };
$("replayPlay").onclick = () => {
  if (replayIndex === flightPackets.length - 1) showReplay(0);
  replayPlaying = !replayPlaying; replayElapsed = 0; replayLastTick = performance.now(); replayLabel();
};
$("replaySeek").oninput = () => { replayPlaying = false; replayElapsed = 0; showReplay(Number($("replaySeek").value)); };
$("replayFull").onclick = () => { replayPlaying = false; showReplay(flightPackets.length - 1); $("fit").click(); };
setInterval(() => {
  if (!replayMode || !replayPlaying) return;
  const now = performance.now();
  replayElapsed += (now - replayLastTick) * Number($("replaySpeed").value); replayLastTick = now;
  let changed = false;
  while (replayIndex < flightPackets.length - 1) {
    const current = flightPackets[replayIndex], next = flightPackets[replayIndex + 1];
    const gap = next.uptime_ms >= current.uptime_ms ? Math.max(1, next.uptime_ms - current.uptime_ms) : 1000;
    if (replayElapsed < gap) break;
    replayElapsed -= gap; replayIndex++; packets.push(next); addToMap(next); changed = true;
  }
  if (replayIndex === flightPackets.length - 1) replayPlaying = false;
  if (changed) {
    autoSelect = true; renderPackets();
    const p = packets.at(-1); if (map && p.has_position && $("follow").checked) map.panTo([p.latitude_deg, p.longitude_deg]);
  }
  replayLabel();
}, 100);
