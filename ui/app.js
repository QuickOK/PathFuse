"use strict";

const $  = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

/* ---------- layout toggle (ops | wall) ---------- */
(function initLayoutToggle(){
  const VALID = new Set(["wall", "ops"]);
  const apply = (mode) => {
    if (!VALID.has(mode)) return;
    document.body.dataset.layout = mode;
    document.querySelectorAll("[data-layout-pick]").forEach(b => {
      const active = b.dataset.layoutPick === mode;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
    try { localStorage.setItem("sbfd_layout", mode); } catch(e){}
  };
  document.querySelectorAll("[data-layout-pick]").forEach(b => {
    b.addEventListener("click", () => apply(b.dataset.layoutPick));
  });
  apply(document.body.dataset.layout || "ops");
})();

let lastApplyAt = 0;
const dirtyFields = new Set();
let lastState = null;
let lastEngarde = null;

const FORM_SELECTOR = 'input[name="mode"], input[name="policy"], input[name="egress_mode"], input[name="fec_mode"], #fec-fixed-ratio, #fec-fixed-custom, #fec-floor-ratio, #fec-floor-custom, input[name="environmental_enabled"], input[name="maintenance_enabled"], #maintenance-hour, #master-wan, #persist';

/* A control's identity for dirty/focus tracking: radios share a name, the
   rest are unique ids. */
function fieldKey(el){ return el.name || el.id; }

function focusedField(){
  const a = document.activeElement;
  return (a && a.matches(FORM_SELECTOR)) ? fieldKey(a) : null;
}

/* Freeze only the control the operator is actually working on. Everything
   else keeps tracking live state, so a page left open across a controller
   restart can't re-post a stale value as deliberate intent. */
function isFrozen(key){ return dirtyFields.has(key) || focusedField() === key; }

const FEC_RATIO_FALLBACK_PRESETS = ["20:1", "8:1", "8:2", "8:4", "8:6", "8:8"];
const FEC_CUSTOM = "__custom__";
const ratioPresetsRendered = {};

/* Render presets + a Custom… option into `selId`. When the effective value is
   not a preset (the operator typed a percent that resolved off-list), select
   Custom… and put the canonical ratio in the box, so a reload round-trips
   instead of snapping back to a preset. The server does the resolving; this
   only ever displays what it sent back. */
function syncRatioDropdown(selId, customId, presets, desired, withAuto){
  const sel = $(selId);
  if (!sel) return;
  const list = Array.isArray(presets) && presets.length ? presets : FEC_RATIO_FALLBACK_PRESETS;
  // Fold the auto flag into the cache key so switching it rebuilds the
  // option list instead of reusing one missing (or wrongly holding) the
  // auto option.
  const key = (withAuto ? "auto|" : "") + list.join("|");
  if (key !== ratioPresetsRendered[selId]){
    sel.innerHTML = "";
    if (withAuto){
      const a = document.createElement("option");
      a.value = "auto"; a.textContent = "auto (per-WAN profile)";
      sel.appendChild(a);
    }
    list.forEach(r => {
      const o = document.createElement("option");
      o.value = r; o.textContent = r;
      sel.appendChild(o);
    });
    const c = document.createElement("option");
    c.value = FEC_CUSTOM; c.textContent = "Custom…";
    sel.appendChild(c);
    ratioPresetsRendered[selId] = key;
  }
  const custom = $(customId);
  if (desired){
    if (desired === "auto" && withAuto){
      if (sel.value !== "auto") sel.value = "auto";
      if (custom && !isFrozen(customId.slice(1))) custom.value = "";
    } else if (list.includes(desired)){
      if (sel.value !== desired) sel.value = desired;
      if (custom && !isFrozen(customId.slice(1))) custom.value = "";
    } else {
      sel.value = FEC_CUSTOM;
      if (custom && !isFrozen(customId.slice(1))) custom.value = desired;
    }
  }
  if (custom) custom.hidden = sel.value !== FEC_CUSTOM;
}

function pad(n, w=2){ return String(n).padStart(w, "0"); }
function fmtClock(d){ return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }
function fmtClockMs(d){ return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(),3)}`; }
function fmtUptime(secs){
  if (secs == null || !isFinite(secs) || secs < 0) return "—";
  secs = Math.floor(secs);
  const d = Math.floor(secs/86400); secs%=86400;
  const h = Math.floor(secs/3600);  secs%=3600;
  const m = Math.floor(secs/60);
  const s = secs%60;
  if (d>0) return `${d}d ${pad(h)}:${pad(m)}:${pad(s)}`;
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}
function fmtBytes(n){
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const u = ["KB","MB","GB","TB"];
  let v = n/1024, i = 0;
  while (v >= 1024 && i < u.length-1){ v/=1024; i++; }
  return `${v.toFixed(v>=10?0:1)} ${u[i]}`;
}

/* ---------- live clock (browser-local time) + time-in-state ticker (250ms) ---------- */
setInterval(() => {
  const now = new Date();
  const c = $("#clock"); if (c) c.textContent = fmtClock(now);
  const t = $("#tick-time"); if (t) t.textContent = fmtClockMs(now);

  if (lastState){
    document.querySelectorAll("[data-since]").forEach(el => {
      const since = parseFloat(el.dataset.since);
      if (isFinite(since)) el.textContent = fmtUptime((now.getTime()/1000) - since);
    });
    if (lastState.failback_remaining_s > 0){
      const start = lastState._fbStart || (lastState._fbStart = now.getTime());
      const elapsed = (now.getTime() - start)/1000;
      const remain = Math.max(0, lastState.failback_remaining_s - elapsed);
      const fbT = $("#failback-text"); if (fbT) fbT.textContent = `${remain.toFixed(1)}s — fallback to master pending`;
      const fillPct = lastState._fbInitial ? Math.max(0, 100 * (1 - remain/lastState._fbInitial)) : 0;
      const fbF = $("#failback-fill"); if (fbF) fbF.style.width = `${fillPct}%`;
    }
  }
}, 250);

function pulse(){
  const el = $("#pulse"); if (!el) return;
  el.classList.add("on");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("on"), 180);
}

/* ---------- fetchers ---------- */
async function fetchState(){
  try {
    const r = await fetch("/api/state", {cache:"no-store"});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    lastState = s;
    render(s);
    pulse();
    setTickStatus("ok", "all systems nominal");
  } catch (e){
    setTickStatus("bad", `state error · ${e}`);
    // Keep the FEC graphs advancing even while the feed is down. Their window
    // is anchored to wall-clock now, but only a redraw applies that -- and the
    // success path is the only thing that redraws. Without this the canvas
    // freezes with the last sample still sitting under the "now" tick, which is
    // precisely the false-currency the wall-clock anchor exists to prevent:
    // the feed being dead is exactly when it misleads. Redrawing here ages the
    // data off the right edge instead, from the history we already hold.
    ["c2r", "r2c"].forEach(dir => drawFecGraph(`fec-${dir}-graph`, fecHist[dir]));
  }
}
async function fetchEngarde(){
  try {
    const r = await fetch("/api/engarde", {cache:"no-store"});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    lastEngarde = s;
    renderConsist(s);
  } catch (e){
    renderConsist({ok:false, error:String(e)});
  }
}

function setTickStatus(cls, text){
  const el = $("#tick-status"); if (!el) return;
  el.className = "tick-status " + cls;
  el.textContent = text;
}

/* ---------- helpers ---------- */
function statusClass(s){
  s = (s || "UNKNOWN").toUpperCase();
  if (s === "UP")   return "up";
  if (s === "DOWN") return "down";
  return "unk";
}
function rttClass(rtt){
  if (rtt == null) return "";
  if (rtt > 200) return "bad";
  if (rtt > 100) return "warn";
  return "";
}
function lossClass(loss){
  if (loss == null) return "";
  if (loss > 5) return "bad";
  if (loss > 1) return "warn";
  return "";
}

/* ---------- master render ---------- */
function render(s){
  const wans     = Object.keys(s.client_local || {});
  const active   = new Set(s.active_wans || []);
  const dyn      = s.dynamic || {};
  const masterWan = (s.mode === "master_backup")
    ? (s.master_policy === "dynamic" ? dyn.master : s.master_wan)
    : null;

  // Active uplinks first, master-among-active first, then alphabetical.
  // `active` is the set sbfd-ctl is currently steering traffic through; in
  // full-redundancy mode every UP wan is typically active, in master_backup
  // mode it's just the master.
  wans.sort((a, b) => {
    const aActive = active.has(a) ? 0 : 1;
    const bActive = active.has(b) ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    if (masterWan) {
      const aMaster = a === masterWan ? 0 : 1;
      const bMaster = b === masterWan ? 0 : 1;
      if (aMaster !== bMaster) return aMaster - bMaster;
    }
    return a.localeCompare(b);
  });

  const eff      = s.effective || {};
  const upCount  = wans.filter(w => (eff[w]||"").toUpperCase() === "UP").length;
  const wanLabel = (w) => (s.wan_labels && s.wan_labels[w]) || w;

  /* top bar */
  $("#station").textContent = s.engarde_server || "—";

  /* KPI: System */
  const sysEl  = $("#kpi-system");
  const sysVal = $("#kpi-system-val");
  const sysSub = $("#kpi-system-sub");
  if (wans.length === 0) {
    sysEl.dataset.state = "";
    sysVal.textContent  = "—";
    sysSub.textContent  = "no wans";
  } else if (active.size === 0) {
    sysEl.dataset.state = "down";
    sysVal.textContent  = "Offline";
    sysSub.textContent  = `0 / ${wans.length} paths active`;
  } else if (upCount === wans.length) {
    sysEl.dataset.state = "ok";
    sysVal.textContent  = "Online";
    sysSub.textContent  = `${upCount} / ${wans.length} paths up`;
  } else {
    sysEl.dataset.state = "degraded";
    sysVal.textContent  = "Degraded";
    sysSub.textContent  = `${upCount} / ${wans.length} paths up`;
  }

  /* KPI: Mode */
  const modeShort = s.mode === "master_backup" ? "Master / Backup" : "Full Redundancy";
  $("#kpi-mode-val").textContent = modeShort;
  $("#kpi-mode-sub").textContent = s.mode === "master_backup"
    ? `policy · ${s.master_policy || "—"}`
    : "all paths active";

  /* KPI: Active */
  const activeLabels = (s.active_wans || []).map(wanLabel);
  $("#kpi-active-val").textContent = activeLabels.length ? activeLabels.join(" + ") : "none";
  $("#kpi-active-sub").textContent = masterWan ? `master · ${wanLabel(masterWan)}` : "—";

  /* KPI: relay sync */
  const syncEl  = $("#kpi-sync");
  const syncVal = $("#kpi-sync-val");
  const syncSub = $("#kpi-sync-sub");
  if (s.relay_remote && s.relay_remote.ok) {
    syncEl.dataset.state = "ok";
    const stale = s.relay_remote.stale_s;
    syncVal.textContent  = stale != null ? `${stale.toFixed(1)}s` : "fresh";
    syncSub.textContent  = "remote view ok";
  } else {
    syncEl.dataset.state = "degraded";
    syncVal.textContent  = "stale";
    syncSub.textContent  = (s.relay_remote && s.relay_remote.error) ? "client-local only" : "no remote";
  }

  /* link sub */
  $("#links-sub").textContent = `${wans.length} wan${wans.length===1?"":"s"} · ${upCount} up`;

  /* populate master-wan select once */
  const sel = $("#master-wan");
  if (sel && !sel.options.length){
    for (const w of wans){
      const o = document.createElement("option");
      o.value = w;
      o.textContent = wanLabel(w);
      sel.appendChild(o);
    }
  }

  /* populate the maintenance-hour select once: 00..23 local */
  const hourSel = $("#maintenance-hour");
  if (hourSel && !hourSel.options.length){
    for (let h = 0; h < 24; h++){
      const o = document.createElement("option");
      o.value = String(h);
      o.textContent = String(h).padStart(2, "0") + ":00";
      hourSel.appendChild(o);
    }
  }

  /* form sync — per-field dirty/focus protection so we don't clobber operator
     edits, while untouched controls keep tracking live state. Apply() posts
     the whole panel, so a control left showing a stale value would otherwise
     be re-applied as though the operator had chosen it. */
  if (Date.now() - lastApplyAt > 5000){
    const setRadio = (name, val) => {
      if (isFrozen(name)) return;
      $$(`input[name="${name}"]`).forEach(r => r.checked = (r.value === val));
    };
    setRadio("mode", s.mode);
    setRadio("policy", s.master_policy);
    setRadio("egress_mode", s.egress_mode || "relay_vpn");
    if (s.master_wan && sel && !isFrozen("master-wan")) sel.value = s.master_wan;
    if (s.fec && s.fec.configured){
      const desiredMode = s.fec.desired_mode
        || (s.fec.desired_enabled ? "adaptive" : "off");
      setRadio("fec_mode", desiredMode);
      // Presets always render; the selected value holds while frozen.
      const presets = s.fec.ratio_presets || s.fec.fixed_ratio_presets;
      // The select and its custom box are one control: typing in the box must
      // freeze the pair, or a poll re-selects a preset and hides the box
      // mid-entry.
      const fixedFrozen = isFrozen("fec-fixed-ratio") || isFrozen("fec-fixed-custom");
      const floorFrozen = isFrozen("fec-floor-ratio") || isFrozen("fec-floor-custom");
      syncRatioDropdown("#fec-fixed-ratio", "#fec-fixed-custom", presets,
        fixedFrozen ? null : s.fec.desired_fixed_ratio);
      syncRatioDropdown("#fec-floor-ratio", "#fec-floor-custom", presets,
        floorFrozen ? null : (("floor_override" in (s.fec || {}))
          ? (s.fec.floor_override === null ? "auto" : s.fec.floor_override)
          : s.fec.floor_ratio),
        true);
    }
    const env = s.environmental || {};
    if (env.configured) setRadio("environmental_enabled", env.enabled ? "on" : "off");
    const maint = s.maintenance || {};
    if (maint.configured){
      setRadio("maintenance_enabled", maint.enabled ? "on" : "off");
      if (hourSel && !isFrozen("maintenance-hour")
          && typeof maint.hour === "number"){
        hourSel.value = String(maint.hour);
      }
    }
    const persistBox = $("#persist");
    if (persistBox && typeof s.persist === "boolean" && !isFrozen("persist")){
      persistBox.checked = s.persist;
    }
  }

  renderWanList(s, wans, active, masterWan, dyn);
  renderSignalDiagram(s, wans, active, masterWan);
  renderBoard(s);
  renderDynamic(s);
  renderFailback(s);
  renderFec(s);
  renderCellSignal(s);
  renderEnvironmental(s);
  renderMaintenance(s);

  /* Local-Direct + master-DOWN red badge */
  const warn = $("#egress-warn");
  if (warn) {
    const masterState = (s.client_local && s.client_local[s.master_wan] || {}).state;
    const inLocalDirect = (s.egress_mode === "local_direct");
    warn.hidden = !(inLocalDirect && masterState === "DOWN");
  }

  /* Engarde-PBR-missing red badge — base config drift indicator */
  const pbrWarn = $("#engarde-pbr-warn");
  if (pbrWarn) {
    const et = s.engarde_table || {};
    pbrWarn.hidden = !!(et.dev);  // dev present means table is provisioned
  }

  if (s.relay_remote){
    const r = s.relay_remote;
    $("#relay-info").textContent = r.ok
      ? `relay state · fresh ${r.stale_s != null ? r.stale_s.toFixed(1)+"s ago" : "now"}`
      : `relay unavailable · ${r.error || "unknown"}`;
  }
}

function renderWanList(s, wans, active, masterWan, dyn){
  const wrap = $("#wan-list");
  wrap.innerHTML = "";
  wans.forEach((w) => {
    const local  = s.client_local[w] || {};
    const remote = (s.relay_remote && s.relay_remote.states && s.relay_remote.states[w]) || {};
    const eff    = (s.effective || {})[w] || "UNKNOWN";
    const label  = (s.wan_labels && s.wan_labels[w]) || w;
    const rtt    = (local.rtt_ms   != null) ? local.rtt_ms   : remote.rtt_ms;
    const loss   = (local.loss_pct != null) ? local.loss_pct : remote.loss_pct;
    const since  = (local.state_since != null) ? local.state_since : remote.state_since;
    const isMaster = masterWan === w;
    const isActive = active.has(w);
    const sCls = statusClass(eff);

    const tags = [];
    if (isMaster) tags.push(`<span class="tag master">master</span>`);
    // "hot standby" only when the link is actually UP — a DOWN backup is
    // muted *and* unusable, so it reads "standby"; an unprobed link mustn't
    // masquerade as a known-good or known-bad backup (Greptile PR#7 P2).
    tags.push(isActive
      ? `<span class="tag active">active</span>`
      : `<span class="tag dropped">${eff === "UP" ? "hot standby" : eff === "DOWN" ? "standby" : "unknown"}</span>`);
    if (dyn.candidate === w && dyn.master !== w) {
      tags.push(`<span class="tag candidate">dyn-candidate</span>`);
    }

    const fecDir = s.fec && s.fec.directions && s.fec.directions.client_to_relay;
    if (fecDir && fecDir.driver_wan === w && s.fec.desired_enabled) {
      tags.push(`<span class="tag candidate">fec driver</span>`);
    }

    const card = document.createElement("div");
    card.className = `wan-card is-${sCls}` + (isMaster ? " is-master" : "");
    card.innerHTML = `
      <div class="wan-head">
        <div class="wan-name">${escapeHtml(label)}</div>
        <div class="wan-iface">${escapeHtml(w)} · client ${escapeHtml(local.state||"?")} · relay ${escapeHtml(remote.state||"?")}</div>
      </div>
      <div class="wan-status">
        <span class="pip ${sCls}"></span>${escapeHtml(eff)}
      </div>
      <div class="wan-meters">
        <div class="meter ${rttClass(rtt)}">
          <div class="meter-lbl">rtt</div>
          <div class="meter-val">${rtt != null ? rtt.toFixed(1) : "—"}<span class="unit">ms</span></div>
        </div>
        <div class="meter ${lossClass(loss)}">
          <div class="meter-lbl">loss</div>
          <div class="meter-val">${loss != null ? loss.toFixed(2) : "—"}<span class="unit">%</span></div>
        </div>
        <div class="meter">
          <div class="meter-lbl">${escapeHtml(eff.toLowerCase())} for</div>
          <div class="meter-val" data-since="${since != null ? since : ""}">${since != null ? fmtUptime((Date.now()/1000) - since) : "—"}</div>
        </div>
      </div>
      <div class="wan-tags">${tags.join("")}</div>`;
    wrap.appendChild(card);
  });
}

function renderSignalDiagram(s, wans, active, masterWan){
  const eff = s.effective || {};
  const W = 600, H = 260;
  const clientX = 96, sbfdX = 300, engX = 504;
  const yMid = H / 2;
  const yStep = wans.length > 1 ? Math.min(80, (H-60)/(wans.length+1)) : 0;
  const yStart = yMid - ((wans.length-1)*yStep)/2;

  const edgesEl  = document.getElementById("edges");
  const labelsEl = document.getElementById("wan-labels");
  edgesEl.innerHTML  = "";
  labelsEl.innerHTML = "";

  wans.forEach((w, i) => {
    const y = yStart + i*yStep;
    const state = (eff[w]||"UNKNOWN").toUpperCase();
    const cls = state === "UP" ? "up" : state === "DOWN" ? "down" : "unk";
    const isMaster = masterWan === w;
    const isActive = active.has(w);
    const extra = (isMaster ? " master" : "") + (!isActive ? " dropped" : "");

    const midX1 = (clientX + sbfdX)/2;
    const path1 = `M ${clientX} ${yMid} C ${midX1} ${yMid} ${midX1} ${y} ${sbfdX-42} ${y}`;
    const linkOk = state === "UP" && isActive;
    const midX2 = (sbfdX + engX)/2;
    const path2 = `M ${sbfdX+42} ${y} C ${midX2} ${y} ${midX2} ${yMid} ${engX-46} ${yMid}`;

    edgesEl.insertAdjacentHTML("beforeend",
      `<path class="edge ${cls}${extra}" d="${path1}"/>` +
      `<path class="edge ${linkOk ? "up" : cls}${extra}" d="${path2}" opacity="${linkOk ? 0.95 : 0.18}"/>`
    );

    // Labels stack above/below the SBFD DECIDE node (centered on it),
    // with upper-half WANs labeled above and lower-half below. Keeps the
    // node's polygon clear of text overlap.
    const sbfdTop = 100, sbfdBot = 160;  // matches the diamond polygon at y=130
    const isAbove = y < yMid;
    // Push labels ~24 SVG-px (≈ 1/4 inch at 96 dpi) clear of the link lines.
    const labelY = isAbove ? sbfdTop - 42 : sbfdBot + 38;
    const subY   = labelY + 12;
    const label  = (s.wan_labels && s.wan_labels[w]) || w;
    labelsEl.insertAdjacentHTML("beforeend",
      `<text class="wan-tag" x="${sbfdX}" y="${labelY}" text-anchor="middle">${escapeHtml(label)}</text>` +
      `<text class="wan-tag-sub" x="${sbfdX}" y="${subY}" text-anchor="middle">${escapeHtml(w)} · ${escapeHtml(state)}${isMaster?" · master":""}</text>`
    );
  });

  // Egress-mode overlay: relabels the engarde node + panel subtitle and dims
  // the BFD edges when the client-device flow is bypassing the tunnel entirely.
  const egressMode = s.egress_mode || "relay_vpn";
  const exit = egressMode === "relay_vpn"        ? { tag: "→ egress relay-VPN",  sub: "client → engarde → egress relay-VPN" }
             : egressMode === "relay_direct"  ? { tag: "→ relay WAN",  sub: "client → engarde → relay WAN" }
             : egressMode === "local_direct"? { tag: "BYPASSED",   sub: "client → local WAN (engarde bypassed)" }
             :                                { tag: "?",          sub: `unknown egress: ${egressMode}` };
  const flowSub = document.getElementById("signal-flow-sub");
  if (flowSub) flowSub.textContent = exit.sub;
  const eSub = document.getElementById("engarde-sub");
  if (eSub) eSub.textContent = exit.tag;
  const svg = document.getElementById("signal-svg");
  if (svg) svg.classList.toggle("bypass", egressMode === "local_direct");
}

function renderBoard(s){
  const rows = (s.recent_switches || []).slice().reverse().slice(0, 10);
  const tbody = $("#board");
  const empty = $("#board-empty");
  if (!rows.length){
    tbody.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  tbody.innerHTML = rows.map(h => {
    const t = new Date(h.ts*1000);
    const when = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
    const from = (h.from || []).join(" + ") || "—";
    const to   = (h.to   || []).join(" + ") || "—";
    return `<tr>
      <td class="when">${when}</td>
      <td class="from">${escapeHtml(from)}</td>
      <td><span class="arrow">→</span></td>
      <td>${escapeHtml(to)}</td>
      <td class="reason">${escapeHtml(h.reason || "")}</td>
    </tr>`;
  }).join("");
}

function renderDynamic(s){
  const block = $("#dynamic-readout");
  if (s.master_policy !== "dynamic" || s.mode !== "master_backup"){
    block.hidden = true; return;
  }
  block.hidden = false;
  const dyn = s.dynamic || {};
  $("#dyn-master").textContent    = dyn.master || "—";
  $("#dyn-candidate").textContent = dyn.candidate || "—";
  $("#dyn-dwell").textContent     = dyn.swap_dwell_remaining_s > 0
    ? `${dyn.swap_dwell_remaining_s.toFixed(1)}s`
    : "—";
}

function renderFailback(s){
  const blk = $("#failback-block");
  if (!s.failback_remaining_s || s.failback_remaining_s <= 0){
    blk.hidden = true; return;
  }
  blk.hidden = false;
  if (!s._fbInitial || s._fbInitial < s.failback_remaining_s) s._fbInitial = s.failback_remaining_s;
  s._fbStart = Date.now();
  $("#failback-text").textContent = `${s.failback_remaining_s.toFixed(1)}s — fallback to master pending`;
  $("#failback-fill").style.width = "0%";
}

/* ---------- engarde sockets ---------- */
function renderConsist(e){
  const tbody = $("#consist-rows");
  const empty = $("#consist-empty");
  const sub   = $("#consist-sub");

  if (!e || !e.ok){
    sub.textContent = `engarde offline${e && e.error ? " · "+e.error : ""}`;
    tbody.innerHTML = "";
    empty.textContent = "Engarde port 8080 unreachable";
    empty.hidden = false;
    return;
  }

  const d = e.data || {};
  const ver = (d.version || "").split(" ")[0] || "—";
  sub.textContent = `${(d.description||"engarde")} · ${ver} · ${d.listenAddress||"?"} → ${d.dstAddress||"?"}`;

  // engarde-client returns `interfaces` (uplink list); engarde-server returns `sockets` (connected peers).
  // Normalize both to a uniform row shape.
  const wanIfaces = new Set(e.wan_ifaces || []);
  let rows = [];
  if (Array.isArray(d.interfaces) && d.interfaces.length) {
    rows = d.interfaces
      // Non-excluded rows: require a senderAddress to hide engarde's ifb-*
      // shaper devices, which aren't real uplinks. Excluded rows: engarde
      // reports a runtime-excluded WAN and a config-excluded non-WAN (LAN,
      // tunnels) identically, so keep only managed WANs — the proxy's
      // wan_ifaces — and show them as "standby": in master_backup mode
      // sbfd-ctl excludes the backup WAN on purpose while SBFD keeps probing.
      .filter(i => i.status === "excluded" ? wanIfaces.has(i.name) : i.senderAddress)
      .map(i => ({
        label: i.name + (i.dstAddress ? ` → ${i.dstAddress}` : ""),
        last: typeof i.last === "number" ? i.last : null,
        status: i.status === "excluded" ? "standby" : i.status,  // "active" | "idle" | "standby"
      }));
  } else if (Array.isArray(d.sockets) && d.sockets.length) {
    rows = d.sockets.map(s => {
      const peer = s.address || s.remoteAddress || s.peer || `${s.remoteHost||"?"}:${s.remotePort||"?"}`;
      const last = typeof s.last === "number" ? s.last : null;
      const isStale = last != null && last > 30;
      return { label: String(peer), last, status: isStale ? "stale" : "active" };
    });
  }

  if (!rows.length){
    tbody.innerHTML = "";
    empty.textContent = (d.interfaces && d.interfaces.length)
      ? "No real uplinks reported"
      : "Standby — no peers bound";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  tbody.innerHTML = rows.map((r, i) => {
    const idle = r.last == null ? "—"
                : r.last === 0 ? "live"
                : r.last < 1   ? "<1s"
                : `${r.last.toFixed(1)}s`;
    const statCls = r.status === "active" ? "stat-up"
                  : r.status === "stale"  ? "stat-down"
                  : "";  // "idle" → default color
    return `<tr>
      <td>${i+1}</td>
      <td>${escapeHtml(r.label)}</td>
      <td>${escapeHtml(idle)}</td>
      <td class="${statCls}">${escapeHtml(r.status)}</td>
    </tr>`;
  }).join("");
}

/* ---------- FEC ---------- */
const FEC_MODE_LABELS = {
  off: "off — forced 8:0",
  fixed: "fixed",
  adaptive: "adaptive",
  min_adaptive: "floor + adaptive",
};

function renderFec(s){
  const fec  = s.fec || {};
  const dirs = fec.directions || {};
  const sub  = $("#fec-sub");
  const desiredMode = fec.desired_mode || (fec.desired_enabled ? "adaptive" : "off");
  const modeLabel   = FEC_MODE_LABELS[desiredMode] || desiredMode;
  if (sub) sub.textContent = !fec.configured ? "not configured"
                        : (desiredMode === "fixed"
                            ? `fixed ${fec.desired_fixed_ratio || ""}`.trim()
                            : modeLabel);

  renderFecCard("c2r", dirs.client_to_relay, true);
  renderFecCard("r2c", dirs.relay_to_client, false);

  const tNow = (typeof s.ts === "number") ? s.ts : Date.now() / 1000;
  if (fecHistSeeded){
    pushFecSample("c2r", tNow, (dirs.client_to_relay || {}).rx || null);
    pushFecSample("r2c", tNow, (dirs.relay_to_client || {}).rx || null);
  }
  drawFecGraph("fec-c2r-graph", fecHist.c2r);
  drawFecGraph("fec-r2c-graph", fecHist.r2c);

  const eff = $("#fec-effective");
  if (eff){
    if (!fec.configured){
      eff.textContent = "FEC not configured";
    } else {
      const o = dirs.relay_to_client || {};
      const relayTxt = !o.ok ? "relay sync pending"
                   : (o.reconcile_pending ? "syncing relay…" : "relay synced");
      const effDesc = desiredMode === "fixed"
        ? `fixed ${fec.desired_fixed_ratio || ""}`.trim()
        : (desiredMode === "min_adaptive"
            ? `${modeLabel} (floor ${fec.floor_ratio || "?"})`
            : modeLabel);
      // The relay reports the floor it is actually running. During a rolling
      // upgrade an older relay omits it (undefined — not a mismatch); a
      // genuinely different value means the two directions disagree, which is
      // worth showing rather than hiding behind our local value.
      const relayFloor = o.floor_ratio;
      const floorMismatch = desiredMode === "min_adaptive"
        && relayFloor && fec.floor_ratio && relayFloor !== fec.floor_ratio;
      eff.textContent = `effective: ${effDesc} · ${relayTxt}`
        + (floorMismatch ? ` · relay floor ${relayFloor}` : "");
      eff.classList.toggle("warn", !!floorMismatch);
    }
  }
  // Radios & dropdown availability follow whether FEC is configured at all,
  // plus the dropdown is only meaningful while Fixed mode is selected.
  $$('input[name="fec_mode"]').forEach(r => r.disabled = !fec.configured);
  const checkedMode = document.querySelector('input[name="fec_mode"]:checked')?.value;
  const fixedSel = $("#fec-fixed-ratio");
  const floorSel = $("#fec-floor-ratio");
  if (fixedSel) fixedSel.disabled = !fec.configured || checkedMode !== "fixed";
  if (floorSel) floorSel.disabled = !fec.configured || checkedMode !== "min_adaptive";
  const fixedCustom = $("#fec-fixed-custom");
  const floorCustom = $("#fec-floor-custom");
  if (fixedCustom) fixedCustom.disabled = fixedSel ? fixedSel.disabled : true;
  if (floorCustom) floorCustom.disabled = floorSel ? floorSel.disabled : true;
}

function renderCellSignal(s){
  const line = $('#fec-cell-line');
  if (!line) return;
  const c = s.cell;
  if (!c || !c.configured){ line.hidden = true; return; }
  line.hidden = false;
  const fmt = (v, unit) => (v == null ? '—' : `${v}${unit}`);
  const parts = [
    `RSRQ ${fmt(c.rsrq, ' dB')}`,
    `RSRP ${fmt(c.rsrp, ' dBm')}`,
    `SINR ${fmt(c.sinr, ' dB')}`,
  ];
  if (c.band) parts.push(c.band);
  const fec = s.fec || {};
  const prof = fec.profile && fec.profile.name && fec.profile.name !== 'default'
    ? ` · profile ${fec.profile.name}` : '';
  const floor = fec.signal_floor_active ? ' · signal floor' : '';
  const dup = s.duplication || {};
  const dupBadge = dup.active
    ? ` · DUPLICATING (${dup.last_reason || 'handoff'})`
    : (dup.count ? ` · ${dup.count} handoff window${dup.count === 1 ? '' : 's'}` : '');
  const stale = c.stale ? ' (stale)' : '';
  const cellLabel = c.wan ? ((s.wan_labels && s.wan_labels[c.wan]) || c.wan) : 'cell';
  $('#fec-cell-readout').textContent =
    `${cellLabel} signal: ${parts.join(' · ')}${stale}${prof}${floor}${dupBadge}`;
}

/* The pip row shows the applied FEC level RELATIVE to what the mode makes
   available: in floor+adaptive the floor rung is the baseline (at the floor,
   nothing is lit — the parity it buys is the resting state, not an event), and
   only rungs the active profile actually has get a dot. The cellular table is
   4 rungs, the base table 5, so a fixed-width row would over- or under-state
   the headroom on one of them. */
const FEC_FALLBACK_LEVELS = 5;   // relay too old to publish `ladder`

function clampLevel(v, hi){
  if (typeof v !== "number" || !isFinite(v)) return 0;
  // Floor, not round: the controller maps a between-rungs ratio to the rung
  // below it, and the row must not disagree with that.
  return Math.max(0, Math.min(Math.floor(v), hi));
}

/* The scale-based row: one fixed set of rungs spanning every profile, with the
   span currently REACHABLE shaded behind them. A position means one ratio no
   matter which profile drives, so the shaded band says which profile is
   driving and how much room the mode leaves it — a single shaded dot in full
   redundancy is the backoff pinning the leg, not a missing ladder. */
function renderFecScale(el, d, lad){
  const scale = lad.scale;
  const n = scale.length;
  const lo = numOr(lad.reach_lo, -1), hi = numOr(lad.reach_hi, -1);
  const applied = numOr(lad.applied_index, -1);
  const floorIdx = numOr(lad.floor_index, -1);
  const below = lad.below_floor === true;
  const pinned = lad.pinned === true;
  // Prefer the ratio the payload reports over the rung it landed on: when the
  // two disagree the reported one is the truth and the rung is our rounding.
  const shown = d.ratio || scale[applied];

  let label;
  if (below) label = "below floor";
  else if (d.mode === "off" || d.enabled === false) label = "off";
  // Off the scale entirely — a relay reporting a ratio from settings we have
  // not pushed yet, or have just replaced. Name it rather than showing a dash:
  // the row cannot place it, but the operator can still read it.
  else if (applied < 0) label = d.ratio ? `${d.ratio} · off scale` : "—";
  else if (d.mode === "fixed") label = `fixed · ${shown}`;
  else if (floorIdx >= 0 && applied <= floorIdx)
    label = pinned ? "at floor · pinned" : "at floor";
  else if (floorIdx >= 0) label = `+${applied - floorIdx} over floor`;
  else label = `${shown}`;
  // `pinned` follows the ROUTING mode, not the FEC mode, so it can be true
  // while floorIdx is -1 (plain adaptive has no floor). Gating the suffix on
  // floorIdx therefore dropped it exactly where the row most needs it. Append
  // wherever the label hasn't already accounted for the state.
  const pinnedNamed = below || applied < 0 || d.mode === "off"
    || d.enabled === false || (floorIdx >= 0 && applied <= floorIdx);
  if (pinned && !pinnedNamed) label += " · pinned";

  // `pinned` belongs in the signature even though it usually moves the span
  // with it: below-floor labels ignore pinned, so backoff could engage or
  // release with lo/hi/applied/label all unchanged and leave the class stale.
  const sig = `s${scale.join(",")}|${lo}|${hi}|${applied}|${label}|${below}|${pinned}`;
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;

  let pips = "";
  for (let i = 0; i < n; i++){
    const cls = ["pip"];
    if (lo >= 0 && i >= lo && i <= hi) cls.push("reach");
    if (i === applied) cls.push("on");
    pips += `<i class="${cls.join(" ")}" title="${escapeHtml(scale[i])}"></i>`;
  }
  el.innerHTML = `${pips}<span class="fec-level-num">${escapeHtml(label)}</span>`;
  // Flash only when loss has pushed the leg above its floor. A pinned leg
  // cannot get there, so a pinned row never flashes.
  el.classList.toggle("is-flashing", !below && floorIdx >= 0 && applied > floorIdx);
  el.classList.toggle("is-below-floor", below);
  el.classList.toggle("is-pinned", pinned);
  // Gates the out-of-reach styling: legacy rows have no reachable band, and
  // without this every one of their pips would match :not(.reach) and lose the
  // hollow accent ring.
  el.classList.add("is-scale");
}

function numOr(v, dflt){
  return (typeof v === "number" && isFinite(v)) ? Math.floor(v) : dflt;
}

function renderFecLevel(el, d){
  if (!el) return;
  const lad = d.ladder;
  // Scale-based row when the controller published one; the older
  // profile-relative shape below stays for a payload that predates it.
  if (lad && Array.isArray(lad.scale) && lad.scale.length){
    renderFecScale(el, d, lad);
    return;
  }
  // Without a ladder, fall back to the base table with no floor rung: the row
  // then reads as plain adaptive, which is wrong only in how much headroom it
  // draws — never in claiming parity that isn't applied.
  // Through clampLevel so a malformed payload can't make `dots` NaN (an empty
  // row and a "+NaN" label) or ask for thousands of nodes.
  const levels   = clampLevel(lad && lad.levels, 32) || FEC_FALLBACK_LEVELS;
  const floorLvl = clampLevel(lad ? lad.floor_level : 0, levels - 1);
  const applied  = clampLevel(lad ? lad.applied_level : d.level, levels - 1);
  const dots = (levels - 1) - floorLvl;
  const lit  = Math.max(0, Math.min(applied - floorLvl, dots));
  // Same off test renderFecCard uses for the card's is-off state, so the two
  // can't disagree: a floor of 8:0 (reachable by typing 0% into the custom
  // field) is min_adaptive by mode but carries no parity at all, and the card
  // already labels that ratio OFF.
  const off = (d.enabled === false) || (d.ratio === "8:0");
  const minAdaptive = d.mode === "min_adaptive";
  const floored = !off && minAdaptive;
  // The ratio on the wire can sit BELOW the floor: raise the floor while the
  // actuator is down and the controller keeps publishing the last ratio it
  // actually got applied. `lit` clamps that to zero, which is indistinguishable
  // from resting at the floor — so the floor would appear to be holding when
  // its parity is exactly what is missing.
  //
  // Prefer the server's flag, which compares the ratios themselves; the rung
  // compare is the fallback for a relay too old to send it, and is coarser (a
  // floor between two rungs shares a position with the rung below it).
  const belowFloor = minAdaptive && (lad && typeof lad.below_floor === "boolean"
    ? lad.below_floor
    : applied < floorLvl);

  let label;
  // Ahead of the off label: a min_adaptive leg carrying no parity at all is the
  // worst case of below-floor, and "off" would read as an intended state.
  if (belowFloor) label = "below floor";
  else if (off || d.mode === "off") label = "off";
  else if (floored) label = dots <= 0 ? "floor at max"
                     : (lit === 0 ? "at floor" : `+${lit} over floor`);
  else if (d.mode === "fixed") label = dots > 0 ? `fixed · ${lit}/${dots}` : "fixed";
  else label = `level ${lit}/${dots}`;

  // Rebuild only when the row's meaning changes. Re-writing innerHTML on every
  // poll would restart the flash animation once a second (it would never get
  // past its first frame); rebuilding on change instead also keeps every lit
  // dot pulsing in phase, since they all start their cycle together.
  const sig = `${dots}|${lit}|${label}`;
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;

  let pips = "";
  for (let i = 0; i < dots; i++) pips += `<i class="pip ${i < lit ? "on" : ""}"></i>`;
  el.innerHTML = `${pips}<span class="fec-level-num">${label}</span>`;
  // Flashing means "above the floor you set" — the condition worth an
  // operator's eye. Steady dots elsewhere: without a floor there is nothing to
  // have exceeded.
  el.classList.toggle("is-flashing", floored && lit > 0);
  el.classList.toggle("is-below-floor", belowFloor);
  // Clear both scale-row states: a payload can fall back to this shape mid-run
  // (relay downgrade, or a restart landing between publishers), and a stale
  // is-pinned would dim a row that is not pinned at all.
  el.classList.remove("is-scale", "is-pinned");
}

function renderFecCard(id, d, local){
  d = d || {};
  const card    = $(`#fec-${id}`);
  const ratioEl = $(`#fec-${id}-ratio`);
  const levelEl = $(`#fec-${id}-level`);
  const metaEl  = $(`#fec-${id}-meta`);
  const durEl   = $(`#fec-${id}-dur`);
  const badgesEl= $(`#fec-${id}-badges`);
  if (!card) return;

  const ratio = d.ratio || "—";
  const off   = (d.enabled === false) || (ratio === "8:0");
  ratioEl.textContent = off && ratio !== "—" ? `${ratio} OFF` : ratio;
  card.classList.toggle("is-off", off);

  renderFecLevel(levelEl, d);

  const dl = (d.driving_loss_pct != null) ? `${d.driving_loss_pct.toFixed(1)}% loss` : "— loss";
  metaEl.textContent = d.driver_wan ? `driving ${dl} · ${d.driver_wan}` : `driving ${dl}`;

  if (durEl){
    durEl.dataset.since = (d.since != null) ? d.since : "";
    durEl.textContent = (d.since != null) ? fmtUptime((Date.now()/1000) - d.since) : "—";
  }

  const b = [];
  if (local){
    b.push(d.actuator_ok === false
      ? `<span class="tag dropped">actuator down</span>`
      : `<span class="tag active">actuator ok</span>`);
  } else if (!d.ok){
    b.push(`<span class="tag dropped">unavailable${d.error ? " · " + escapeHtml(d.error) : ""}</span>`);
  } else {
    b.push(`<span class="tag active">${d.stale_s != null ? "fresh " + d.stale_s.toFixed(1) + "s" : "fresh"}</span>`);
    b.push(d.reconcile_pending
      ? `<span class="tag candidate">syncing…</span>`
      : `<span class="tag master">synced</span>`);
  }
  badgesEl.innerHTML = b.join("");
  const wireEl = $(`#fec-${id}-wire`);
  if (wireEl){
    const w = d.wire;
    if (w && !w.stale && w.tx_mbps != null){
      wireEl.textContent = `↑ ${w.tx_mbps.toFixed(2)} Mb/s · +${(w.overhead_pct ?? 0).toFixed(0)}% parity`;
    } else if (w && w.stale){
      wireEl.textContent = "↑ — · stale";
    } else {
      wireEl.textContent = "";
    }
  }
}

/* ---------- FEC decode-outcome graph ---------- */
const FEC_GRAPH_WINDOW_S = 300;                 // 5-minute scroll window
const FEC_GRAPH_H = 170;                        // tall enough for axes + tick labels
const FEC_HIST_CAP = 3600;                      // matches server retention
const fecHist = { c2r: [], r2c: [] };
let fecHistSeeded = false;
const fecHover = { c2r: null, r2c: null };      // pointer x per canvas, CSS px

function pushFecSample(dir, t, rx){
  const arr = fecHist[dir];
  if (arr.length && arr[arr.length - 1].t === t) return;   // poll faster than tick
  arr.push({
    t,
    delivered: rx ? rx.delivered_per_s : null,
    recovered: rx ? rx.recovered_per_s : null,
    lost:      rx ? rx.lost_pkts_est_per_s : null,
    waste:     rx ? rx.par_waste_per_s : null,
  });
  if (arr.length > FEC_HIST_CAP) arr.shift();
}

async function seedFecHistory(){
  try {
    const r = await fetch("/api/fec_history", {cache: "no-store"});
    const j = await r.json();
    (j.samples || []).forEach(s => {
      pushFecSample("c2r", s.t, s.c2r && {
        delivered_per_s: s.c2r.delivered_per_s,
        recovered_per_s: s.c2r.recovered_per_s,
        lost_pkts_est_per_s: s.c2r.lost_pkts_est_per_s,
        par_waste_per_s: s.c2r.par_waste_per_s,
      });
      pushFecSample("r2c", s.t, s.r2c && {
        delivered_per_s: s.r2c.delivered_per_s,
        recovered_per_s: s.r2c.recovered_per_s,
        lost_pkts_est_per_s: s.r2c.lost_pkts_est_per_s,
        par_waste_per_s: s.r2c.par_waste_per_s,
      });
    });
  } catch (e) { /* endpoint absent (older controller): graph starts empty */ }
  fecHistSeeded = true;
}

function cssVar(name){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Round a peak up to a "nice" axis maximum (1/2/5 x 10^n) so gridline labels
// land on readable numbers instead of whatever the busiest sample happened to be.
function niceCeil(v){
  if (!(v > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
}

function drawFecGraph(id, series){
  const cv = document.getElementById(id);
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  // Both dimensions come from the laid-out box, so CSS stays the single source
  // of truth. Sizing the backing store from a JS constant while the box is
  // sized by CSS stretches the whole chart the moment the two drift.
  const cssW = cv.clientWidth || 300, cssH = cv.clientHeight || FEC_GRAPH_H;
  if (cv.width !== Math.round(cssW * dpr)) cv.width = Math.round(cssW * dpr);
  if (cv.height !== Math.round(cssH * dpr)) cv.height = Math.round(cssH * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  // Read each CSS var once; getComputedStyle is not free and this function
  // runs per-frame per-graph.
  const cDelivered = cssVar("--fec-delivered"), cRecovered = cssVar("--fec-recovered"),
        cLost = cssVar("--fec-lost"),
        cInset = cssVar("--bg-inset"), cWaste = cssVar("--fec-waste"),
        cBorderStrong = cssVar("--border-strong"), cFg = cssVar("--fg"),
        cFgDim = cssVar("--fg-dim"), cGrid = cssVar("--border-subtle"),
        cAxis = cssVar("--border"), cSurface = cssVar("--bg-elevated");

  // Plot frame: room on the left for y labels and below for time ticks. The
  // axes are what this borrows from the PepVPN chart -- the old sparkline had
  // no scale at all, so a spike's magnitude was unreadable.
  const ML = 44, MR = 10, MT = 10, MB = 20;
  const plotL = ML, plotR = cssW - MR, plotT = MT, plotB = cssH - MB;
  const plotW = Math.max(1, plotR - plotL), plotH = Math.max(1, plotB - plotT);

  // Anchor the window to wall-clock now, NOT to the newest sample. Anchored to
  // the sample, a stalled feed (controller restarting, relay poll failing, tab
  // throttled in the background) keeps its last reading pinned under the "now"
  // tick, so minutes-old decoder history reads as current. Anchored to the
  // clock, stale data visibly slides left and leaves a growing empty gutter --
  // which is the honest picture. Falls back to the newest sample if the browser
  // clock sits behind the server's, so skew cannot blank the chart.
  const last = series.length ? series[series.length - 1] : null;
  const t1 = Math.max(Date.now() / 1000, last ? last.t : 0);
  const t0 = t1 - FEC_GRAPH_WINDOW_S;
  const windowed = series.filter(p => p.t >= t0);
  const pts = windowed.filter(p => p.delivered != null);

  const X = t => plotL + ((t - t0) / FEC_GRAPH_WINDOW_S) * plotW;

  // Axes and gridlines draw even with no data, so an empty graph still reads as
  // a chart with a scale rather than a blank box.
  let peak = 1;
  pts.forEach(p => {
    const tot = (p.delivered || 0) + (p.recovered || 0) + (p.lost || 0);
    if (tot > peak) peak = tot;
    if ((p.waste || 0) > peak) peak = p.waste;
  });
  const axisMax = niceCeil(peak);
  const Y = v => plotB - (v / axisMax) * plotH;

  ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
  ctx.textBaseline = "middle";

  // horizontal gridlines + y labels (pkt/s)
  const Y_TICKS = 4;
  ctx.textAlign = "right";
  for (let i = 0; i <= Y_TICKS; i++){
    const val = (axisMax / Y_TICKS) * i;
    const y = Math.round(Y(val)) + 0.5;
    ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y);
    ctx.lineWidth = 1; ctx.strokeStyle = i === 0 ? cAxis : cGrid; ctx.stroke();
    ctx.fillStyle = cFgDim;
    ctx.fillText(fmtRate(val), plotL - 6, y);
  }
  // vertical gridlines + time labels, one per minute of the window
  ctx.textAlign = "center";
  const STEP_S = 60;
  for (let s = 0; s <= FEC_GRAPH_WINDOW_S; s += STEP_S){
    const x = Math.round(plotL + (s / FEC_GRAPH_WINDOW_S) * plotW) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, plotT); ctx.lineTo(x, plotB);
    ctx.lineWidth = 1; ctx.strokeStyle = cGrid; ctx.stroke();
    const mins = (FEC_GRAPH_WINDOW_S - s) / 60;
    ctx.fillStyle = cFgDim;
    ctx.fillText(mins === 0 ? "now" : `-${mins}m`, x, plotB + 10);
  }
  // y-axis line
  ctx.beginPath();
  ctx.moveTo(Math.round(plotL) + 0.5, plotT); ctx.lineTo(Math.round(plotL) + 0.5, plotB);
  ctx.lineWidth = 1; ctx.strokeStyle = cAxis; ctx.stroke();
  ctx.textBaseline = "alphabetic";

  if (!pts.length){
    ctx.fillStyle = cFgDim;
    ctx.textAlign = "center";
    ctx.fillText("no decoder data", plotL + plotW / 2, plotT + plotH / 2);
    ctx.textAlign = "left";
    return;
  }

  // Outages must show as a visual break, not a bridged straight edge: split
  // the window into contiguous runs, breaking whenever a sample's delivered
  // value is null OR consecutive samples are more than 10s apart (a dead
  // poll shouldn't bridge either). Each run gets its own closed fill/stroke.
  const FEC_GRAPH_MAX_GAP_S = 10;
  const runs = [];
  let cur = [];
  windowed.forEach(p => {
    if (p.delivered == null){
      if (cur.length) runs.push(cur);
      cur = [];
      return;
    }
    if (cur.length && (p.t - cur[cur.length - 1].t) > FEC_GRAPH_MAX_GAP_S){
      runs.push(cur);
      cur = [];
    }
    cur.push(p);
  });
  if (cur.length) runs.push(cur);

  // Stacked bands, FIXED order bottom->top: delivered, recovered, lost.
  // Position encodes identity (CVD-safe with the 2px separators below).
  const bands = [
    { lo: () => 0,                              hi: p => p.delivered || 0,                                    color: cDelivered },
    { lo: p => p.delivered || 0,                hi: p => (p.delivered || 0) + (p.recovered || 0),             color: cRecovered, mustShow: true },
    { lo: p => (p.delivered || 0) + (p.recovered || 0),
      hi: p => (p.delivered || 0) + (p.recovered || 0) + (p.lost || 0),                                       color: cLost, mustShow: true },
  ];
  // Recovery and loss are the events worth seeing, and they are exactly the
  // ones the scale hides: delivered sets the axis, so a handful of recovered
  // packets against thousands delivered is a sub-pixel sliver — and then the
  // 2px separator drawn on its top edge paints over what little there was, so
  // the event is in the data and absent from the chart. Where a band is too
  // thin to survive that, stroke its OWN colour along its true top edge at
  // full opacity. Ink guarantees the event is seen; putting it exactly on the
  // boundary marks presence without overstating magnitude, which inflating the
  // band to a minimum height would do on an axis that is labelled in pkt/s.
  // Bands with room keep the surface separator, so the gap between fills
  // survives wherever it fits.
  const MIN_VIS_PX = 3;
  const bandValue = (b, p) => b.hi(p) - b.lo(p);
  // Thin marks are a rendering floor, so two of them can collide: with
  // recovered and lost both tiny their edges are a fraction of a pixel apart,
  // and whichever draws second hides the other outright. Give each thin band
  // its own MIN_VIS_PX of screen, stacked in the same order as the bands, so
  // simultaneous events stay separately readable. Ordering stays truthful;
  // only the exact y is floored, and only for bands already too small to
  // render at all.
  const isThin = (b, p) => bandValue(b, p) > 0 &&
    Math.abs(Y(b.lo(p)) - Y(b.hi(p))) < MIN_VIS_PX;
  const markOffset = (b, p) => {
    if (!b.mustShow || !isThin(b, p)) return 0;
    let off = 0;
    for (const other of bands){
      if (other === b) break;                       // bands below this one only
      if (other.mustShow && isThin(other, p)) off += MIN_VIS_PX;
    }
    return off;
  };

  // Walk the top edge segment by segment and give each piece the style it has
  // earned: the surface separator where the band has room for one, its own
  // colour where it is too thin to survive one.
  //
  // The split happens at the CROSSING, not at a sample boundary. Snapping to
  // samples was wrong in both directions — a whole-sample span leaves the
  // straddling segment unstroked, and extending to cover it then paints colour
  // across a thick stretch that still deserved its separator, and through
  // zero-valued neighbours as if an event had happened there. Y is linear in
  // value, so the crossing is exact arithmetic rather than a guess.
  function strokeBandEdge(b, run){
    const vThresh = MIN_VIS_PX * axisMax / plotH;   // value that renders MIN_VIS_PX tall
    const pieces = [];
    let lastX = NaN, lastY = NaN;
    for (let i = 0; i + 1 < run.length; i++){
      const A = run[i], B = run[i + 1];
      const vA = bandValue(b, A), vB = bandValue(b, B);
      if (vA <= 0 && vB <= 0) continue;             // no band over this interval
      const cuts = [0, 1];
      if ((vA < vThresh) !== (vB < vThresh) && vA !== vB)
        cuts.splice(1, 0, (vThresh - vA) / (vB - vA));
      const xA = X(A.t), xB = X(B.t);
      // Lift a thin mark clear of any thin mark below it (see markOffset).
      const yA = Y(b.hi(A)) - markOffset(b, A), yB = Y(b.hi(B)) - markOffset(b, B);
      for (let k = 0; k + 1 < cuts.length; k++){
        // f0/f1, not t0/t1: those are the plot window's start and end time in
        // this same closure, which X() maps through.
        const f0 = cuts[k], f1 = cuts[k + 1];
        const vMid = vA + (vB - vA) * (f0 + f1) / 2;
        if (vMid <= 0) continue;                    // zero band draws nothing
        const color = vMid < vThresh ? (b.mustShow ? b.color : null) : cInset;
        if (!color) continue;
        pieces.push({ color,
                      x0: xA + (xB - xA) * f0, y0: yA + (yB - yA) * f0,
                      x1: xA + (xB - xA) * f1, y1: yA + (yB - yA) * f1 });
      }
    }
    // Stroke consecutive same-coloured pieces as ONE path. Drawn individually
    // they are butt-capped segments that meet at an angle, which leaves a
    // notch at every join and gives a sloped edge a serrated look.
    let path = null, pathColor = null;
    const flush = () => {
      if (!path) return;
      ctx.lineWidth = 2; ctx.strokeStyle = pathColor; ctx.globalAlpha = 1;
      ctx.stroke(path);
      path = null;
    };
    pieces.forEach(pc => {
      const joins = path && pathColor === pc.color
        && Math.abs(pc.x0 - lastX) < 0.01 && Math.abs(pc.y0 - lastY) < 0.01;
      if (!joins){ flush(); path = new Path2D(); path.moveTo(pc.x0, pc.y0); pathColor = pc.color; }
      path.lineTo(pc.x1, pc.y1);
      lastX = pc.x1; lastY = pc.y1;
    });
    flush();
  }
  // Pass 1: every fill. A later band's translucent fill lands exactly on the
  // edge of the one beneath it, so drawing fills and marks interleaved let a
  // fill wash out a mark that was already correct.
  bands.forEach(b => {
    runs.forEach(run => {
      if (run.length === 1){
        const p = run[0], x = X(p.t);
        const yHi = Y(b.hi(p)), yLo = Y(typeof b.lo === "function" ? b.lo(p) : 0);
        if (b.mustShow && isThin(b, p)) return;     // pass 2 marks this one
        ctx.globalAlpha = 0.55; ctx.fillStyle = b.color;
        ctx.fillRect(x - 1, Math.min(yHi, yLo), 2, Math.abs(yLo - yHi));
        ctx.globalAlpha = 1;
        return;
      }
      ctx.beginPath();
      run.forEach((p, i) => { const x = X(p.t), y = Y(b.hi(p)); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(X(run[i].t), Y(typeof b.lo === "function" ? b.lo(run[i]) : 0));
      ctx.closePath();
      ctx.globalAlpha = 0.55; ctx.fillStyle = b.color; ctx.fill();
      ctx.globalAlpha = 1;
    });
  });

  // Pass 2: separators and visibility marks, on top of every fill.
  bands.forEach(b => {
    runs.forEach(run => {
      if (run.length === 1){
        // A one-sample run has no edge to stroke; mark it directly, centred on
        // the true edge so it does not drift off its value.
        const p = run[0];
        if (!b.mustShow || !isThin(b, p)) return;
        const x = X(p.t), y = Y(b.hi(p)) - markOffset(b, p);
        ctx.fillStyle = b.color;
        ctx.fillRect(x - 1, y - 1, 2, 2);
        return;
      }
      strokeBandEdge(b, run);
    });
  });
  // wasted parity: thin muted overlay line. Honor the same gap/validity rules
  // as the bands above — iterate the full window (not the delivered-filtered
  // `pts`) and break the line on a null waste sample, a null delivered sample,
  // or a >FEC_GRAPH_MAX_GAP_S jump, so it never bridges a gap the bands break on.
  ctx.beginPath();
  let started = false;
  let lastT = null;
  windowed.forEach(p => {
    if (p.delivered == null || p.waste == null ||
        (lastT != null && (p.t - lastT) > FEC_GRAPH_MAX_GAP_S)){
      started = false;
      lastT = null;
      return;
    }
    const x = X(p.t), y = Y(p.waste);
    started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    started = true;
    lastT = p.t;
  });
  ctx.lineWidth = 1.5; ctx.strokeStyle = cWaste; ctx.stroke();

  // Crosshair + shared tooltip, the other thing borrowed from the PepVPN chart:
  // one vertical rule at the hovered sample and every series' value at that
  // instant, rather than a single line of text jammed in the corner.
  const dir = id.includes("c2r") ? "c2r" : "r2c";
  const hx = fecHover[dir];
  if (hx == null) return;
  // Ignore hovers in the axis gutters -- there is no sample under them.
  if (hx < plotL || hx > plotR) return;

  const ht = t0 + ((hx - plotL) / plotW) * FEC_GRAPH_WINDOW_S;
  // Decide from the rendered INTERVAL, not from nearest-sample identity. The
  // bands draw a break across a span; asking "is the closest sample a null one"
  // answers a different question and gets the edges wrong -- hovering just
  // inside a break, but marginally nearer the last valid sample, would quote
  // that sample where the chart plainly shows nothing. At the ops width samples
  // sit under a pixel apart so that band is invisible, but the wall layout is
  // several times wider and it becomes big enough to land on.
  //
  // Bracket the hovered instant and ask whether the bands broke across it:
  // either endpoint missing (before the first sample or past the last), either
  // endpoint an explicitly recorded outage, or a span wider than the run
  // threshold. That is exactly the rule the run-splitting above uses.
  let prev = null, next = null;
  windowed.forEach(p => {
    if (p.t <= ht && (!prev || p.t > prev.t)) prev = p;
    if (p.t >= ht && (!next || p.t < next.t)) next = p;
  });
  const inGap = !prev || !next
             || prev.delivered == null || next.delivered == null
             || (next.t - prev.t) > FEC_GRAPH_MAX_GAP_S;
  // Off a break, both brackets are valid samples -- read out the nearer.
  const best = inGap ? null
             : (Math.abs(prev.t - ht) <= Math.abs(next.t - ht) ? prev : next);
  const bx = inGap ? hx : X(best.t);

  ctx.strokeStyle = cBorderStrong; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(Math.round(bx) + 0.5, plotT); ctx.lineTo(Math.round(bx) + 0.5, plotB);
  ctx.stroke();

  if (inGap){
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    const msg = "no data";
    const w = ctx.measureText(msg).width + 16, h = 20;
    const gx = Math.max(2, Math.min(bx + 8, cssW - w - 2));
    ctx.globalAlpha = 0.96; ctx.fillStyle = cSurface;
    ctx.fillRect(gx, plotT + 6, w, h);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = cAxis; ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(gx) + 0.5, Math.round(plotT + 6) + 0.5, w, h);
    ctx.fillStyle = cFgDim; ctx.textBaseline = "middle"; ctx.textAlign = "left";
    ctx.fillText(msg, gx + 8, plotT + 6 + h / 2);
    ctx.textBaseline = "alphabetic";
    return;
  }

  const rows = [
    { label: "delivered", val: best.delivered, color: cDelivered },
    { label: "recovered", val: best.recovered, color: cRecovered },
    { label: "lost",      val: best.lost,      color: cLost },
    // Same wording as the legend below the canvas -- two names for one series
    // is a reader's problem, not a layout saving.
    { label: "parity wasted", val: best.waste,  color: cWaste },
  ];
  ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
  const rowH = 13, padX = 8, padY = 6, sw = 7, gap = 6;
  let labelW = 0, valW = 0;
  rows.forEach(r => {
    labelW = Math.max(labelW, ctx.measureText(r.label).width);
    valW = Math.max(valW, ctx.measureText(fmtRate(r.val)).width);
  });
  const boxW = padX * 2 + sw + gap + labelW + 10 + valW;
  const boxH = padY * 2 + rows.length * rowH;
  // Prefer the right of the crosshair, flip left if that would leave the plot.
  let boxX = bx + 10;
  if (boxX + boxW > plotR) boxX = bx - 10 - boxW;
  // Clamp to the CANVAS, not the plot. On a narrow card the tooltip can be
  // wider than the plot itself, and clamping to `plotR - boxW` then resolves
  // below plotL, so the lower bound wins and the box runs off the canvas
  // entirely. Better to overhang the axis gutter than to be unreadable.
  boxX = Math.max(2, Math.min(boxX, cssW - boxW - 2));
  // Sit near the top of the plot, but never hang past its bottom edge.
  let boxY = plotT + 6;
  if (boxY + boxH > plotB - 2) boxY = Math.max(plotT + 2, plotB - boxH - 2);

  ctx.globalAlpha = 0.96;
  ctx.fillStyle = cSurface;
  ctx.fillRect(boxX, boxY, boxW, boxH);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = cAxis; ctx.lineWidth = 1;
  ctx.strokeRect(Math.round(boxX) + 0.5, Math.round(boxY) + 0.5, boxW, boxH);

  ctx.textBaseline = "middle";
  rows.forEach((r, i) => {
    const y = boxY + padY + rowH * i + rowH / 2;
    ctx.fillStyle = r.color;
    ctx.fillRect(boxX + padX, y - sw / 2, sw, sw);
    ctx.fillStyle = cFgDim; ctx.textAlign = "left";
    ctx.fillText(r.label, boxX + padX + sw + gap, y);
    ctx.fillStyle = cFg; ctx.textAlign = "right";
    ctx.fillText(fmtRate(r.val), boxX + boxW - padX, y);
  });
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
}

function fmtRate(v){ return v == null ? "—" : (v >= 100 ? Math.round(v) : v.toFixed(1)); }

/* ---------- environmental ---------- */
function renderEnvironmental(s){
  const env = s.environmental || {};
  const envEl = document.getElementById('environment-effective');
  if (envEl) {
    if (!env.configured) {
      envEl.textContent = 'not configured';
    } else if (!env.enabled) {
      envEl.textContent = 'off';
    } else if (env.active) {
      envEl.textContent = 'forcing full · ' + (env.reason || 'hazard');
    } else {
      // enabled + not active ⇒ no honored override ⇒ clear. (A fresh force_full
      // override while enabled would set env.active, handled above.)
      envEl.textContent = 'clear';
    }
  }
  $$('input[name="environmental_enabled"]').forEach(r => r.disabled = !env.configured);
}

/* ---------- maintenance reboot ---------- */
function renderMaintenance(s){
  const m = s.maintenance || {};
  const el = document.getElementById('maintenance-effective');
  if (el){
    if (!m.configured)      el.textContent = 'not configured';
    else if (!m.enabled)    el.textContent = 'off';
    else                    el.textContent = 'daily at '
      + String(m.hour ?? 0).padStart(2, "0") + ':00 local';
  }
  $$('input[name="maintenance_enabled"]').forEach(r => r.disabled = !m.configured);
  const sel = $("#maintenance-hour");
  const on = document.querySelector('input[name="maintenance_enabled"]:checked')?.value === "on";
  if (sel) sel.disabled = !m.configured || !on;
}

/* ---------- apply ---------- */
async function apply(){
  const mode      = document.querySelector('input[name="mode"]:checked')?.value;
  const policy    = document.querySelector('input[name="policy"]:checked')?.value;
  const masterWan = $("#master-wan").value;
  const egressMode = document.querySelector('input[name="egress_mode"]:checked')?.value;
  const persist   = $("#persist").checked;
  const fecMode = document.querySelector('input[name="fec_mode"]:checked')?.value;
  /* Send the raw entry; the server resolves x:y or percent and echoes back the
     canonical ratio. The browser deliberately does no arithmetic here. */
  const ratioOf = (selId, customId) => {
    const sel = $(selId);
    if (!sel) return null;
    return sel.value === FEC_CUSTOM ? ($(customId)?.value || "").trim() : sel.value;
  };
  const fecFixed = ratioOf("#fec-fixed-ratio", "#fec-fixed-custom");
  const fecFloor = ratioOf("#fec-floor-ratio", "#fec-floor-custom");
  const envSel = document.querySelector('input[name="environmental_enabled"]:checked')?.value;
  const maintSel = document.querySelector('input[name="maintenance_enabled"]:checked')?.value;
  const maintHour = $("#maintenance-hour")?.value;
  const status    = $("#apply-status");
  if (!mode || !policy || !egressMode){
    status.textContent = "select mode, policy, and egress first";
    status.className = "apply-status err"; return;
  }
  $("#apply").disabled = true;
  status.textContent = "applying…"; status.className = "apply-status";
  try {
    const fecPayload = {};
    if (fecMode) fecPayload.fec_mode = fecMode;
    if (fecMode === "fixed" && fecFixed) fecPayload.fec_fixed_ratio = fecFixed;
    if (fecMode === "min_adaptive" && fecFloor)
      fecPayload.fec_floor_ratio = fecFloor === "auto" ? null : fecFloor;
    const r = await fetch("/api/runtime", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(Object.assign(
        {mode, master_policy: policy, master_wan: masterWan, egress_mode: egressMode, persist},
        fecPayload,
        (envSel ? {environmental_enabled: envSel === "on"} : {}),
        (maintSel ? {maintenance_enabled: maintSel === "on"} : {}),
        (maintSel === "on" && maintHour !== undefined
          ? {maintenance_hour: Number(maintHour)} : {})))
    });
    if (r.status === 409){
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error === "master_wan_down"
        ? `cannot apply Local Direct: master WAN ${j.wan || "?"} is DOWN`
        : `conflict (409): ${j.error || "unknown"}`);
    }
    if (!r.ok){
      const j = await r.json().catch(() => ({error: r.statusText}));
      throw new Error(j.error || r.statusText);
    }
    lastApplyAt = Date.now();
    dirtyFields.clear();
    status.textContent = "applied";
    status.className = "apply-status ok";
  } catch (e){
    status.textContent = `error · ${e}`;
    status.className = "apply-status err";
  } finally {
    $("#apply").disabled = false;
    fetchState();
  }
}

/* ---------- helpers ---------- */
function escapeHtml(s){
  return String(s ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

/* ---------- wire ---------- */
$("#apply").addEventListener("click", apply);
$$(FORM_SELECTOR).forEach(el => {
  el.addEventListener("change", () => {
    dirtyFields.add(fieldKey(el));
    if (el.name === "fec_mode"){
      const fixedSel = $("#fec-fixed-ratio");
      const floorSel = $("#fec-floor-ratio");
      if (fixedSel) fixedSel.disabled = el.value !== "fixed";
      if (floorSel) floorSel.disabled = el.value !== "min_adaptive";
      const fixedCustom = $("#fec-fixed-custom");
      const floorCustom = $("#fec-floor-custom");
      if (fixedCustom) fixedCustom.disabled = fixedSel ? fixedSel.disabled : true;
      if (floorCustom) floorCustom.disabled = floorSel ? floorSel.disabled : true;
    }
    if (el.id === "fec-fixed-ratio" || el.id === "fec-floor-ratio"){
      const custom = $(el.id === "fec-fixed-ratio" ? "#fec-fixed-custom"
                                                   : "#fec-floor-custom");
      if (custom){
        custom.hidden = el.value !== FEC_CUSTOM;
        if (!custom.hidden) custom.focus();
      }
    }
    if (el.name === "maintenance_enabled"){
      const hs = $("#maintenance-hour");
      if (hs) hs.disabled = el.value !== "on";
    }
  });
  el.addEventListener("input",  () => { dirtyFields.add(fieldKey(el)); });
});

["c2r", "r2c"].forEach(dir => {
  const cv = document.getElementById(`fec-${dir}-graph`);
  if (!cv) return;
  // Coalesce hover redraws to one per frame. A pointermove can fire far more
  // often than the display refreshes, and each redraw rebuilds the whole chart
  // (twelve getComputedStyle reads among them) -- on the wall-display hardware
  // that is worth not doing several times between frames.
  let pending = null;
  cv.addEventListener("pointermove", e => {
    fecHover[dir] = e.offsetX;
    if (pending != null) return;
    pending = requestAnimationFrame(() => {
      pending = null;
      drawFecGraph(`fec-${dir}-graph`, fecHist[dir]);
    });
  });
  cv.addEventListener("pointerleave", () => {
    fecHover[dir] = null;
    drawFecGraph(`fec-${dir}-graph`, fecHist[dir]);
  });
});

fetchState();
fetchEngarde();
seedFecHistory();
setInterval(fetchState,   1000);
setInterval(fetchEngarde, 2000);
