// The Exit KPI tile, ui/app.js renderKpiExit, under node with no DOM. The function
// and the label helper it uses are taken from app.js as written and handed stub
// elements that start stale and record every property written to them, so each
// check sees what a browser would show: the value, the sub-line, the sub-line's
// hover title and the tile's data-state.
//
// Usage: node tests/js/test_kpi_exit.js <path/to/ui/app.js>   (tests/test_ui_js.py runs it)
// Prints each failed check, then a count. Exits 0 when every check passes, 1 when
// one fails, and 2 when app.js lacks a piece this test takes from it.
"use strict";
const fs = require("fs");

const src = fs.readFileSync(process.argv[2], "utf8");
function take(re, what) {
  const m = src.match(re);
  if (!m) {
    console.log(`FAIL app.js has no ${what}`);
    process.exit(2);
  }
  return m[0];
}
const code = [
  take(/^const EGRESS_LABELS = \{[\s\S]*?\};$/m, "EGRESS_LABELS"),
  take(/^const egressLabel = [\s\S]*?;$/m, "egressLabel"),
  take(/^function renderKpiExit\(s\)\{[\s\S]*?^\}$/m, "renderKpiExit"),
].join("\n");
// app.js is strict mode, so the extract runs in it too.
const load = ($) => new Function("$", `"use strict";\n${code}\nreturn renderKpiExit;`)($);

let checks = 0, failed = 0;
function check(name, got, want, detail = "") {
  checks++;
  if (got === want) return;
  failed++;
  console.log(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}${detail}`);
}

// An element as renderKpiExit sees it: stale values it must overwrite, and the
// names of the properties written to it.
function element(props) {
  const written = new Set();
  const el = new Proxy(props, {
    set(target, key, value) {
      written.add(String(key));
      target[key] = value;
      return true;
    },
  });
  return {el, written};
}

// Renders one snapshot (undefined: none at all) on a fresh, stale tile.
function render(o) {
  const tile = element({dataset: {state: "stale"}});
  const val = element({textContent: "stale"});
  const sub = element({textContent: "stale", title: "stale"});
  const els = {"#kpi-exit": tile.el, "#kpi-exit-val": val.el, "#kpi-exit-sub": sub.el};
  load((q) => els[q] || null)(o === undefined ? {} : {egress_observed: o});
  return {value: val.el.textContent, sub: sub.el.textContent, title: sub.el.title,
          state: tile.el.dataset.state,
          writes: `tile[${[...tile.written]}] val[${[...val.written]}] sub[${[...sub.written].sort()}]`};
}

// One render, checked whole. The title is the sub-line itself, so a line the tile
// ellipsises stays readable on hover; and the only writes are textContent and
// title, so a server string lands as text, never as markup.
function tile(name, o, value, sub, state) {
  let r;
  try {
    r = render(o);
  } catch (e) {
    checks++;
    failed++;
    console.log(`FAIL ${name}: threw ${e}`);
    return null;
  }
  check(`${name}: value`, r.value, value);
  check(`${name}: sub-line`, r.sub, sub);
  check(`${name}: title`, r.title, sub);
  check(`${name}: data-state`, r.state, state);
  check(`${name}: writes`, r.writes, "tile[] val[textContent] sub[textContent,title]");
  return r;
}

const B = "relay_backbone", D = "relay_direct", IP = "198.51.100.20";
const SINCE = 1_770_000_123;                   // when the status began
const CHECKED = SINCE + 5 * 3600 + 17 * 60;    // the latest check, at another HH:MM
const AT = new Date(SINCE * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
const ABSENT = Symbol("absent");

// A snapshot as the observer publishes it; a field given as ABSENT is left out.
function snap(status, fields = {}) {
  const o = {selected: B, observed: D, ip: IP, status, since: SINCE, checked_at: CHECKED,
             error: null, error_checks: 3, interval_s: 120, ...fields};
  for (const k of Object.keys(o)) if (o[k] === ABSENT) delete o[k];
  return o;
}

// The check off.
tile("no snapshot", undefined, "—", "check off", "");
tile("a null snapshot", null, "—", "check off", "");

// match: the observed exit and its address.
tile("match", snap("match", {observed: B}), "relay Backbone", `matches · ${IP}`, "ok");
tile("match, ip null", snap("match", {observed: B, ip: null}), "relay Backbone", "matches", "ok");

// pending and mismatch name the reason first and the address last, so where the
// tile is too narrow the ellipsis cuts the address, never the reason. Without an
// address there is no separator and no placeholder.
const IPS = [["an ip", IP], ["ip null", null], ['ip ""', ""], ["ip undefined", undefined],
             ["no ip field", ABSENT]];
const SINCES = [["since", SINCE], ["since null", null], ["no since field", ABSENT]];
for (const status of ["pending", "mismatch"]) {
  for (const [ipName, ip] of IPS) {
    for (const [sinceName, since] of SINCES) {
      const name = `${status}, ${ipName}, ${sinceName}`;
      const reason = status === "pending"
        ? "≠ selected relay Backbone (rechecking)"          // pending never shows since
        : "≠ selected relay Backbone" + (since === SINCE ? ` since ${AT}` : "");
      const want = reason + (ip === IP ? ` · ${IP}` : "");
      const r = tile(name, snap(status, {ip, since}), "relay Direct", want, "degraded");
      if (!r) continue;
      const line = ` in ${JSON.stringify(r.sub)}`;
      check(`${name}: starts with the reason`, r.sub.startsWith("≠ selected relay Backbone"), true, line);
      if (ip === IP) {
        check(`${name}: ends with the ip`, r.sub.endsWith(` · ${IP}`), true, line);
        check(`${name}: one separator`, r.sub.split(" · ").length, 2, line);
      } else {
        check(`${name}: no separator`, r.sub.includes("·"), false, line);
        check(`${name}: no placeholder`, /null|undefined/.test(r.sub), false, line);
      }
    }
  }
}

// The rest of the table. Each snapshot still carries an exit and an address (the
// observer's keep the last check's through an error), to show these lines leave
// them out.
tile("skipped", snap("skipped", {selected: "local_direct"}), "n/a", "local direct", "");
tile("error", snap("error", {error: "timeout"}), "unknown", "trace failed", "degraded");
tile("failing", snap("failing", {error: "timeout"}), "unknown", `check failing since ${AT}`, "degraded");
tile("failing, since null", snap("failing", {error: "timeout", since: null}),
     "unknown", "check failing", "degraded");
tile("checking", snap("checking"), "…", "checking", "");
tile("a status the tile does not know", snap("<b>new</b>"), "…", "checking", "");

// Labels come from egressLabel, an own-key lookup: a mode named like an
// Object.prototype member renders as itself, and no mode as "unknown".
tile("modes named like prototype members",
     snap("mismatch", {observed: "constructor", selected: "toString", ip: null, since: null}),
     "constructor", "≠ selected toString", "degraded");
tile("no observed exit", snap("mismatch", {observed: null, ip: null, since: null}),
     "unknown", "≠ selected relay Backbone", "degraded");
// An address with markup in it is text.
tile("markup in the ip", snap("mismatch", {ip: "<img src=x onerror=alert(1)>", since: null}),
     "relay Direct", "≠ selected relay Backbone · <img src=x onerror=alert(1)>", "degraded");

// Without the tile (an index.html older than this app.js) rendering does nothing:
// a throw would end render() and leave every panel after it stale.
checks++;
try {
  load(() => null)({egress_observed: snap("mismatch")});
} catch (e) {
  failed++;
  console.log(`FAIL no tile in the page: threw ${e}`);
}

// render() draws the tile on every state update.
check("render() calls renderKpiExit(s)",
      /^\s+renderKpiExit\(s\);$/m.test(take(/^function render\(s\)\{[\s\S]*?^\}$/m, "render")), true);

console.log(`${checks} checks, ${failed} failed`);
process.exit(failed ? 1 : 0);
