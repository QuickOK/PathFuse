#!/usr/bin/env python3
"""Replay a client RTT trace through _dynamic_pick variants and report flap stats.

Trace format: JSONL produced by /tmp/pi_trace.py (one row per 200ms poll).
We thin to the ~1Hz publish cadence of sbfd (the controller's actual tick
rate is 0.5s) by sampling every Nth row to match the controller's timing.

Usage:
  ./simulate_dynamic.py [trace.jsonl]
"""
import json
import sys
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_trace(path):
    """Yield (ts, eff_state, rtt_ms, loss_pct) tuples at ~0.5s resolution."""
    raw = []
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if 'event' in d:
                continue
            if 'wan1' not in d or 'wan2' not in d:
                continue
            raw.append(d)
    # thin to 0.5s; trace is 200ms so take every other tick
    out = []
    last_ts = -1
    for r in raw:
        ts = r['ts']
        if ts - last_ts < 0.45:
            continue
        last_ts = ts
        eff = {}
        rtt = {}
        loss = {}
        for w in ('wan1', 'wan2'):
            sd = r[w]
            # Effective state from local sbfd's perspective. UNKNOWN/INIT
            # treated as not-UP for the simulation.
            eff[w] = 'UP' if sd.get('state') == 'UP' else (
                'DOWN' if sd.get('state') == 'DOWN' else 'UNKNOWN')
            rtt[w] = sd.get('rtt') or 0.0
            loss[w] = sd.get('loss') or 0.0
        out.append((ts, eff, rtt, loss))
    return out


class Simulator:
    """Drive _dynamic_pick over a trace and collect flap statistics."""
    def __init__(self, pick_fn, configured_master='wan2',
                 rtt_margin_ms=25.0, loss_margin_pct=1.0, dwell_s=10.0):
        self.pick = pick_fn
        self.configured_master = configured_master
        self.rtt_margin_ms = rtt_margin_ms
        self.loss_margin_pct = loss_margin_pct
        self.dwell_s = dwell_s

    def run(self, ticks, initial_master='wan2'):
        master = initial_master
        cand = None
        cand_since = None
        master_swaps = 0
        cand_set = 0
        cand_clear = 0
        time_on_configured = 0.0
        time_on_alt = 0.0
        switches = []
        prev_ts = ticks[0][0]

        for i, (ts, eff, rtt, loss) in enumerate(ticks):
            elapsed = max(0.0, ts - prev_ts)
            if master == self.configured_master:
                time_on_configured += elapsed
            else:
                time_on_alt += elapsed
            prev_ts = ts

            new_master, new_cand, new_cand_since = self.pick(
                eff, rtt, loss, self.configured_master,
                master, cand, cand_since,
                self.rtt_margin_ms, self.loss_margin_pct, self.dwell_s, ts,
            )

            if new_master != master:
                master_swaps += 1
                switches.append((ts, master, new_master,
                                 dict(rtt), dict(loss)))
            if cand is None and new_cand is not None:
                cand_set += 1
            elif cand is not None and new_cand is None:
                cand_clear += 1

            master, cand, cand_since = new_master, new_cand, new_cand_since

        return {
            'master_swaps': master_swaps,
            'cand_set': cand_set,
            'cand_clear': cand_clear,
            'time_on_configured': time_on_configured,
            'time_on_alt': time_on_alt,
            'frac_on_configured': time_on_configured / max(0.001,
                                                           time_on_configured + time_on_alt),
            'final_master': master,
            'switches': switches,
        }


def variant_baseline():
    """Reproduce the buggy/original ranker (pre-fix)."""
    def pick(eff_state, rtt_ms, loss_pct, configured_master,
             current_master, candidate, candidate_since,
             rtt_margin_ms, loss_margin_pct, dwell_s, now):
        ups = [w for w in eff_state if eff_state.get(w) == "UP"]
        if not ups:
            return configured_master, None, None
        if len(ups) == 1:
            return ups[0], None, None
        if current_master is None or current_master not in ups:
            best = min(ups, key=lambda w: (
                loss_pct.get(w, 0.0), rtt_ms.get(w, float("inf")),
                0 if w == configured_master else 1,
            ))
            return best, None, None
        cur_rtt = rtt_ms.get(current_master, float("inf"))
        cur_loss = loss_pct.get(current_master, 0.0)
        challenger = None
        for w in ups:
            if w == current_master:
                continue
            w_rtt = rtt_ms.get(w, float("inf"))
            w_loss = loss_pct.get(w, 0.0)
            loss_better = w_loss + loss_margin_pct <= cur_loss
            rtt_better = (w_rtt + rtt_margin_ms <= cur_rtt
                          and w_loss <= cur_loss + loss_margin_pct)
            if loss_better or rtt_better:
                challenger = w
                break
        if challenger is None:
            return current_master, None, None  # buggy reset
        if candidate == challenger and candidate_since is not None:
            if now - candidate_since >= dwell_s:
                return challenger, None, None
            return current_master, candidate, candidate_since
        return current_master, challenger, now
    return pick


def variant_fix_c_jitter_only():
    """Fix C: preserve candidate_since when challenger=None and
    candidate==configured_master. Does NOT add asymmetric pull-back."""
    def pick(eff_state, rtt_ms, loss_pct, configured_master,
             current_master, candidate, candidate_since,
             rtt_margin_ms, loss_margin_pct, dwell_s, now):
        ups = [w for w in eff_state if eff_state.get(w) == "UP"]
        if not ups:
            return configured_master, None, None
        if len(ups) == 1:
            return ups[0], None, None
        if current_master is None or current_master not in ups:
            best = min(ups, key=lambda w: (
                loss_pct.get(w, 0.0), rtt_ms.get(w, float("inf")),
                0 if w == configured_master else 1,
            ))
            return best, None, None
        cur_rtt = rtt_ms.get(current_master, float("inf"))
        cur_loss = loss_pct.get(current_master, 0.0)
        challenger = None
        for w in ups:
            if w == current_master:
                continue
            w_rtt = rtt_ms.get(w, float("inf"))
            w_loss = loss_pct.get(w, 0.0)
            loss_better = w_loss + loss_margin_pct <= cur_loss
            rtt_better = (w_rtt + rtt_margin_ms <= cur_rtt
                          and w_loss <= cur_loss + loss_margin_pct)
            if loss_better or rtt_better:
                challenger = w
                break
        if challenger is None:
            if candidate is not None and candidate == configured_master:
                return current_master, candidate, candidate_since
            return current_master, None, None
        if candidate == challenger and candidate_since is not None:
            if now - candidate_since >= dwell_s:
                return challenger, None, None
            return current_master, candidate, candidate_since
        return current_master, challenger, now
    return pick


def variant_fix_c_plus():
    """Fix C+: asymmetric pull-back to configured_master + jitter tolerance.
    This is the version now committed in sbfd_ctl.py."""
    sbfd_ctl = load_module(REPO / 'sbfd_ctl.py', 'sbfd_ctl')
    return sbfd_ctl._dynamic_pick


def fmt(d):
    return (f"swaps={d['master_swaps']:3d}  cand_set={d['cand_set']:3d}  "
            f"cand_clear={d['cand_clear']:3d}  on_configured={d['frac_on_configured']*100:5.1f}%  "
            f"final={d['final_master']}")


def synthesize_satellite_spike_trace(base_ticks, spike_intervals_s=(300, 400, 500),
                                     spike_duration_s=15.0, spike_rtt_ms=220.0):
    """Inject brief Satellite RTT spikes into a baseline trace.

    spike_intervals_s: list of seconds-from-start at which to inject a spike
    spike_duration_s: how long each spike lasts
    spike_rtt_ms: wan2 rtt during spike
    """
    if not base_ticks:
        return []
    t0 = base_ticks[0][0]
    out = []
    for ts, eff, rtt, loss in base_ticks:
        rel = ts - t0
        rtt_new = dict(rtt)
        for s_at in spike_intervals_s:
            if s_at <= rel < s_at + spike_duration_s:
                rtt_new['wan2'] = spike_rtt_ms
                break
        out.append((ts, eff, rtt_new, loss))
    return out


def synthesize_loss_event(base_ticks, start_s=200.0, duration_s=30.0,
                          which='wan2', loss_pct=8.0):
    """Inject a loss event on one WAN."""
    if not base_ticks:
        return []
    t0 = base_ticks[0][0]
    out = []
    for ts, eff, rtt, loss in base_ticks:
        rel = ts - t0
        loss_new = dict(loss)
        if start_s <= rel < start_s + duration_s:
            loss_new[which] = loss_pct
        out.append((ts, eff, rtt, loss_new))
    return out


def main():
    trace_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/pi_trace.jsonl'
    ticks = load_trace(trace_path)
    if not ticks:
        print(f"No valid ticks in {trace_path}")
        sys.exit(1)
    print(f"loaded {len(ticks)} ticks ({ticks[-1][0] - ticks[0][0]:.1f}s span)")
    print()

    variants = [
        ('baseline (buggy)', variant_baseline(), 25.0, 10.0),
        ('Fix C (jitter only)', variant_fix_c_jitter_only(), 25.0, 10.0),
        ('Fix C+ (asym pull-back, default tunings)', variant_fix_c_plus(), 25.0, 10.0),
        ('Fix C+ + dwell=30s', variant_fix_c_plus(), 25.0, 30.0),
        ('Fix C+ + margin=40 dwell=30', variant_fix_c_plus(), 40.0, 30.0),
        ('Fix C+ + margin=50 dwell=60', variant_fix_c_plus(), 50.0, 60.0),
    ]

    print(f"{'variant':<48} | starting at wan1 (off configured)")
    print('-' * 110)
    for name, fn, margin, dwell in variants:
        sim = Simulator(fn, rtt_margin_ms=margin, dwell_s=dwell)
        r = sim.run(ticks, initial_master='wan1')
        print(f"{name:<48} | {fmt(r)}")
    print()
    print(f"{'variant':<48} | starting at wan2 (on configured)")
    print('-' * 110)
    for name, fn, margin, dwell in variants:
        sim = Simulator(fn, rtt_margin_ms=margin, dwell_s=dwell)
        r = sim.run(ticks, initial_master='wan2')
        print(f"{name:<48} | {fmt(r)}")

    print()
    spike_ticks = synthesize_satellite_spike_trace(ticks)
    print(f"{'variant':<48} | + 3 Satellite spikes (15s @ 220ms), starting at wan2")
    print('-' * 110)
    for name, fn, margin, dwell in variants:
        sim = Simulator(fn, rtt_margin_ms=margin, dwell_s=dwell)
        r = sim.run(spike_ticks, initial_master='wan2')
        print(f"{name:<48} | {fmt(r)}")

    print()
    long_spike_ticks = synthesize_satellite_spike_trace(
        ticks, spike_intervals_s=(150,), spike_duration_s=120.0, spike_rtt_ms=220.0)
    print(f"{'variant':<48} | + 1 LONG Satellite spike (120s @ 220ms), starting at wan2")
    print('-' * 110)
    for name, fn, margin, dwell in variants:
        sim = Simulator(fn, rtt_margin_ms=margin, dwell_s=dwell)
        r = sim.run(long_spike_ticks, initial_master='wan2')
        print(f"{name:<48} | {fmt(r)}")

    print()
    loss_ticks = synthesize_loss_event(ticks, start_s=200.0,
                                       duration_s=60.0, which='wan2', loss_pct=8.0)
    print(f"{'variant':<48} | + wan2 loss event (60s @ 8%), starting at wan2")
    print('-' * 110)
    for name, fn, margin, dwell in variants:
        sim = Simulator(fn, rtt_margin_ms=margin, dwell_s=dwell)
        r = sim.run(loss_ticks, initial_master='wan2')
        print(f"{name:<48} | {fmt(r)}")


if __name__ == '__main__':
    main()
