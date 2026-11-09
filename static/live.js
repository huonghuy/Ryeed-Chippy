"use strict";
let userSession = "", serialInput = null, demoTimer = null, monitorSession = "", monitorAfter = 0, monitorLines = [];
window.resetMonitor = () => { monitorSession = ""; monitorAfter = 0; monitorLines = []; $("serialMonitor").textContent = "Waiting for serial input."; };
window.pollMonitor = async id => {
  if (!id) { $("monitorStatus").textContent = "NO INPUT"; return; }
  if (id !== monitorSession) { window.resetMonitor(); monitorSession = id; }
  const lines = await api(`/api/raw?session=${encodeURIComponent(id)}&after=${monitorAfter}`);
  if (id !== session || id !== monitorSession) return;
  if (lines.length) {
    monitorAfter = lines.at(-1).id;
    monitorLines.push(...lines.map(x => `${clock(x.received_at)}  ${x.line}`));
    monitorLines = monitorLines.slice(-300);
    const el = $("serialMonitor"), atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.textContent = monitorLines.join("\n"); if (atBottom) el.scrollTop = el.scrollHeight;
  }
  $("monitorStatus").textContent = followingLive ? "SHARED FEED" : "PRIVATE INPUT";
};
window.updateAdmin = admin => {
  $("adminLocked").hidden = admin; $("adminTools").hidden = !admin;
  $("adminLogin").textContent = admin ? "Admin account" : "Admin Login";
  $("liveTab").hidden = !admin;
  if (admin) return;
  if (sourceMode === "user") $("followTab").click();
  stopDemo();
  if (serialInput) {
    error("Admin session ended. USB publishing has stopped; log in again to reconnect.");
    serialInput.stop(false);
  }
};
function openLogin() {
  if (state.admin) { sourceTab("admin"); return; }
  $("loginError").textContent = ""; $("loginPassword").value = "";
  $("loginDialog").showModal(); $("loginPassword").focus();
}
$("adminLogin").onclick = openLogin; $("adminUnlock").onclick = openLogin;
$("cancelLogin").onclick = () => $("loginDialog").close();
$("loginForm").onsubmit = async e => {
  e.preventDefault();
  try {
    await api("/api/login", {password: $("loginPassword").value});
    $("loginDialog").close(); $("loginPassword").value = ""; error("");
    state.admin = true; window.updateAdmin(true); sourceTab("admin"); await poll();
  } catch (e) { $("loginError").textContent = e.message; }
};
$("logout").onclick = async () => {
  try { if (serialInput?.shared) await serialInput.stop(); await api("/api/logout", {}); state.admin = false; window.updateAdmin(false); await poll(); }
  catch (e) { error(e.message); }
};
$("changePassword").onclick = () => { $("passwordForm").reset(); $("passwordError").textContent = ""; $("passwordDialog").showModal(); };
$("cancelPassword").onclick = () => $("passwordDialog").close();
$("passwordForm").onsubmit = async e => {
  e.preventDefault();
  if ($("newPassword").value !== $("confirmPassword").value) { $("passwordError").textContent = "The new passwords do not match."; return; }
  try {
    await api("/api/password", {current: $("currentPassword").value, password: $("newPassword").value});
    $("passwordDialog").close(); $("passwordForm").reset();
    $("publishHelp").textContent = "Password changed and saved. Other admin sessions and receiver uploaders must log in again.";
  } catch (e) { $("passwordError").textContent = e.message; }
};

class USBInput {
  constructor(port, shared, id) {
    this.port = port; this.shared = shared; this.id = id; this.queue = []; this.pending = null;
    this.stopping = false; this.reader = null; this.timer = setInterval(() => this.flush(), 400);
  }
  async flush() {
    if (this.sending || (!this.queue.length && !this.pending)) return;
    this.sending = true;
    if (!this.pending) this.pending = {batch: crypto.randomUUID(), lines: this.queue.splice(0, 200)};
    try {
      await api("/api/input/append", {session: this.id, shared: this.shared, ...this.pending});
      this.pending = null;
      if (this.shared) $("publishHelp").textContent = "Publishing live USB readings. Keep this browser tab open.";
      if (this.stopping && !this.queue.length) { clearInterval(this.timer); if (serialInput === this) serialInput = null; }
    } catch (e) {
      error(`Input upload paused: ${e.message}. ${this.queue.length + this.pending.lines.length} lines buffered in this tab; retrying automatically.`);
    } finally { this.sending = false; }
  }
  async read() {
    this.reader = this.port.readable.getReader(); let buffer = "", discard = false;
    try {
      while (!this.stopping) {
        const {value, done} = await this.reader.read(); if (done) break;
        for (const byte of value) {
          if (byte === 10) {
            if (!discard) this.queue.push(buffer.replace(/\r$/, ""));
            buffer = ""; discard = false;
          } else if (!discard) {
            buffer += String.fromCharCode(byte);
            if (buffer.length > 2048) { buffer = ""; discard = true; error("Ignored oversized USB line."); }
          }
        }
        if (this.queue.length > 20000) { error("Upload buffer is full. USB capture stopped. Keep this tab open to retry buffered data."); break; }
      }
    } catch (e) { if (!this.stopping) error(`USB connection lost: ${e.message}`); }
    finally {
      this.reader.releaseLock(); this.reader = null;
      await this.port.close().catch(() => {}); this.stopping = true;
      $(this.shared ? "adminDisconnect" : "disconnect").disabled = true;
    }
  }
  async stop(flush = true) {
    this.stopping = true;
    await this.reader?.cancel().catch(() => {});
    await this.reading;
    // Keep the retry timer running if an outage leaves unsent readings.
    if (flush) await this.flush();
    if (!this.queue.length && !this.pending) { clearInterval(this.timer); if (serialInput === this) serialInput = null; }
  }
}
async function connectUSB(shared) {
  if (!navigator.serial || !window.isSecureContext) return error("USB browser capture needs a browser with Web Serial on HTTPS or localhost. You can also use receiver_uploader.py for shared publishing.");
  if (serialInput) return error("Disconnect the current USB input first. If uploads are buffered, wait for them to finish before reconnecting.");
  let port;
  try {
    // Invoke the browser picker directly in the click gesture.
    port = await navigator.serial.requestPort(); await port.open({baudRate: 115200});
    stopDemo();
    const result = await api("/api/input/start", {shared});
    serialInput = new USBInput(port, shared, result.session);
    if (!shared) userSession = result.session;
    followingLive = shared; replayPlaying = false; replayMode = false; session = result.session;
    $("replayControls").hidden = true; clearView(); window.resetMonitor(); error("");
    $(shared ? "adminDisconnect" : "disconnect").disabled = false;
    serialInput.reading = serialInput.read(); await poll();
  } catch (e) { if (port && !serialInput) await port.close().catch(() => {}); if (e.name !== "NotFoundError") error(e.message); }
}
function stopDemo() { if (demoTimer) clearInterval(demoTimer); demoTimer = null; }
$("connect").onclick = () => connectUSB(false); $("adminConnect").onclick = () => connectUSB(true);
$("disconnect").onclick = async () => { stopDemo(); if (serialInput && !serialInput.shared) await serialInput.stop(); $("disconnect").disabled = true; };
$("adminDisconnect").onclick = async () => { if (serialInput?.shared) await serialInput.stop(); };
window.restoreUserInput = () => {
  const id = userSession || state.sessions?.find(s => s.source === "User input" || s.source === "Demo · private")?.id;
  replayMode = false; replayPlaying = false; $("replayControls").hidden = true;
  session = id || ""; clearView(); window.resetMonitor(); poll();
};
$("demo").onclick = async () => {
  if (serialInput) return error("Disconnect USB input before starting a demo.");
  stopDemo();
  try {
    const result = await api("/api/input/start", {shared: false, demo: true}); userSession = result.session;
    followingLive = false; session = userSession; replayMode = false; replayPlaying = false; $("replayControls").hidden = true;
    clearView(); window.resetMonitor(); let seq = 0, sending = false;
    const tick = async () => {
      if (sending) return; sending = true;
      const n = seq++, seconds = n * 5, t = new Date();
      const line = `${seconds*1000},1,C1,${n},${t.getUTCHours()},${t.getUTCMinutes()},${t.getUTCSeconds()},${seconds*1000},${(21-n*.03).toFixed(2)},1000,50,1,${(38.985+n*.0006).toFixed(6)},${(-76.942+n*.0009).toFixed(6)},${80+seconds*4.2},10,1,1,1,-65,9`;
      try { await api("/api/input/append", {session: result.session, shared: false, batch: crypto.randomUUID(), lines: ["# DEMO — simulated flight data", line]}); }
      catch (e) { error(e.message); stopDemo(); }
      finally { sending = false; }
    };
    $("disconnect").disabled = false; await tick(); demoTimer = setInterval(tick, 1000); await poll();
  } catch (e) { error(e.message); }
};
$("publishCsv").onclick = async () => {
  const file = $("adminCsv").files[0];
  if (!file || file.size > 8 * 1024 * 1024) return error("Choose a CSV file up to 8 MB.");
  if (serialInput?.shared) return error("Stop USB publishing before replacing the shared feed with a CSV.");
  $("publishCsv").disabled = true;
  try {
    const result = await api("/api/import", {name: file.name, csv: await file.text(), shared: true});
    $("publishHelp").textContent = `Published ${file.name}: ${result.count} packets. Everyone following the feed will see this flight.`;
    followingLive = true; session = ""; replayPlaying = false; replayMode = false; clearView(); window.resetMonitor(); error(""); await poll();
  } catch (e) { error(e.message); }
  finally { $("publishCsv").disabled = false; }
};
window.addEventListener("beforeunload", e => { if (serialInput && (!serialInput.stopping || serialInput.queue.length || serialInput.pending)) { e.preventDefault(); e.returnValue = ""; } });
sourceTab("follow"); poll();
