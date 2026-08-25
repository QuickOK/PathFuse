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
let vehMarker = null, crumb = null, crumbPts = [];
const stationLayer = L.layerGroup().addTo(map);
const environLayer = L.layerGroup().addTo(map);
const locationLayer = L.layerGroup().addTo(map);
let firstFix = true;

function levelColor(level) {
  return ["#2b2", "#9c2", "#eb0", "#e83", "#e33"][Math.max(0, Math.min(level || 0, 4))];
}

function drawLocation(loc) {
  locationLayer.clearLayers();
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
        const c = L.circle([z.lat, z.lon], {
          radius: z.radius_m, color: "#fff", weight: 2, dashArray: "6 4",
          fillColor: levelColor(z.level), fillOpacity: 0.2,
        }).addTo(locationLayer);
        const tip = document.createElement("span");
        tip.className = "stn-tip";
        tip.textContent = `${z.label}: level ${z.level}` + (z.wans ? ` (${z.wans.join(", ")})` : "");
        c.bindTooltip(tip, { permanent: true, direction: "center" });
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
map.on("dragstart", () => {
  follow = false;
  document.getElementById("follow").classList.remove("on");
});

tick();
setInterval(tick, 3000);
