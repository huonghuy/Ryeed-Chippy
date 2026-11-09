"use strict";
const mission = window.missionMap = {
  config: {revision: "", geofence: null, kml: null}, airspaces: [], airspaceVersion: 0,
  lastConfigPoll: 0, configBusy: false, layers: {}, source: null, globeSignature: "", airspaceRequest: 0,
  updateAdmin(admin) { for (const id of ["publishFence", "publishKml"]) $(id).hidden = !admin; },
  async pollConfig(force = false) {
    this.updateAdmin(state.admin);
    if (this.configBusy || (!force && Date.now() - this.lastConfigPoll < 5000)) return;
    this.configBusy = true; this.lastConfigPoll = Date.now();
    const scope = $("overlayScope").value;
    try {
      const config = scope === "private" ? this.privateConfig() : await api("/api/map-config");
      if (scope !== $("overlayScope").value) return;
      if (this.scope !== scope || config.revision !== this.config.revision || force) {
        this.scope = scope; this.config = config; this.draw2d(); this.render3d(true); this.fillForm();
      }
    } catch (e) { error(`Map overlays could not load: ${e.message}`); }
    finally { this.configBusy = false; }
  },
  privateConfig() {
    if (!this.localOverlays) {
      try { this.localOverlays = JSON.parse(localStorage.getItem("chippy-private-overlays")); } catch {}
      this.localOverlays ||= {revision: "", geofence: null, kml: null};
    }
    return this.localOverlays;
  },
  savePrivate(body) {
    this.localOverlays = LocalFiles.applyOverlay(this.privateConfig(), body);
    // A full or blocked localStorage only costs persistence across page reloads.
    try { localStorage.setItem("chippy-private-overlays", JSON.stringify(this.localOverlays)); this.storageError = ""; }
    catch { this.storageError = "Overlay is shown, but this browser could not save it. It will be gone after a refresh."; }
    return this.localOverlays;
  },
  async save(body, shared) {
    const config = shared ? await api("/api/map-config", body) : this.savePrivate(body);
    $("overlayScope").value = shared ? "shared" : "private";
    this.scope = $("overlayScope").value; this.config = config; this.lastConfigPoll = 0;
    this.draw2d(); this.render3d(true); this.fillForm(); error(shared ? "" : this.storageError);
  },
  fillForm() {
    const fence = this.config.geofence;
    if (fence && !$("geofenceForm").contains(document.activeElement)) {
      fence.corners.forEach((p, i) => { $("fenceLat"+i).value = p[0]; $("fenceLon"+i).value = p[1]; });
      $("fenceWarning").value = fence.warning_m;
    }
    const polygons = (this.config.kml?.features || []).map((f, i) => [f, i]).filter(([f]) => f.geometry.type === "Polygon");
    $("kmlPolygon").replaceChildren(new Option(polygons.length ? "Select a polygon" : "No polygon loaded", ""), ...polygons.map(([f,i]) => new Option(f.properties.name, String(i))));
    $("kmlHelp").textContent = this.config.kml ? `${this.config.kml.name} · ${this.config.kml.features.length} features · ${this.scope === "shared" ? "shared with viewers" : "private to this browser"}` : "No KML loaded in the selected overlay source.";
  },
  features() {
    return [...($("airspaceToggle").checked ? this.airspaces : []), ...($("kmlToggle").checked ? this.config.kml?.features || [] : [])];
  },
  color(f) {
    const p = f.properties || {};
    return p.layer === "tfr" ? "#e14849" : p.layer === "sua" ? "#d6a031" : p.layer === "controlled" ? ({B:"#5789e9", C:"#c167d2", D:"#579cc1"}[p.CLASS] || "#5789e9") : "#16a8ac";
  },
  description(f) {
    const p = f.properties || {};
    const limits = p.layer && p.layer !== "tfr" ? ` · lower ${p.LOWER_VAL ?? "?"} ${p.LOWER_UOM || ""} ${p.LOWER_CODE || ""} / upper ${p.UPPER_VAL ?? "?"} ${p.UPPER_UOM || ""} ${p.UPPER_CODE || ""}` : p.layer === "tfr" ? " · ground footprint; vertical limits unavailable in this feed" : "";
    return `${p.name || "Map feature"}${limits}${p.TIMESOFUSE ? " · " + p.TIMESOFUSE : ""}`;
  },
  info(f) { $("airspaceInfo").hidden = false; $("airspaceInfo").textContent = this.description(f); },
  draw2d() {
    if (!map || !window.L) return;
    for (const key of ["overlays", "fence", "position"]) {
      if (!this.layers[key]) this.layers[key] = L.layerGroup().addTo(map);
      this.layers[key].clearLayers();
    }
    for (const f of this.features()) {
      const text = document.createElement("div"); text.textContent = this.description(f);
      L.geoJSON(f, {style: {color: this.color(f), weight: 2, fillOpacity: .12}, pointToLayer: (_f,p) => L.circleMarker(p,{radius:6,color:this.color(f)})}).bindPopup(text).on("click", () => this.info(f)).addTo(this.layers.overlays);
    }
    if (this.config.geofence && $("choppiesToggle").checked) {
      L.polygon(this.config.geofence.corners, {color:"#dc7c23",weight:3,dashArray:"8 5",fillColor:"#f3b753",fillOpacity:.13}).addTo(this.layers.fence);
    }
    this.updatePosition();
  },
  updatePosition() {
    const visible = $("choppiesToggle").checked && !!this.config.geofence;
    $("fenceStatus").hidden = !visible;
    const p = packets.at(-1), result = MissionGeo.fenceStatus(this.config.geofence, p, !replayMode);
    if (visible) {
      $("fenceStatus").className = `fence-status ${result.kind}`;
      $("fenceStatus").textContent = `${replayMode ? "RECORDED POSITION · " : ""}${result.text} · dashboard monitoring only`;
    }
    this.layers.position?.clearLayers();
    const entities = this.source?.entities;
    for (const id of ["payload-ground", "payload-boundary"]) entities?.removeById(id);
    if (!visible || !p?.has_position) return;
    const color = result.kind === "outside" ? "#d72f41" : result.kind === "near" ? "#cd8c17" : result.kind === "unknown" ? "#7e8984" : "#207c55";
    if (this.layers.position) {
      const label = document.createElement("span"); label.textContent = `Payload ground position · ${result.text}`;
      L.circleMarker([p.latitude_deg,p.longitude_deg],{radius:8,color:"white",weight:3,fillColor:color,fillOpacity:1}).bindTooltip(label).addTo(this.layers.position);
      if (result.nearest) L.polyline([[p.latitude_deg,p.longitude_deg],result.nearest],{color,weight:2,dashArray:"4 5"}).addTo(this.layers.position);
    }
    if (entities && window.flightGlobe?.active) {
      const C = Cesium;
      entities.add({id:"payload-ground",position:C.Cartesian3.fromDegrees(p.longitude_deg,p.latitude_deg,0),point:{pixelSize:12,color:C.Color.fromCssColorString(color),outlineColor:C.Color.WHITE,outlineWidth:2,disableDepthTestDistance:Infinity}});
      if (result.nearest) entities.add({id:"payload-boundary",polyline:{positions:C.Cartesian3.fromDegreesArray([p.longitude_deg,p.latitude_deg,result.nearest[1],result.nearest[0]]),width:2,material:new C.PolylineDashMaterialProperty({color:C.Color.fromCssColorString(color)})}});
      window.flightGlobe.viewer.scene.requestRender();
    }
  },
  render3d(force = false) {
    const globe = window.flightGlobe;
    if (!globe?.active || !globe.viewer || !window.Cesium) return;
    const C = Cesium;
    if (!this.source) { this.source = new C.CustomDataSource("CHIPPY map overlays"); globe.viewer.dataSources.add(this.source); }
    const signature = [this.scope,this.config.revision,this.airspaceVersion,$("airspaceToggle").checked,$("choppiesToggle").checked,$("kmlToggle").checked].join("/");
    if (!force && signature === this.globeSignature) { this.updatePosition(); return; }
    this.globeSignature = signature; const entities = this.source.entities; entities.removeAll();
    const position = (p, mode) => C.Cartesian3.fromDegrees(p[0],p[1],mode === "clampToGround" ? 0 : p[2] || 0);
    const polygon = (rings, f) => {
      const prop = f.properties || {}, mode = prop.altitude_mode || "clampToGround", color = C.Color.fromCssColorString(this.color(f));
      const hierarchy = new C.PolygonHierarchy(rings[0].map(p => position(p,mode)),rings.slice(1).map(r => new C.PolygonHierarchy(r.map(p => position(p,mode)))));
      const known = prop.floor_m != null && prop.ceiling_m != null && prop.ceiling_m > prop.floor_m;
      const opts = {hierarchy,material:color.withAlpha(known ? .12 : .16),outline:true,outlineColor:color};
      if (prop.layer) { opts.height = known ? prop.floor_m : 0; if (known) opts.extrudedHeight = prop.ceiling_m; }
      else if (mode !== "clampToGround") opts.perPositionHeight = true;
      else opts.height = 0;
      entities.add({name:prop.name,polygon:opts});
    };
    for (const f of this.features()) {
      const geom = f.geometry, prop = f.properties || {}; if (!geom) continue;
      if (geom.type === "Polygon") polygon(geom.coordinates,f);
      else if (geom.type === "MultiPolygon") for (const rings of geom.coordinates) polygon(rings,f);
      else if (geom.type === "LineString") entities.add({name:prop.name,polyline:{positions:geom.coordinates.map(p=>position(p,prop.altitude_mode)),width:3,material:C.Color.fromCssColorString(this.color(f))}});
      else if (geom.type === "Point") entities.add({name:prop.name,position:position(geom.coordinates,prop.altitude_mode),point:{pixelSize:9,color:C.Color.AQUA,outlineColor:C.Color.WHITE,outlineWidth:2},label:{text:prop.name || "KML point",font:"11px sans-serif",pixelOffset:new C.Cartesian2(0,-15),showBackground:true}});
    }
    const fence = this.config.geofence;
    if (fence && $("choppiesToggle").checked) {
      const corners = fence.corners.map(([lat,lon]) => C.Cartesian3.fromDegrees(lon,lat,0));
      entities.add({polygon:{hierarchy:corners,height:0,material:C.Color.ORANGE.withAlpha(.17)}});
      entities.add({polyline:{positions:[...corners,corners[0]],width:3,material:new C.PolylineDashMaterialProperty({color:C.Color.ORANGE})}});
    }
    this.updatePosition(); globe.viewer.scene.requestRender();
  },
  async loadAirspace() {
    $("airspaceControls").hidden = !$("airspaceToggle").checked;
    const ticket = ++this.airspaceRequest;
    if (!$("airspaceToggle").checked) { this.draw2d(); this.render3d(true); return; }
    const kinds = [["classLayer","controlled"],["suaLayer","sua"],["tfrLayer","tfr"]].filter(([id])=>$(id).checked).map(([,k])=>k);
    if (!kinds.length) { this.airspaces=[]; this.airspaceVersion++; this.draw2d(); this.render3d(true); $("airspaceStatus").textContent="All airspace categories are off."; return; }
    let bounds;
    const globe = window.flightGlobe;
    if (globe?.active) {
      const rect = globe.viewer.camera.computeViewRectangle(globe.viewer.scene.globe.ellipsoid);
      if (rect) bounds = [rect.west,rect.south,rect.east,rect.north].map(Cesium.Math.toDegrees);
    }
    if (!bounds && map) { const b=map.getBounds(); bounds=[b.getWest(),b.getSouth(),b.getEast(),b.getNorth()]; }
    if (!bounds) return;
    bounds=[Math.max(-180,bounds[0]),Math.max(-90,bounds[1]),Math.min(180,bounds[2]),Math.min(90,bounds[3])];
    if (bounds[2]-bounds[0] > 8 || bounds[3]-bounds[1] > 8 || bounds[2] <= bounds[0]) { $("airspaceStatus").textContent="Zoom in to load airspace (view must span less than 8°). Existing overlays remain visible."; return; }
    if (bounds[2]-bounds[0]<.01) { const c=(bounds[0]+bounds[2])/2; bounds[0]=c-.005; bounds[2]=c+.005; }
    if (bounds[3]-bounds[1]<.01) { const c=(bounds[1]+bounds[3])/2; bounds[1]=c-.005; bounds[3]=c+.005; }
    $("airspaceStatus").textContent="Loading FAA airspace for this view…";
    try {
      const data=await api(`/api/airspace?bounds=${bounds.join(",")}&layers=${kinds.join(",")}`);
      if (ticket !== this.airspaceRequest) return;
      this.airspaces=data.layers.flatMap(layer=>layer.features); this.airspaceVersion++;
      const issues=data.layers.filter(l=>l.stale || l.truncated);
      const times=data.layers.filter(l=>l.fetched_at).map(l=>dateTime(l.fetched_at));
      $("airspaceStatus").textContent=`${this.airspaces.length} FAA features · ${times.length ? "fetched " + times.at(-1) : "no current data"}${issues.length ? " · " + issues.map(l=>`${l.kind}: ${l.truncated ? "partial results — zoom in" : "unavailable or stale"}`).join("; ") : ""}. ${data.layers.every(l=>!l.stale) ? "Fetched reference data; check FAA details for effective times." : "Do not interpret missing data as clear airspace."}`;
      this.draw2d(); this.render3d(true);
    } catch(e) { if(ticket===this.airspaceRequest) $("airspaceStatus").textContent=`Airspace unavailable: ${e.message}. Existing overlays may be stale.`; }
  }
};
$("geofenceTab").onclick=()=>sourceTab("geofence"); $("kmlTab").onclick=()=>sourceTab("kml");
$("overlayScope").onchange=()=>mission.pollConfig(true);
for (const id of ["choppiesToggle","kmlToggle"]) $(id).onchange=()=>{mission.draw2d();mission.render3d(true);};
for (const id of ["airspaceToggle","classLayer","suaLayer","tfrLayer","refreshAirspace"]) $(id)[id==="refreshAirspace"?"onclick":"onchange"]=()=>mission.loadAirspace();
$("dropdownToggle").onchange=async()=>{if($("dropdownToggle").checked && !window.flightGlobe.active) await window.flightGlobe.show(true);window.flightGlobe.render(packets);};
async function saveFence(shared) {
  if (!$("geofenceForm").reportValidity()) return;
  try {
    const corners=Array.from({length:4},(_,i)=>[Number($("fenceLat"+i).value),Number($("fenceLon"+i).value)]);
    await mission.save({action:"geofence",corners,warning_m:Number($("fenceWarning").value)},shared);
    $("choppiesToggle").checked=true;mission.draw2d();mission.render3d(true);
    if(map)map.fitBounds(corners,{padding:[25,25]});
    $("geofenceHelp").textContent=`Geofence ${shared ? "published to live viewers" : "saved in this browser only"}. Monitoring only; cutdown remains onboard.`;
  } catch(e){error(e.message);}
}
$("geofenceForm").onsubmit=e=>{e.preventDefault();saveFence(false);}; $("publishFence").onclick=()=>saveFence(true);
async function uploadKml(shared) {
  const file=$("kmlFile").files[0];if(!file || !file.name.toLowerCase().endsWith(".kml") || file.size>4*1024*1024)return error("Choose a .kml file up to 4 MB.");
  try {await mission.save({action:"kml",name:file.name,kml:await file.text()},shared);$("kmlToggle").checked=true;mission.draw2d();mission.render3d(true);
    if(map&&mission.layers.overlays){const layer=L.geoJSON(mission.config.kml);if(layer.getBounds().isValid())map.fitBounds(layer.getBounds(),{padding:[30,30]});}
  }catch(e){error(e.message);}
}
$("loadKml").onclick=()=>uploadKml(false);$("publishKml").onclick=()=>uploadKml(true);
for(const [id,action] of [["clearFence","clear_geofence"],["clearKml","clear_kml"]])$(id).onclick=async()=>{try{await mission.save({action},$("overlayScope").value==="shared");}catch(e){error(e.message);}};
$("kmlFence").onclick=async()=>{
  const i=$("kmlPolygon").value;if(i==="")return error("Select a KML polygon first.");
  const feature=mission.config.kml?.features[Number(i)],rings=feature?.geometry.coordinates;
  if(!rings || rings.length!==1 || rings[0].length!==5)return error("The geofence needs a four-corner rectangle without holes.");
  try{await mission.save({action:"geofence",corners:rings[0].slice(0,-1).map(p=>[p[1],p[0]]),warning_m:Number($("fenceWarning").value)},$("overlayScope").value==="shared");$("choppiesToggle").checked=true;mission.draw2d();mission.render3d(true);sourceTab("geofence");}catch(e){error(e.message);}
};
if(map){let timer;map.on("moveend",()=>{clearTimeout(timer);timer=setTimeout(()=>{if($("airspaceToggle").checked && !window.flightGlobe?.active)mission.loadAirspace();},700);});}
// TFRs can change while a viewer remains on the same part of the map.
setInterval(()=>{if($("airspaceToggle").checked && !document.hidden)mission.loadAirspace();},60000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden && $("airspaceToggle").checked)mission.loadAirspace();});
mission.pollConfig(true);
