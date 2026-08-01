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


def test_relay_fec_direction_rescales_the_relay_ladder(monkeypatch):
    # The relay's own ladder is scaled to its view of one profile. The client
    # re-derives it against the shared cross-profile scale so both cards share
    # positions — anchored on the ratio the relay REPORTS, not our desired one.
    import fec_control as F
    scale = ["12:1", "8:1", "8:2", "8:4", "8:6", "8:8"]
    reach = F.reachable_ratios(F.MODE_MIN_ADAPTIVE, F.DEFAULT_LOSS_TABLE, "8:1")
    fetch = {"ok": True, "error": None, "data": {
        "ratio": "8:4", "level": 2,
        "ladder": {"levels": 5, "floor_level": 0, "applied_level": 2}}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True,
                              last_acked=True,
                              ladder_inputs=(scale, reach, "8:1",
                                             F.MODE_MIN_ADAPTIVE))
    assert d["ladder"]["scale"] == scale
    assert d["ladder"]["applied_index"] == 3          # 8:4
    assert (d["ladder"]["reach_lo"], d["ladder"]["reach_hi"]) == (1, 5)
    # The relay never applies full-redundancy backoff, so its span is never
    # pinned even while the client leg is.
    assert d["ladder"]["pinned"] is False


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
