/* Map page: polls /api/map, draws vehicle + stations + environ points +
   location FEC tiles/zones, base tiles via the local caching proxy,
   radar via RainViewer. */
"use strict";

const map = L.map("map", { zoomControl: true }).setView([39.0, -98.0], 5);
L.tileLayer("/tiles/{z}/{x}/{y}.png", {
  maxZoom: 17, attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

// ---- RainViewer radar (browser-direct; silently absent offline) ------------
let radarLayers = [], radarOn = true, radarFrame = 0;

async function loadRadar() {
  radarLayers.forEach((l) => map.removeLayer(l));
  radarLayers = [];
  if (!radarOn) return;
  try {
    const r = await fetch("https://api.rainviewer.com/public/weather-maps.json");
    const j = await r.json();
    const frames = (j.radar.past || []).slice(-4).concat(j.radar.nowcast || []);
    radarLayers = frames.map((f) =>
      L.tileLayer(j.host + f.path + "/256/{z}/{x}/{y}/2/1_1.png",
                  { opacity: 0, maxZoom: 17 }).addTo(map));
    radarFrame = 0;
  } catch (e) { /* offline: no radar */ }
}

function stepRadar() {
  if (!radarLayers.length) return;
  radarLayers.forEach((l, i) => l.setOpacity(i === radarFrame ? 0.6 : 0));
  radarFrame = (radarFrame + 1) % radarLayers.length;
}

loadRadar();
setInterval(loadRadar, 5 * 60 * 1000);
setInterval(stepRadar, 700);

// ---- live state -------------------------------------------------------------
let follow = true;
let zoneMode = false;
let vehMarker = null, crumb = null, crumbPts = [];
const stationLayer = L.layerGroup().addTo(map);
const environLayer = L.layerGroup().addTo(map);
const locationLayer = L.layerGroup().addTo(map);
let firstFix = true;

function levelColor(level) {
  return ["#2b2", "#9c2", "#eb0", "#e83", "#e33"][Math.max(0, Math.min(level || 0, 4))];
}

// The last location_fec block the server sent. The editor is built from it
// (level list, WAN names, floor), and the layer is redrawn from it when zone
// mode is toggled -- the circles' interactivity depends on that mode.
let lastLoc = null;

function drawLocation(loc) {
  locationLayer.clearLayers();
  lastLoc = loc || null;
  if (!loc) return;
  // One bad value must cost the location layer alone. tick() draws this
  // in the same pass that moves the vehicle marker, so an exception
  // escaping here would freeze the marker and the breadcrumb. And one bad
  // ENTRY must cost only itself: the per-item try/catch below keeps a single
  // `bbox: null` from taking every other tile and zone down with it. The
  // outer catch stays as the last resort.
  try {
    (loc.tiles || []).forEach((t) => {
      try {
        const [s, w, n, e] = t.bbox;
        const entries = Object.entries(t.wans || {});
        const worst = Math.max(0, ...entries.map(([, v]) => v.ewma_loss || 0));
        const confirmed = entries.some(([, v]) => (v.passes || 0) >= 3);
        // Colour by loss band, not by level: the map has no profile table.
        const level = worst > 10 ? 4 : worst > 5 ? 3 : worst > 2 ? 2 : worst > 0.5 ? 1 : 0;
        const r = L.rectangle([[s, w], [n, e]], {
          color: levelColor(level), weight: confirmed ? 1.5 : 0.5,
          fillColor: levelColor(level), fillOpacity: confirmed ? 0.35 : 0.12,
        }).addTo(locationLayer);
        const tip = document.createElement("span");
        tip.className = "stn-tip";
        tip.textContent = entries.map(([wan, v]) =>
          `${wan}: ${(v.ewma_loss || 0).toFixed(1)}% (${v.passes || 0} passes)`).join("  ")
          + (t.residual != null ? `  residual ${Number(t.residual).toFixed(1)}/s` : "");
        r.bindTooltip(tip);
      } catch (e) { /* skip this tile, draw the rest */ }
    });
    (loc.zones || []).forEach((z) => {
      try {
        const drawn = z.source === "operator";
        // Every zone the daemon is acting on is drawn; `editable` is a
        // separate question. A config zone ships with the box, and an
        // operator zone with no id cannot be addressed by the endpoint --
        // both are shown, and both say why they cannot be changed here.
        const c = L.circle([z.lat, z.lon], {
          radius: z.radius_m, color: "#fff", weight: 2,
          dashArray: z.editable ? null : "6 4",
          fillColor: levelColor(z.level), fillOpacity: z.editable ? 0.3 : 0.2,
          interactive: !!z.editable && zoneMode,
        }).addTo(locationLayer);
        const tip = document.createElement("span");
        tip.className = "stn-tip";
        tip.textContent = `${z.label}: level ${z.level}`
          + (z.wans ? ` (${z.wans.join(", ")})` : "")
          + (z.editable ? ""
             : drawn ? " - no id: edit the file, or delete and redraw"
                     : " - from the config file");
        c.bindTooltip(tip, { permanent: true, direction: "center" });
        if (z.editable && zoneMode) c.on("click", (e) => {
          // Without this the map's own click handler also fires and opens a
          // NEW zone on top of the one just asked for.
          L.DomEvent.stopPropagation(e);
          openEditor(z);
        });
      } catch (e) { /* skip this zone, draw the rest */ }
    });
  } catch (e) {
    locationLayer.clearLayers();
  }
}

function vehIcon(track) {
  const rot = track == null ? 0 : track;
  return L.divIcon({ className: "", html:
    `<div class="veh-icon" style="transform:rotate(${rot}deg)">&#10148;</div>`,
    iconSize: [26, 26], iconAnchor: [13, 13] });
}

function precipColor(v) {
  if (v == null) return "#888";
  if (v >= 2.5) return "#e33";
  if (v >= 1.0) return "#eb0";
  return "#2b2";
}

async function renameStation(id, oldLabel) {
  const label = prompt("Label for " + id, oldLabel || "");
  if (label === null) return;
  await fetch("/api/station-label", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id, label: label }) });
}

function drawStations(stations, predictions) {
  stationLayer.clearLayers();
  stations.forEach((s) => {
    if (s.lat == null) return;
    const predicted = predictions.includes(s.id);
    const m = L.circleMarker([s.lat, s.lon], {
      radius: 6 + Math.min(s.visits || 0, 10),
      color: predicted ? "#4af" : "#ccc",
      weight: predicted ? 3 : 1.5,
      dashArray: predicted ? "4 4" : null,
      fillColor: "#333", fillOpacity: 0.6,
    }).addTo(stationLayer);
    const tip = document.createElement("span");
    tip.className = "stn-tip";
    tip.textContent = (s.label || s.id) + (predicted ? " (next?)" : "");
    m.bindTooltip(tip, { permanent: true, direction: "top", offset: [0, -8] });
    m.on("click", () => renameStation(s.id, s.label));
  });
}

function drawEnviron(env) {
  environLayer.clearLayers();
  if (!env || !env.points) return;
  env.points.forEach((p) => {
    const v = (p.values || {}).precip;
    const m = L.circleMarker([p.lat, p.lon], {
      radius: 14, color: precipColor(v), weight: 2,
      fillOpacity: 0.12, fillColor: precipColor(v),
    }).addTo(environLayer);
    const tip = document.createElement("span");
    tip.className = "stn-tip";
    tip.textContent = Object.entries(p.values || {})
      .map(([k, val]) => `${k}: ${val == null ? "?" : Number(val).toFixed(1)}`)
      .join("  ");
    m.bindTooltip(tip);
  });
}

// ---- zone editor ------------------------------------------------------------
// A zone has five fields and only means anything against the map underneath
// it, so it is edited in a panel with a live preview circle rather than in a
// chain of prompts. The server stays the truth: Save posts, and the next
// poll redraws the layer -- nothing here mutates locationLayer optimistically.

const el = (id) => document.getElementById(id);
let editing = null;       // {lat, lon, id?} being edited, or null when closed
let preview = null;       // the circle that follows the radius slider

function zoneLevels() {
  const levels = (lastLoc && lastLoc.levels) || [];
  if (levels.length) return levels;
  // No FEC section configured, so the server could not describe a table. The
  // endpoint still validates against the default one; offer its indices
  // rather than an empty list the operator cannot save from.
  return [0, 1, 2, 3, 4].map((i) => ({ level: i, ratio: null,
                                       overhead_pct: null }));
}

function buildLevels(chosen) {
  const sel = el("z-level");
  const floor = lastLoc ? lastLoc.floor_level : null;
  sel.textContent = "";
  zoneLevels().forEach((lv) => {
    const o = document.createElement("option");
    o.value = String(lv.level);
    let text = `level ${lv.level}`;
    if (lv.ratio) text += ` — ${lv.ratio}`;
    if (lv.overhead_pct != null) text += ` (${lv.overhead_pct}% overhead)`;
    if (floor != null && lv.level <= floor) {
      // Still selectable: an operator may want the zone to outlive a floor
      // change, and it costs nothing while the floor is where it is.
      text += " — already covered by the floor";
      o.className = "dim";
    }
    o.textContent = text;
    sel.appendChild(o);
  });
  sel.value = String(chosen);
  if (!sel.value) sel.selectedIndex = sel.options.length - 1;
}

function buildWans(chosen) {
  const row = el("z-wans");
  const wans = (lastLoc && lastLoc.wans) || {};
  row.textContent = "";
  Object.keys(wans).forEach((name) => {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = name;
    box.checked = !!(chosen && chosen.indexOf(name) >= 0);
    const text = document.createElement("span");
    // Operator-supplied, from the config: text only, never markup.
    text.textContent = " " + (wans[name] || name);
    label.appendChild(box);
    label.appendChild(text);
    row.appendChild(label);
  });
}

function checkedWans() {
  const out = [];
  el("z-wans").querySelectorAll("input:checked").forEach((b) => {
    out.push(b.value);
  });
  return out;
}

// The number input is the authoritative radius; the slider is a quick way to
// move it and only spans the radii worth dragging. A hand-written 5 km zone
// therefore opens at 5000 and stays 5000 unless the operator actually moves
// the slider -- the editor must never quietly shrink a zone it was only
// opened to look at.
function radiusValue() {
  return Number(el("z-radius-m").value);
}

function syncFromSlider() {
  el("z-radius-m").value = el("z-radius").value;
  updatePreview();
}

function syncFromNumber() {
  const r = radiusValue();
  if (Number.isFinite(r)) {
    el("z-radius").value = String(Math.min(2000, Math.max(50, r)));
  }
  updatePreview();
}

function updatePreview() {
  if (!editing) return;
  const radius = radiusValue();
  // A half-typed or emptied number is not a circle. Leave the preview where
  // it was; the server names the problem if they save it anyway.
  if (!Number.isFinite(radius) || radius <= 0) return;
  const color = levelColor(Number(el("z-level").value));
  const at = [editing.lat, editing.lon];
  if (!preview) {
    preview = L.circle(at, { radius: radius, color: color, weight: 2,
                             dashArray: "3 3", fillColor: color,
                             fillOpacity: 0.25,
                             interactive: false }).addTo(map);
  } else {
    preview.setLatLng(at);
    preview.setRadius(radius);
    preview.setStyle({ color: color, fillColor: color });
  }
}

function openEditor(zone) {
  editing = { lat: zone.lat, lon: zone.lon, id: zone.id };
  el("z-label").value = zone.label || "";
  const radius = Number(zone.radius_m) || 300;
  el("z-radius-m").value = String(radius);
  el("z-radius").value = String(Math.min(2000, Math.max(50, radius)));
  buildLevels(zone.level != null ? zone.level : 2);
  buildWans(zone.wans);
  el("z-suppress").checked = !!zone.suppress_learned;
  el("z-error").textContent = "";
  el("z-delete").hidden = !zone.id;
  el("zone-editor").hidden = false;
  updatePreview();
}

function closeEditor() {
  editing = null;
  el("zone-editor").hidden = true;
  if (preview) { map.removeLayer(preview); preview = null; }
}

async function postZone(body) {
  let resp, data;
  try {
    resp = await fetch("/api/location-zone", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    data = await resp.json();
  } catch (e) {
    el("z-error").textContent = "save failed: " + e;
    return;
  }
  if (!resp.ok || !data || !data.ok) {
    // Leave the panel open on a refusal: the operator has to see which field
    // the server rejected, with what they typed still in front of them.
    el("z-error").textContent = (data && data.error) || ("HTTP " + resp.status);
    return;
  }
  closeEditor();
  tick();
}

function banner(d) {
  const el = document.getElementById("banner");
  const bits = [];
  if (d.mode) bits.push(d.mode);
  if (d.active) bits.push("active: " + d.active.join("+"));
  if (d.environ && d.environ.force_full)
    bits.push("FULL (" + (d.environ.reason || "env") + ")");
  if (d.fix) bits.push((d.fix.speed || 0).toFixed(1) + " m/s");
  else bits.push("no GPS");
  el.textContent = bits.join("  |  ") || "no data";
  el.className = d.environ && d.environ.force_full ? "alert" : "";
}

async function tick() {
  let d;
  try {
    d = await (await fetch("/api/map")).json();
  } catch (e) { return; }
  banner(d);
  drawStations(d.stations || [], d.predictions || []);
  drawEnviron(d.environ);
  if (d.fix) {
    const ll = [d.fix.lat, d.fix.lon];
    if (!vehMarker) vehMarker = L.marker(ll, { icon: vehIcon(d.fix.track),
                                               zIndexOffset: 1000 }).addTo(map);
    else { vehMarker.setLatLng(ll); vehMarker.setIcon(vehIcon(d.fix.track)); }
    crumbPts.push(ll);
    if (crumbPts.length > 1000) crumbPts.shift();
    if (!crumb) crumb = L.polyline(crumbPts, { color: "#4af", weight: 2,
                                               opacity: 0.6 }).addTo(map);
    else crumb.setLatLngs(crumbPts);
    if (firstFix) { map.setView(ll, 13); firstFix = false; }
    else if (follow) map.panTo(ll);
  }
  // Last: where the vehicle is outranks every overlay, so nothing drawn from
  // the location store can cost us the marker update even if it throws.
  drawLocation(d.location_fec);
}

document.getElementById("follow").onclick = function () {
  follow = !follow; this.classList.toggle("on", follow);
};
document.getElementById("radar").onclick = function () {
  radarOn = !radarOn; this.classList.toggle("on", radarOn); loadRadar();
};
document.getElementById("zones").onclick = function () {
  zoneMode = !zoneMode;
  this.classList.toggle("on", zoneMode);
  if (!zoneMode) closeEditor();
  // Redraw so the operator circles pick up (or drop) their click handlers.
  drawLocation(lastLoc);
};
map.on("dragstart", () => {
  follow = false;
  document.getElementById("follow").classList.remove("on");
});
map.on("click", (e) => {
  if (!zoneMode) return;
  openEditor({ lat: e.latlng.lat, lon: e.latlng.lng, level: 2 });
});

el("z-radius").oninput = syncFromSlider;
el("z-radius-m").oninput = syncFromNumber;
el("z-level").onchange = updatePreview;
el("z-cancel").onclick = closeEditor;
el("z-save").onclick = () => {
  if (!editing) return;
  const body = {
    lat: editing.lat, lon: editing.lon,
    radius_m: radiusValue(),
    level: Number(el("z-level").value),
    label: el("z-label").value,
    wans: checkedWans(),
    suppress_learned: el("z-suppress").checked,
  };
  if (editing.id) body.id = editing.id;
  postZone(body);
};
el("z-delete").onclick = () => {
  if (editing && editing.id) postZone({ id: editing.id, delete: true });
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeEditor();
});

tick();
setInterval(tick, 3000);
