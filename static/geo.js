/* Pure geometry helpers shared by the map and its checks. */
(function(root) {
  "use strict";
  const R = 6371008.8, rad = n => n * Math.PI / 180;
  function distance(a, b) {
    const p = rad(a[0]), q = rad(b[0]), dlat = q - p, dlon = rad(b[1] - a[1]);
    const h = Math.sin(dlat/2)**2 + Math.cos(p)*Math.cos(q)*Math.sin(dlon/2)**2;
    return R * 2 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))));
  }
  function fenceStatus(fence, packet, live, now = Date.now()) {
    if (!fence) return {kind: "none", text: "No geofence loaded"};
    if (!packet?.has_position || packet.latitude_deg == null || packet.longitude_deg == null || packet.duplicate) return {kind: "unknown", text: "No valid current GPS position"};
    if (live && (!packet.received_at || now - Date.parse(packet.received_at) > 15000 || !Number.isFinite(Date.parse(packet.received_at)))) return {kind: "unknown", text: "GPS telemetry is stale — boundary status unknown"};
    const lat = packet.latitude_deg, lon = packet.longitude_deg;
    const inside = lat >= fence.south && lat <= fence.north && lon >= fence.west && lon <= fence.east;
    let nearest, meters;
    if (inside) {
      // North/south edges are constant latitude. Closest meridian point is
      // computed on the sphere, then clamped to the finite edge.
      const choices = [[fence.south, lon], [fence.north, lon]];
      for (const edge of [fence.west, fence.east]) {
        const phi = Math.atan2(Math.sin(rad(lat)), Math.cos(rad(lat))*Math.cos(rad(lon-edge))) * 180 / Math.PI;
        choices.push([Math.max(fence.south, Math.min(fence.north, phi)), edge]);
      }
      meters = Infinity;
      for (const p of choices) { const d = distance([lat,lon], p); if (d < meters) { meters = d; nearest = p; } }
    } else {
      const clampedLon = Math.max(fence.west, Math.min(fence.east, lon));
      const choices = [[fence.south, clampedLon], [fence.north, clampedLon]];
      for (const edge of [fence.west, fence.east]) {
        const phi = Math.atan2(Math.sin(rad(lat)), Math.cos(rad(lat))*Math.cos(rad(lon-edge))) * 180 / Math.PI;
        choices.push([Math.max(fence.south, Math.min(fence.north, phi)), edge]);
      }
      meters = Infinity;
      for (const p of choices) { const d = distance([lat,lon], p); if (d < meters) { meters = d; nearest = p; } }
    }
    const kind = !inside ? "outside" : meters <= fence.warning_m ? "near" : "inside";
    return {kind, inside, meters, nearest, text: `${inside ? kind === "near" ? "NEAR GEOFENCE EDGE" : "INSIDE GEOFENCE" : "OUTSIDE GEOFENCE"} · ${Math.round(meters).toLocaleString()} m ${inside ? "to boundary" : "outside boundary"}`};
  }
  const api = {distance, fenceStatus};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MissionGeo = api;
})(typeof window !== "undefined" ? window : globalThis);
