import json
import sbfd_ctl as M


# Every relay control-plane request goes through M._relay_request, which pools
# the keep-alive connection and returns (status, reason, body_bytes). Patching
# that seam keeps these tests about the payload and the error handling rather
# than about the transport underneath.

def fake_relay(monkeypatch, status=200, reason="OK", resp_body=b"{}", raises=None):
    """Patch the relay transport; return a dict recording what was sent."""
    seen = {}

    def fake(url, method="GET", body=None, headers=None, timeout_s=2.0):
        seen["url"] = url
        seen["method"] = method
        seen["headers"] = headers or {}
        seen["body"] = json.loads(body.decode()) if body else None
        if raises is not None:
            raise raises
        return status, reason, resp_body

    monkeypatch.setattr(M, "_relay_request", fake)
    return seen


def test_fetch_relay_fec_no_url():
    r = M.fetch_relay_fec("", 1.0)
    assert r["ok"] is False and "fec_url" in r["error"]


def test_fetch_relay_fec_success(monkeypatch):
    fake_relay(monkeypatch,
               resp_body=json.dumps({"ratio": "8:4", "enabled": True}).encode())
    r = M.fetch_relay_fec("http://relay/fec", 1.0)
    assert r["ok"] is True
    assert r["data"]["ratio"] == "8:4"


def test_fetch_relay_fec_transport_error(monkeypatch):
    fake_relay(monkeypatch, raises=OSError("down"))
    r = M.fetch_relay_fec("http://relay/fec", 1.0)
    assert r["ok"] is False and r["data"] is None and "transport" in r["error"]


def test_fetch_relay_fec_non_200_is_not_a_parse_error(monkeypatch):
    # A relay that answers 503 must be reported as an HTTP status, not silently
    # folded into a parse failure against the error page's body.
    fake_relay(monkeypatch, status=503, reason="Service Unavailable",
               resp_body=b"<html>nope</html>")
    r = M.fetch_relay_fec("http://relay/fec", 1.0)
    assert r["ok"] is False and r["data"] is None
    assert "503" in r["error"]


def test_fetch_relay_fec_unparseable_body_is_a_parse_error(monkeypatch):
    fake_relay(monkeypatch, resp_body=b"{ not json")
    r = M.fetch_relay_fec("http://relay/fec", 1.0)
    assert r["ok"] is False and r["error"].startswith("parse:")


def test_post_relay_fec_no_url():
    assert M.post_relay_fec("", "adaptive", "20:1", "20:1", 1.0) is False


def test_post_relay_fec_success(monkeypatch):
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://relay/fec", "off", "20:1", "20:1", 1.0) is True
    assert seen["method"] == "POST"
    assert seen["headers"].get("Content-Type") == "application/json"


def test_post_relay_fec_transport_error_returns_false(monkeypatch):
    fake_relay(monkeypatch, raises=OSError("down"))
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0) is False


def test_post_relay_fec_body_includes_mode_and_legacy_enabled(monkeypatch):
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://relay/fec", "fixed", "20:1", "20:1", 1.0) is True
    assert seen["body"]["mode"] == "fixed"
    assert seen["body"]["fixed_ratio"] == "20:1"
    assert seen["body"]["enabled"] is True   # legacy field for older relays
    seen.clear()
    assert M.post_relay_fec("http://relay/fec", "off", "20:1", "20:1", 1.0) is True
    assert seen["body"]["enabled"] is False


def test_fec_driver_wan_picks_max_loss():
    assert M.fec_driver_wan({"wan1": 5.0, "wan2": 1.0}, {"wan1", "wan2"}) == "wan1"


def test_fec_driver_wan_tie_is_deterministic_alphabetically_first():
    # Equal loss (the common clean case, both 0.0) must not depend on set
    # hash order — sorting active_wans before max() makes the tie-break
    # deterministic. max() keeps the first item achieving the maximum, so
    # over a sorted input the alphabetically first WAN wins.
    loss = {"wan1": 0.0, "wan2": 0.0}
    assert M.fec_driver_wan(loss, {"wan1", "wan2"}) == "wan1"
    assert M.fec_driver_wan(loss, {"wan2", "wan1"}) == "wan1"


def test_fec_driver_wan_falls_back_to_loss_keys_when_no_active():
    # mirrors compute_fec_target's `active = active_wans or set(loss.keys())`
    assert M.fec_driver_wan({"wan1": 2.0}, set()) == "wan1"


def test_fec_driver_wan_none_when_nothing():
    assert M.fec_driver_wan({}, set()) is None


def test_should_post_when_desired_differs():
    assert M.should_post_fec(False, True, 100.0, 101.0, 30.0) is True


def test_should_post_on_first_tick():
    assert M.should_post_fec(True, True, None, 0.0, 30.0) is True


def test_should_post_on_heartbeat():
    assert M.should_post_fec(True, True, 0.0, 40.0, 30.0) is True


def test_no_post_when_synced_and_recent():
    assert M.should_post_fec(True, True, 50.0, 60.0, 30.0) is False


def test_relay_fec_direction_ok():
    fetch = {"ok": True, "error": None,
             "data": {"enabled": True, "ratio": "8:2", "level": 1,
                      "driving_loss_pct": 1.2, "since": 5.0}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["ratio"] == "8:2"
    assert d["ok"] is True
    assert abs(d["stale_s"] - 0.5) < 1e-6
    assert d["reconcile_pending"] is False
    assert d["wire"] is None


def test_relay_fec_direction_unreachable_sets_pending():
    fetch = {"ok": False, "data": None, "error": "transport: x"}
    d = M.relay_fec_direction(fetch, fetched_at=None, now=100.0, desired=False, last_acked=True)
    assert d["ok"] is False
    assert d["error"].startswith("transport")
    assert d["stale_s"] is None
    assert d["reconcile_pending"] is True


def test_relay_fec_direction_passes_through_wire():
    fetch = {"ok": True, "error": None, "data": {
        "enabled": True, "ratio": "8:2", "level": 1, "driving_loss_pct": 1.2, "since": 5.0,
        "wire": {"tx_mbps": 3.8, "overhead_pct": 9.0, "sample_age_s": 4.0, "stale": False}}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["wire"] == {"tx_mbps": 3.8, "overhead_pct": 9.0, "sample_age_s": 4.0, "stale": False}


def test_relay_fec_direction_wire_none_when_absent():
    fetch = {"ok": True, "error": None, "data": {"ratio": "8:0"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["wire"] is None


def test_relay_fec_direction_passes_through_ladder():
    lad = {"levels": 4, "floor_level": 1, "applied_level": 3}
    fetch = {"ok": True, "error": None, "data": {
        "enabled": True, "ratio": "8:1", "level": 3, "ladder": lad}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["ladder"] == lad


def _ladder_inputs(scale):
    # Deliberately carries no client mode/floor/fixed: the rebuild must take
    # every setting from the relay's own report or not run at all.
    import fec_control as F
    return {"scale": scale,
            "tables": {"wan1": F.DEFAULT_CELL_LOSS_TABLE},
            "default_table": F.DEFAULT_LOSS_TABLE}


SCALE = ["12:1", "8:1", "8:2", "8:4", "8:6", "8:8"]


def test_relay_fec_direction_rescales_the_relay_ladder():
    # The relay's own ladder is scaled to its view of one profile. The client
    # re-derives it against the shared cross-profile scale so both cards share
    # positions — anchored on what the relay REPORTS.
    fetch = {"ok": True, "error": None, "data": {
        "ratio": "8:4", "level": 2, "mode": "min_adaptive", "floor_ratio": "8:1",
        "profile": "default",
        "ladder": {"levels": 5, "floor_level": 0, "applied_level": 2}}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=True,
                              ladder_inputs=_ladder_inputs(SCALE))
    assert d["ladder"]["scale"] == SCALE
    assert d["ladder"]["applied_index"] == 3          # 8:4
    assert (d["ladder"]["reach_lo"], d["ladder"]["reach_hi"]) == (1, 5)
    # The relay never applies full-redundancy backoff, so its span is never
    # pinned even while the client leg is.
    assert d["ladder"]["pinned"] is False


def test_relay_ladder_follows_the_relay_not_our_desired_settings():
    # Mid-reconcile: we want min_adaptive at 8:1, the relay is still OFF. The
    # card must not claim the floor or the adaptive span we have not got yet.
    fetch = {"ok": True, "error": None, "data": {
        "ratio": "8:0", "mode": "off", "floor_ratio": "8:1",
        "profile": "default"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=False,
                              ladder_inputs=_ladder_inputs(SCALE))
    lad = d["ladder"]
    assert lad["floor_index"] == -1                   # not min_adaptive there
    assert (lad["reach_lo"], lad["reach_hi"]) == (-1, -1)   # 8:0 is off-scale
    assert lad["applied_index"] == -1
    assert lad["below_floor"] is False


def test_relay_ladder_uses_the_relays_floor_after_a_restart():
    # A restarted relay reverts to its config floor for up to a heartbeat. Its
    # 20:1 is beneath every rung of our scale, so the row places nothing rather
    # than pretending the operator's floor is in force.
    fetch = {"ok": True, "error": None, "data": {
        "ratio": "20:1", "mode": "min_adaptive", "floor_ratio": "20:1",
        "profile": "wan1"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=False,
                              ladder_inputs=_ladder_inputs(SCALE))
    assert d["ladder"]["applied_index"] == -1
    assert d["ladder"]["below_floor"] is False        # 20:1 IS the relay's floor


def test_relay_ladder_not_rebuilt_when_the_relay_omits_settings():
    # A minimal or older response is NOT enough. Substituting our desired mode
    # and floor for the missing ones would publish a floor and a reachable span
    # the relay never claimed.
    relay_own = {"levels": 5, "floor_level": 0, "applied_level": 1}
    fetch = {"ok": True, "error": None,
             "data": {"ratio": "8:2", "ladder": relay_own}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=True,
                              ladder_inputs=_ladder_inputs(SCALE))
    assert d["ladder"] == relay_own
    # Fixed mode additionally needs the ratio it is fixed AT.
    nofixed = {"ok": True, "error": None, "data": {
        "ratio": "8:2", "mode": "fixed", "floor_ratio": "8:1",
        "profile": "default"}}
    assert M.relay_fec_direction(nofixed, 100.0, 100.5, True, True,
                                 ladder_inputs=_ladder_inputs(SCALE))["ladder"] is None


def test_relay_ladder_survives_a_non_string_profile():
    # /fec is remote JSON. An unhashable profile value would raise on the dict
    # lookup and take the controller loop down with it.
    for bad in ([], {}, 7, True):
        fetch = {"ok": True, "error": None, "data": {
            "ratio": "8:2", "mode": "min_adaptive", "floor_ratio": "8:1",
            "profile": bad}}
        d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5,
                                  desired=True, last_acked=True,
                                  ladder_inputs=_ladder_inputs(SCALE))
        assert d["ladder"] is None


def test_relay_fec_direction_keeps_relay_ladder_without_inputs():
    fetch = {"ok": True, "error": None,
             "data": {"ratio": "8:2", "ladder": {"levels": 5}}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=True)
    assert d["ladder"] == {"levels": 5}


def test_relay_fec_direction_ladder_none_from_an_older_relay():
    # A relay that predates the field: the UI falls back rather than drawing an
    # empty pip row.
    fetch = {"ok": True, "error": None, "data": {"ratio": "8:2", "level": 1}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["ladder"] is None


# ---------------------------------------------------------------------------
# Direction-correct loss sourcing (2026-07-08): sbfd loss_pct is RX-side, so
# the loss that the client->relay FEC leg repairs is measured at the RELAY.
# The client must drive its TX leg from the relay-fetched snapshot and push
# its own (relay->client direction) measurement to the relay.
# ---------------------------------------------------------------------------

def _snap(ok, per_wan):
    return M.StateSnapshot(ok=ok, per_wan={
        w: M.WanSample(state="UP", rtt_ms=10.0, loss_pct=l)
        for w, l in per_wan.items()})


def test_fec_loss_map_prefers_fresh_remote():
    local = _snap(True, {"wan1": 5.0, "wan2": 1.5})   # relay->client loss (not ours to fix)
    remote = _snap(True, {"wan1": 0.0, "wan2": 0.3})  # client->relay loss (what our TX leg repairs)
    loss, source = M.fec_loss_map(local, remote, remote_fresh=True, wans=["wan1", "wan2"])
    assert loss == {"wan1": 0.0, "wan2": 0.3}
    assert source == "relay"


def test_fec_loss_map_falls_back_when_remote_stale():
    local = _snap(True, {"wan1": 2.0})
    remote = _snap(True, {"wan1": 9.0})
    loss, source = M.fec_loss_map(local, remote, remote_fresh=False, wans=["wan1"])
    assert loss == {"wan1": 2.0}
    assert source == "local"


def test_fec_loss_map_falls_back_when_remote_not_ok():
    local = _snap(True, {"wan1": 2.0})
    remote = M.StateSnapshot(ok=False, per_wan={})
    loss, source = M.fec_loss_map(local, remote, remote_fresh=True, wans=["wan1"])
    assert loss == {"wan1": 2.0}
    assert source == "local"


def test_fec_loss_map_missing_sample_is_zero():
    local = _snap(True, {})
    remote = _snap(True, {"wan1": 1.0})
    loss, source = M.fec_loss_map(local, remote, remote_fresh=True, wans=["wan1", "wan2"])
    assert loss == {"wan1": 1.0, "wan2": 0.0}
    assert source == "relay"


def test_worst_active_loss_max_over_active():
    assert M.worst_active_loss({"wan1": 1.0, "wan2": 4.0}, {"wan2"}) == 4.0


def test_worst_active_loss_falls_back_to_all_wans():
    assert M.worst_active_loss({"wan1": 1.0, "wan2": 4.0}, set()) == 4.0


def test_worst_active_loss_empty():
    assert M.worst_active_loss({}, set()) == 0.0


def test_post_relay_fec_includes_client_loss_pct(monkeypatch):
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0,
                            client_loss_pct=1.47) is True
    assert seen["body"]["client_loss_pct"] == 1.47


def test_post_relay_fec_omits_client_loss_when_none(monkeypatch):
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0) is True
    assert "client_loss_pct" not in seen["body"]


def test_post_relay_fec_http_error_returns_false_and_warns_once(monkeypatch, caplog):
    fake_relay(monkeypatch, status=400, reason="Bad Request",
               resp_body=b'{"error": "unknown wan_profile"}')
    monkeypatch.setattr(M, "_post_relay_fec_last_warned", None)
    caplog.set_level("WARNING")
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0,
                            wan_profile="bogus") is False
    # A persistent 400 (e.g. a relay whose config lacks wan_profiles) must
    # not spam a warning on every reconcile tick.
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0,
                            wan_profile="bogus") is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "400" in warnings[0].getMessage()


def test_post_relay_fec_transport_error_does_not_warn(monkeypatch, caplog):
    fake_relay(monkeypatch, raises=OSError("down"))
    monkeypatch.setattr(M, "_post_relay_fec_last_warned", None)
    caplog.set_level("WARNING")
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0) is False
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_relay_fec_direction_passes_through_loss_source():
    fetch = {"ok": True, "error": None, "data": {"ratio": "8:2", "loss_source": "client_push"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["loss_source"] == "client_push"


def test_post_relay_fec_sends_floor_ratio(monkeypatch):
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://relay/fec", "min_adaptive", "8:2", "8:1",
                            1.0, client_loss_pct=1.25) is True
    assert seen["body"]["floor_ratio"] == "8:1"
    assert seen["body"]["fixed_ratio"] == "8:2"
    assert seen["body"]["mode"] == "min_adaptive"
    assert seen["body"]["enabled"] is True


def test_post_relay_fec_includes_profile_and_signal(monkeypatch):
    seen = fake_relay(monkeypatch)
    ok = M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                          "8:0", 1.0, client_loss_pct=0.4,
                          wan_profile="wan1", signal_floor=True)
    assert ok
    assert seen["body"]["wan_profile"] == "wan1"
    assert seen["body"]["signal_floor"] is True


def test_post_relay_fec_serializes_explicit_false_signal_floor(monkeypatch):
    # signal_floor=False (explicitly passed, e.g. the floor just released)
    # must be distinguished from signal_floor=None (never engaged/omitted) —
    # both are falsy in Python but only the former belongs in the payload.
    seen = fake_relay(monkeypatch)
    ok = M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                          "8:0", 1.0, client_loss_pct=0.4,
                          wan_profile="wan1", signal_floor=False)
    assert ok
    assert "signal_floor" in seen["body"]
    assert seen["body"]["signal_floor"] is False


def test_post_relay_fec_omits_absent_profile(monkeypatch):
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                            "20:1", 1.0, client_loss_pct=0.4)
    # Omitted keys must stay omitted so older relays see an unchanged payload.
    assert "wan_profile" not in seen["body"]
    assert "signal_floor" not in seen["body"]


def test_relay_desired_tuple_includes_floor():
    # A floor change must make should_post_fec see a different desired tuple,
    # or the operator's new floor never reaches the relay.
    last_acked = ("min_adaptive", "8:2", "20:1", 0)
    desired    = ("min_adaptive", "8:2", "8:1", 0)
    assert desired != last_acked
    # last_post_ts=50, now=60, heartbeat=30 -> the heartbeat has NOT elapsed,
    # so True here proves the floor difference alone drove the post.
    assert M.should_post_fec(desired, last_acked, 50.0, 60.0, 30.0) is True
    assert M.should_post_fec(desired, desired, 50.0, 60.0, 30.0) is False


def test_relay_fec_direction_passes_through_floor_ratio():
    # The relay publishes its own floor; the UI needs it to surface a
    # mid-upgrade mismatch instead of hiding it.
    fetch = {"ok": True, "error": None,
             "data": {"enabled": True, "ratio": "8:2", "level": 1,
                      "fixed_ratio": "8:4", "floor_ratio": "8:1"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5,
                              desired=True, last_acked=True)
    assert d["floor_ratio"] == "8:1"


def test_relay_fec_direction_floor_none_when_absent():
    # An older relay that doesn't publish a floor must not fault.
    fetch = {"ok": True, "error": None, "data": {"ratio": "8:0"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5,
                              desired=True, last_acked=True)
    assert d["floor_ratio"] is None


def test_relay_fec_direction_rx_comes_from_local_tracker_not_fetch():
    # The relay->client direction is decoded ON THE CLIENT, so its rx block
    # must come from the local tracker; the relay's own rx (which describes
    # client->relay) must not leak in via the fetch.
    fetch = {"ok": True, "data": {"ratio": "8:2", "rx": {"delivered_per_s": 1.0}}}
    local = {"delivered_per_s": 250.0, "recovered_per_s": 3.0,
             "lost_pkts_est_per_s": 0.0, "par_waste_per_s": 60.0}
    d = M.relay_fec_direction(fetch, 100.0, 101.0, ("adaptive", "8:2"),
                              ("adaptive", "8:2"), local_rx=local)
    assert d["rx"] == local


def test_relay_fec_direction_rx_defaults_none():
    d = M.relay_fec_direction({"ok": True, "data": {}}, 100.0, 101.0, "a", "a")
    assert d["rx"] is None


def test_relay_ladder_not_rebuilt_when_the_fetch_failed():
    # data is empty, so every input would fall back to OUR desired settings —
    # a relay-confirmed span drawn from nothing the relay confirmed.
    fetch = {"ok": False, "data": None, "error": "transport: x"}
    d = M.relay_fec_direction(fetch, fetched_at=None, now=100.0, desired=True,
                              last_acked=False,
                              ladder_inputs=_ladder_inputs(SCALE))
    assert d["ladder"] is None


def test_relay_ladder_not_rebuilt_for_an_unknown_profile():
    # The relay keeps reporting a pushed profile name until it goes stale; if
    # that profile has since been removed from our config, modelling it as the
    # default table would claim rungs its real (cellular) table caps below.
    relay_own = {"levels": 4, "floor_level": 1, "applied_level": 3}
    fetch = {"ok": True, "error": None, "data": {
        "ratio": "8:1", "mode": "min_adaptive", "floor_ratio": "20:1",
        "profile": "wan9", "ladder": relay_own}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=False,
                              ladder_inputs=_ladder_inputs(SCALE))
    assert d["ladder"] == relay_own          # the relay's own view is kept


def test_relay_ladder_rebuilt_for_the_default_profile():
    # "default" is not a key in tables but IS resolvable — it means the base
    # table, so this one must still be rebuilt onto the shared scale.
    fetch = {"ok": True, "error": None, "data": {
        "ratio": "8:2", "mode": "min_adaptive", "floor_ratio": "8:1",
        "profile": "default"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=True,
                              ladder_inputs=_ladder_inputs(SCALE))
    assert d["ladder"]["scale"] == SCALE
    assert d["ladder"]["applied_index"] == 2


# ---------------------------------------------------------------------------
# Sticky FEC driver. The driver selects table+floor+hysteresis, so flipping it
# swaps the ladder under the adaptive engine — it needs the same margin+dwell
# protection the ratio already has.
# ---------------------------------------------------------------------------

DWELL = 120.0


def _pick(loss, active, cur, cand, since, now):
    return M.fec_driver_pick(loss, active, cur, cand, since, DWELL, now)


def _replay(samples, start_driver, dwell=DWELL, step=0.5):
    """Feed (duration_s, loss_dict) segments through the picker. Returns
    (final_driver, switch_count)."""
    cur, cand, since, switches, t = start_driver, None, None, 0, 0.0
    for dur, loss in samples:
        end = t + dur
        while t < end:
            d, cand, since = M.fec_driver_pick(loss, {"wan1", "wan2"}, cur,
                                               cand, since, dwell, t)
            if d != cur:
                switches += 1
            cur = d
            t += step
    return cur, switches


def test_driver_rides_out_the_observed_blip_pattern():
    # The measured shape: ~95s where the other link is briefly worse, ~40x a
    # day. Before this change each one flipped the profile and reseeded the
    # adaptive runtime; the dwell now outlasts the excursion.
    quiet = {"wan1": 0.0, "wan2": 0.0}
    blip = {"wan1": 0.0, "wan2": 0.3}
    driver, switches = _replay([(600.0, quiet), (95.0, blip), (600.0, quiet)] * 4,
                               start_driver="wan1")
    assert (driver, switches) == ("wan1", 0)


def test_driver_holds_a_candidate_during_a_blip_without_yielding():
    d, c, s = _pick({"wan1": 0.0, "wan2": 0.3}, {"wan1", "wan2"}, "wan1",
                    None, None, 100.0)
    assert d == "wan1" and c == "wan2"


def test_driver_dwell_clock_is_not_restarted_by_a_persisting_challenger():
    # Distinct from creating the candidate: an unchanged challenger must keep
    # its ORIGINAL timestamp. Restarting it each tick would mean no challenge
    # could ever accumulate dwell and the driver would never move again.
    active = {"wan1", "wan2"}
    loss = {"wan1": 0.0, "wan2": 2.0}
    d, c, s = _pick(loss, active, "wan1", None, None, 100.0)
    assert (c, s) == ("wan2", 100.0)
    for t in (100.5, 130.0, 100.0 + DWELL - 0.5):
        d, c, s = _pick(loss, active, "wan1", c, s, t)
        assert (d, c, s) == ("wan1", "wan2", 100.0)      # clock still at 100.0
    d, c, s = _pick(loss, active, "wan1", c, s, 100.0 + DWELL)
    assert d == "wan2"


def test_driver_challenger_is_the_worst_link_not_the_first_by_name():
    # With 3+ active WANs the challenger must be the most degraded path, since
    # it selects the table/floor/hysteresis that will be applied.
    active = {"wan1", "wan2", "wan3"}
    loss = {"wan1": 0.0, "wan2": 2.0, "wan3": 9.0}
    d, c, s = _pick(loss, active, "wan1", None, None, 100.0)
    assert c == "wan3"
    d, c, s = _pick(loss, active, "wan1", c, s, 100.0 + DWELL)
    assert d == "wan3"


def test_driver_candidate_is_dropped_when_the_challenge_lapses():
    active = {"wan1", "wan2"}
    d, c, s = _pick({"wan1": 0.0, "wan2": 2.0}, active, "wan1", None, None, 100.0)
    assert c == "wan2"
    # Both clean again: the incumbent IS the canonical pick, so no challenger
    # remains and the dwell clock must not keep running.
    d, c, s = _pick({"wan1": 0.0, "wan2": 0.0}, active, "wan1", c, s, 160.0)
    assert (d, c, s) == ("wan1", None, None)


def test_driver_comes_home_after_a_degradation_ends():
    # Without an implicit challenger the driver would stay on wan2 forever once
    # it got there, and the cell signal floor — which only engages while the
    # cellular WAN drives — would stay disarmed.
    driver, switches = _replay([
        (300.0, {"wan1": 0.0, "wan2": 0.0}),     # steady on wan1
        (900.0, {"wan1": 0.0, "wan2": 3.0}),     # wan2 degrades, takes over
        (900.0, {"wan1": 0.0, "wan2": 0.0}),     # recovers
    ], start_driver="wan1")
    assert driver == "wan1"
    assert switches == 2                          # away and back, nothing more


def test_driver_needs_margin_and_dwell_before_switching():
    active = {"wan1", "wan2"}
    # Over the margin: becomes a candidate, but does not take over yet.
    d, c, s = _pick({"wan1": 0.0, "wan2": 2.0}, active, "wan1", None, None, 100.0)
    assert (d, c, s) == ("wan1", "wan2", 100.0)
    # Still inside the dwell.
    d, c, s = _pick({"wan1": 0.0, "wan2": 2.0}, active, "wan1", c, s, 100.0 + DWELL - 1)
    assert d == "wan1" and c == "wan2"
    # Dwell satisfied.
    d, c, s = _pick({"wan1": 0.0, "wan2": 2.0}, active, "wan1", c, s, 100.0 + DWELL)
    assert (d, c, s) == ("wan2", None, None)


def test_driver_short_circuits_when_only_one_wan_is_active():
    # Every mode but full redundancy. No race, so no dwell — this must never
    # delay a master_backup failover.
    d, c, s = _pick({"wan1": 9.0, "wan2": 0.0}, {"wan2"}, "wan1", "wan2", 100.0, 101.0)
    assert (d, c, s) == ("wan2", None, None)


def test_driver_repicks_when_the_incumbent_stops_carrying_traffic():
    d, c, s = _pick({"wan1": 0.0, "wan2": 4.0}, {"wan2", "wan3"}, "wan1",
                    None, None, 100.0)
    assert d == "wan2" and c is None


def test_driver_tie_no_longer_thrashes_on_alphabetical_order():
    # Both clean: whoever holds it keeps it, instead of the sorted-first WAN
    # winning every tick.
    active = {"wan1", "wan2"}
    assert _pick({"wan1": 0.0, "wan2": 0.0}, active, "wan2", None, None, 100.0)[0] == "wan2"
    assert _pick({"wan1": 0.0, "wan2": 0.0}, active, "wan1", None, None, 100.0)[0] == "wan1"


def test_driver_adopts_a_joining_wan_without_making_it_serve_the_dwell():
    # Entering full redundancy: wan2 was the sole active link, so it holds the
    # driver by short-circuit, and wan1 arrives. Both are clean, so the tie
    # resolves to wan1 — but as a plain challenger it would owe 120s of dwell
    # first, and for that whole time the default profile keeps driving the
    # cellular link. A WAN that just joined never had the chance to compete for
    # the incumbency it is being measured against.
    d, c, s = M.fec_driver_pick({"wan1": 0.0, "wan2": 0.0}, {"wan1", "wan2"},
                                "wan2", None, None, DWELL, 100.0,
                                prev_active={"wan2"})
    assert (d, c, s) == ("wan1", None, None)


def test_driver_still_makes_an_incumbent_rival_serve_the_dwell_after_a_join():
    # Only the newcomer skips the queue. wan3 arriving clean while wan2 — which
    # was active all along — degrades must start an ordinary challenge: the
    # membership change is not a licence to re-rank everyone.
    d, c, s = M.fec_driver_pick({"wan1": 0.0, "wan2": 4.0, "wan3": 0.0},
                                {"wan1", "wan2", "wan3"}, "wan1", None, None,
                                DWELL, 100.0, prev_active={"wan1", "wan2"})
    assert (d, c, s) == ("wan1", "wan2", 100.0)


def test_driver_keeps_a_running_dwell_when_a_bystander_joins():
    # wan2 is mid-challenge against wan1. wan3 joining is unrelated to that
    # race, and must not hand wan2 the driver ahead of its clock.
    active = {"wan1", "wan2"}
    loss = {"wan1": 0.0, "wan2": 2.0, "wan3": 0.0}
    d, c, s = _pick(loss, active, "wan1", None, None, 100.0)
    assert (c, s) == ("wan2", 100.0)
    d, c, s = M.fec_driver_pick(loss, {"wan1", "wan2", "wan3"}, "wan1", c, s,
                                DWELL, 130.0, prev_active=active)
    assert (d, c, s) == ("wan1", "wan2", 100.0)      # clock still at 100.0


def test_driver_keeps_a_running_dwell_when_a_bystander_leaves():
    # The mirror case: losing a WAN the incumbent already outranked changes
    # nothing about the challenge in flight.
    loss = {"wan1": 0.0, "wan2": 2.0, "wan3": 0.0}
    d, c, s = _pick(loss, {"wan1", "wan2", "wan3"}, "wan1", None, None, 100.0)
    assert (c, s) == ("wan2", 100.0)
    d, c, s = M.fec_driver_pick(loss, {"wan1", "wan2"}, "wan1", c, s,
                                DWELL, 130.0, prev_active={"wan1", "wan2", "wan3"})
    assert (d, c, s) == ("wan1", "wan2", 100.0)


def test_driver_bootstraps_to_the_raw_ranking():
    d, c, s = _pick({"wan1": 1.0, "wan2": 5.0}, {"wan1", "wan2"}, None,
                    None, None, 100.0)
    assert (d, c, s) == ("wan2", None, None)
    assert M.fec_driver_pick({}, set(), None, None, None, DWELL, 1.0) == \
        (None, None, None)


# ---------------------------------------------------------------------------
# Location floor on the relay leg: the level we resolved for this place has to
# reach the relay, and what the relay says it is holding has to come back.
# ---------------------------------------------------------------------------

def test_post_relay_fec_includes_the_location_level(monkeypatch):
    seen = fake_relay(monkeypatch)
    ok = M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                          "8:0", 1.0, client_loss_pct=0.4,
                          wan_profile="wan1", signal_floor=False,
                          location_level=3)
    assert ok
    assert seen["body"]["location_level"] == 3


def test_post_relay_fec_serializes_a_zero_location_level(monkeypatch):
    # 0 is the release, and it must go on the wire: omitting it would leave a
    # relay holding the last level it was told until the push went stale.
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                            "8:0", 1.0, wan_profile="wan1", location_level=0)
    assert seen["body"]["location_level"] == 0


def test_post_relay_fec_omits_the_location_level_when_none(monkeypatch):
    # Rolling upgrade: with no level to send the payload must be byte-for-byte
    # what an older relay already understands.
    seen = fake_relay(monkeypatch)
    assert M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                            "20:1", 1.0, client_loss_pct=0.4)
    assert "location_level" not in seen["body"]


def test_post_relay_fec_drops_a_junk_location_level_without_raising(monkeypatch):
    # This module is fail-open and runs inside the controller tick: a level
    # that is not a plain int must cost us the KEY, never the push (and never
    # an exception out of int()). A bool would be a guaranteed 400 at the
    # relay, so it goes the same way.
    seen = fake_relay(monkeypatch)
    for junk in ("3", 3.5, True, object()):
        seen.clear()
        assert M.post_relay_fec("http://192.0.2.9/fec", "min_adaptive", "20:1",
                                "8:0", 1.0, wan_profile="wan1",
                                location_level=junk) is True
        assert "location_level" not in seen["body"]
        assert seen["body"]["wan_profile"] == "wan1"   # the push still went


def test_relay_fec_direction_echoes_the_location_level():
    fetch = {"ok": True, "error": None,
             "data": {"ratio": "8:2", "location_level": 2}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5,
                              desired=True, last_acked=True)
    assert d["location_level"] == 2


def test_relay_fec_direction_location_level_none_from_an_older_relay():
    fetch = {"ok": True, "error": None, "data": {"ratio": "8:2"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5,
                              desired=True, last_acked=True)
    assert d["location_level"] is None
