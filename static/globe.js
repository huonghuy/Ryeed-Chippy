"use strict";
// Cesium uses a full-scale globe and meter heights, without a 100,000-ft ceiling.
window.flightGlobe = {
  active: false, viewer: null, loading: null, ids: new Map(),
  async load() {
    if (window.Cesium) return;
    if (!this.loading) this.loading = new Promise((resolve, reject) => {
      const base = "https://cdn.jsdelivr.net/npm/cesium@1.121.0/Build/Cesium/";
      window.CESIUM_BASE_URL = base;
      const css = document.createElement("link"); css.rel = "stylesheet"; css.href = base + "Widgets/widgets.css"; document.head.append(css);
      const script = document.createElement("script"); script.src = base + "Cesium.js";
      script.onload = resolve; script.onerror = () => { this.loading = null; script.remove(); reject(new Error("3D library could not load. Check your internet connection and try again.")); };
      document.head.append(script);
    });
    await this.loading;
  },
  async show(active) {
    if (!active) {
      this.active = false; $("globe").hidden = true; $("globeHelp").hidden = true; $("map").hidden = false;
      $("view2d").setAttribute("aria-pressed", "true"); $("view3d").setAttribute("aria-pressed", "false"); map?.invalidateSize(); window.missionMap?.draw2d(); return;
    }
    $("view3d").disabled = true;
    try {
      await this.load();
      $("globe").hidden = false; $("map").hidden = true; $("globeHelp").hidden = false;
      if (!this.viewer) {
        const C = Cesium;
        this.viewer = new C.Viewer("globe", {animation: false, timeline: false, baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false, navigationHelpButton: false, fullscreenButton: false, infoBox: false, selectionIndicator: false, baseLayer: false, requestRenderMode: true});
        try {
          const imagery = await C.ArcGisMapServerImageryProvider.fromUrl("https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer");
          this.viewer.imageryLayers.addImageryProvider(imagery);
        } catch (_) {
          // The globe and flight path remain usable if imagery is unavailable.
          error("Satellite imagery is unavailable. The 3D globe and flight path still work.");
        }
        this.viewer.scene.globe.depthTestAgainstTerrain = false;
        this.viewer.screenSpaceEventHandler.setInputAction(movement => {
          const picked = this.viewer.scene.pick(movement.position), id = picked?.id?.properties?.packetId?.getValue();
          if (id != null) { autoSelect = false; selectPacket(id, false); }
        }, C.ScreenSpaceEventType.LEFT_CLICK);
      }
      this.active = true; this.viewer.resize(); this.render(packets); this.fit();
      $("view2d").setAttribute("aria-pressed", "false"); $("view3d").setAttribute("aria-pressed", "true");
      $("tileStatus").textContent = "3D globe · altitude in meters · ellipsoid surface";
    } catch (e) { await this.show(false); error(`3D view unavailable: ${e.message}`); }
    finally { $("view3d").disabled = false; }
  },
  render(data) {
    if (!this.viewer || !this.active) return;
    const C = Cesium, valid = data.filter(p => p.has_position && !p.duplicate && p.altitude_m != null), groups = new Map();
    this.viewer.entities.suspendEvents(); this.viewer.entities.removeAll(); this.ids.clear();
    for (const p of valid) {
      const position = C.Cartesian3.fromDegrees(p.longitude_deg, p.latitude_deg, p.altitude_m), key = `${p.sender}/${p.boot}`;
      if (!groups.has(key)) groups.set(key, []); groups.get(key).push(p);
      const entity = this.viewer.entities.add({position, properties: {packetId: p.id}, point: {pixelSize: p.id === selected ? 11 : 6, color: p.id === selected ? C.Color.YELLOW : C.Color.fromCssColorString("#28694e"), outlineColor: C.Color.WHITE, outlineWidth: 1, disableDepthTestDistance: Number.POSITIVE_INFINITY}});
      this.ids.set(p.id, entity);
    }
    const dropdown = $("dropdownToggle")?.checked;
    if (this.viewer.imageryLayers.length) this.viewer.imageryLayers.get(0).brightness = dropdown ? .6 : 1;
    for (const samples of groups.values()) {
      if (samples.length < 2) continue;
      const positions = samples.map(p => C.Cartesian3.fromDegrees(p.longitude_deg, p.latitude_deg, p.altitude_m));
      this.viewer.entities.add({polyline: {positions, width: dropdown ? 2 : 3, material: dropdown ? C.Color.WHITE : C.Color.fromCssColorString("#d5ef91"), clampToGround: false}});
      if (dropdown) {
        let segment = [samples[0]], ascending = samples[1].altitude_m >= samples[0].altitude_m;
        const curtain = (part, up) => {
          if (part.length < 2) return;
          const color = C.Color.fromCssColorString(up ? "#e149ad" : "#f2a23e");
          const ground = part.map(p => C.Cartesian3.fromDegrees(p.longitude_deg, p.latitude_deg, 0));
          this.viewer.entities.add({wall: {positions: ground, maximumHeights: part.map(p => Math.max(0, p.altitude_m)), minimumHeights: part.map(() => 0), material: color.withAlpha(.3), outline: false}});
          this.viewer.entities.add({polyline: {positions: ground, width: 6, material: C.Color.WHITE, clampToGround: false}});
          this.viewer.entities.add({polyline: {positions: ground, width: 3, material: color, clampToGround: false}});
        };
        for (let i = 1; i < samples.length; i++) {
          const up = samples[i].altitude_m >= samples[i-1].altitude_m;
          if (up !== ascending) { curtain(segment, ascending); segment = [samples[i-1]]; ascending = up; }
          segment.push(samples[i]);
        }
        curtain(segment, ascending);
      }
    }
    if (valid.length) {
      for (const [p, color, title] of [[valid[0], C.Color.DODGERBLUE, "Start"], [valid.at(-1), C.Color.fromCssColorString("#f23e56"), "Latest"]]) {
        this.viewer.entities.add({position: C.Cartesian3.fromDegrees(p.longitude_deg, p.latitude_deg, dropdown ? 0 : p.altitude_m), point: {pixelSize: 13, color, outlineColor: C.Color.WHITE, outlineWidth: 3, disableDepthTestDistance: Number.POSITIVE_INFINITY}, properties: {packetId: p.id}});
      }
    }
    if (valid.length) {
      const p = valid.at(-1);
      this.viewer.entities.add({polyline: {positions: C.Cartesian3.fromDegreesArrayHeights([p.longitude_deg, p.latitude_deg, 0, p.longitude_deg, p.latitude_deg, p.altitude_m]), width: 1, material: new C.PolylineDashMaterialProperty({color: C.Color.DARKSLATEGRAY})}});
      const peak = valid.reduce((a, b) => a.altitude_m >= b.altitude_m ? a : b);
      this.viewer.entities.add({position: C.Cartesian3.fromDegrees(peak.longitude_deg, peak.latitude_deg, peak.altitude_m), label: {text: `${Math.round(peak.altitude_m).toLocaleString()} m · ${Math.round(peak.altitude_m / .3048).toLocaleString()} ft`, font: "12px sans-serif", fillColor: C.Color.WHITE, showBackground: true, backgroundColor: C.Color.fromCssColorString("#19362edd"), pixelOffset: new C.Cartesian2(0, 25), disableDepthTestDistance: Number.POSITIVE_INFINITY}});
    }
    this.viewer.entities.resumeEvents(); this.viewer.scene.requestRender();
    window.missionMap?.render3d();
    const newest = valid.at(-1);
    if (newest && this.lastId !== newest.id && $("follow").checked && autoSelect) {
      const center = C.Cartesian3.fromDegrees(newest.longitude_deg, newest.latitude_deg, newest.altitude_m);
      const radius = Math.max(6000, Math.abs(newest.altitude_m) * 1.5);
      this.viewer.camera.flyToBoundingSphere(new C.BoundingSphere(center, 0), {duration: .3, offset: new C.HeadingPitchRange(this.viewer.camera.heading, this.viewer.camera.pitch, radius)});
    }
    this.lastId = newest?.id;
  },
  highlight(p, focus) {
    if (!this.viewer || !this.active) return;
    for (const [id, entity] of this.ids) { entity.point.pixelSize = id === p.id ? 11 : 6; entity.point.color = id === p.id ? Cesium.Color.YELLOW : Cesium.Color.fromCssColorString("#28694e"); }
    if (focus && this.ids.has(p.id)) {
      $("follow").checked = false;
      this.viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(this.ids.get(p.id).position.getValue(), 1000), {duration: .4, offset: new Cesium.HeadingPitchRange(0, -.55, Math.max(5000, Math.abs(p.altitude_m) * .7))});
    }
    this.viewer.scene.requestRender();
  },
  fit() {
    if (!this.viewer || !this.active) return;
    const positions = [...this.ids.values()].map(e => e.position.getValue());
    if (!positions.length) { this.viewer.camera.flyTo({destination: Cesium.Cartesian3.fromDegrees(-76.942, 38.985, 80000), duration: 0}); return; }
    const sphere = Cesium.BoundingSphere.fromPoints(positions);
    this.viewer.camera.flyToBoundingSphere(sphere, {duration: .4, offset: new Cesium.HeadingPitchRange(0, -.55, Math.max(6000, sphere.radius * 4))});
  }
};
$("view2d").onclick = () => window.flightGlobe.show(false);
$("view3d").onclick = () => window.flightGlobe.show(true);
