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
let userDirty = false;
let lastState = null;
let lastEngarde = null;

function isFormFocused(){
  const a = document.activeElement;
  if (!a) return false;
  return a.matches('input[name="mode"], input[name="policy"], input[name="egress_mode"], input[name="fec_enabled"], #master-wan, #persist');
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
  const wans     = Object.keys(s.pi_local || {});
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
    syncSub.textContent  = (s.relay_remote && s.relay_remote.error) ? "pi-local only" : "no remote";
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

  /* form sync — focus-protected so we don't clobber operator typing */
  if (Date.now() - lastApplyAt > 5000 && !userDirty && !isFormFocused()){
    const setRadio = (name, val) => $$(`input[name="${name}"]`).forEach(r => r.checked = (r.value === val));
    setRadio("mode", s.mode);
    setRadio("policy", s.master_policy);
    setRadio("egress_mode", s.egress_mode || "relay_vpn");
    if (s.master_wan && sel) sel.value = s.master_wan;
    if (s.fec && s.fec.configured) setRadio("fec_enabled", s.fec.desired_enabled ? "on" : "off");
  }

  renderWanList(s, wans, active, masterWan, dyn);
  renderSignalDiagram(s, wans, active, masterWan);
  renderBoard(s);
  renderDynamic(s);
  renderFailback(s);
  renderFec(s);

  /* Local-Direct + master-DOWN red badge */
  const warn = $("#egress-warn");
  if (warn) {
    const masterState = (s.pi_local && s.pi_local[s.master_wan] || {}).state;
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
    const local  = s.pi_local[w] || {};
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
    tags.push(isActive
      ? `<span class="tag active">active</span>`
      : `<span class="tag dropped">nft-dropped</span>`);
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
        <div class="wan-iface">${escapeHtml(w)} · pi ${escapeHtml(local.state||"?")} · relay ${escapeHtml(remote.state||"?")}</div>
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
  const truckX = 96, sbfdX = 300, engX = 504;
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

    const midX1 = (truckX + sbfdX)/2;
    const path1 = `M ${truckX} ${yMid} C ${midX1} ${yMid} ${midX1} ${y} ${sbfdX-42} ${y}`;
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
             : egressMode === "local_direct"? { tag: "BYPASSED",   sub: "client → cab WAN (engarde bypassed)" }
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
  let rows = [];
  if (Array.isArray(d.interfaces) && d.interfaces.length) {
    rows = d.interfaces
      // Hide engarde's intermediate functional-block (ifb-*) interfaces — they're
      // back-pressure shaper devices, not real uplinks. Real WAN uplinks have a
      // populated senderAddress; ifb-* report empty.
      .filter(i => i.status !== "excluded" && i.senderAddress)
      .map(i => ({
        label: i.name + (i.dstAddress ? ` → ${i.dstAddress}` : ""),
        last: typeof i.last === "number" ? i.last : null,
        status: i.status,  // "active" | "idle"
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
      ? "All uplinks excluded"
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
function renderFec(s){
  const fec  = s.fec || {};
  const dirs = fec.directions || {};
  const sub  = $("#fec-sub");
  if (sub) sub.textContent = !fec.configured ? "not configured"
                        : (fec.desired_enabled ? "adaptive" : "disabled — forced 8:0");

  renderFecCard("c2r", dirs.client_to_relay, true);
  renderFecCard("r2c", dirs.relay_to_client, false);

  const eff = $("#fec-effective");
  if (eff){
    if (!fec.configured){
      eff.textContent = "FEC not configured";
    } else {
      const o = dirs.relay_to_client || {};
      const ovhTxt = !o.ok ? "relay sync pending"
                   : (o.reconcile_pending ? "syncing relay…" : "relay synced");
      eff.textContent = `effective: ${fec.desired_enabled ? "adaptive" : "disabled"} · ${ovhTxt}`;
    }
  }
  $$('input[name="fec_enabled"]').forEach(r => r.disabled = !fec.configured);
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

  const lvl = (typeof d.level === "number") ? d.level : 0;
  let pips = "";
  for (let i = 0; i < 5; i++) pips += `<i class="pip ${i <= lvl ? "on" : ""}"></i>`;
  levelEl.innerHTML = `${pips}<span class="fec-level-num">level ${lvl}</span>`;

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

/* ---------- apply ---------- */
async function apply(){
  const mode      = document.querySelector('input[name="mode"]:checked')?.value;
  const policy    = document.querySelector('input[name="policy"]:checked')?.value;
  const masterWan = $("#master-wan").value;
  const egressMode = document.querySelector('input[name="egress_mode"]:checked')?.value;
  const persist   = $("#persist").checked;
  const fecOn = document.querySelector('input[name="fec_enabled"]:checked')?.value;
  const status    = $("#apply-status");
  if (!mode || !policy || !egressMode){
    status.textContent = "select mode, policy, and egress first";
    status.className = "apply-status err"; return;
  }
  $("#apply").disabled = true;
  status.textContent = "applying…"; status.className = "apply-status";
  try {
    const r = await fetch("/api/runtime", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(Object.assign(
        {mode, master_policy: policy, master_wan: masterWan, egress_mode: egressMode, persist},
        (fecOn ? {fec_enabled: fecOn === "on"} : {})))
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
    userDirty = false;
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
$$('input[name="mode"], input[name="policy"], input[name="egress_mode"], input[name="fec_enabled"], #master-wan, #persist').forEach(el => {
  el.addEventListener("change", () => { userDirty = true; });
  el.addEventListener("input",  () => { userDirty = true; });
});

fetchState();
fetchEngarde();
setInterval(fetchState,   1000);
setInterval(fetchEngarde, 2000);
