import json
import sbfd_ctl as M


def test_fetch_relay_fec_no_url():
    r = M.fetch_relay_fec("", 1.0)
    assert r["ok"] is False and "fec_url" in r["error"]


def test_fetch_relay_fec_success(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ratio": "8:4", "enabled": True}).encode()
    monkeypatch.setattr(M.urllib.request, "urlopen", lambda url, timeout=None: FakeResp())
    r = M.fetch_relay_fec("http://relay/fec", 1.0)
    assert r["ok"] is True
    assert r["data"]["ratio"] == "8:4"


def test_fetch_relay_fec_transport_error(monkeypatch):
    def boom(url, timeout=None):
        raise M.urllib.error.URLError("down")
    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    r = M.fetch_relay_fec("http://relay/fec", 1.0)
    assert r["ok"] is False and r["data"] is None and "transport" in r["error"]


def test_post_relay_fec_no_url():
    assert M.post_relay_fec("", "adaptive", "20:1", "20:1", 1.0) is False


def test_post_relay_fec_success(monkeypatch):
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(M.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    assert M.post_relay_fec("http://relay/fec", "off", "20:1", "20:1", 1.0) is True


def test_post_relay_fec_transport_error_returns_false(monkeypatch):
    def boom(req, timeout=None):
        raise M.urllib.error.URLError("down")
    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0) is False


def test_post_relay_fec_body_includes_mode_and_legacy_enabled(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()
    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    assert M.post_relay_fec("http://relay/fec", "fixed", "20:1", "20:1", 1.0) is True
    assert seen["body"]["mode"] == "fixed"
    assert seen["body"]["fixed_ratio"] == "20:1"
    assert seen["body"]["enabled"] is True   # legacy field for older relays
    seen.clear()
    assert M.post_relay_fec("http://relay/fec", "off", "20:1", "20:1", 1.0) is True
    assert seen["body"]["enabled"] is False


def test_fec_driver_wan_picks_max_loss():
    assert M.fec_driver_wan({"wan1": 5.0, "wan2": 1.0}, {"wan1", "wan2"}) == "wan1"


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
    seen = {}
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()
    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0,
                            client_loss_pct=1.47) is True
    assert seen["body"]["client_loss_pct"] == 1.47


def test_post_relay_fec_omits_client_loss_when_none(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()
    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", "20:1", 1.0) is True
    assert "client_loss_pct" not in seen["body"]


def test_relay_fec_direction_passes_through_loss_source():
    fetch = {"ok": True, "error": None, "data": {"ratio": "8:2", "loss_source": "client_push"}}
    d = M.relay_fec_direction(fetch, fetched_at=100.0, now=100.5, desired=True, last_acked=True)
    assert d["loss_source"] == "client_push"


def test_post_relay_fec_sends_floor_ratio(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()
    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    assert M.post_relay_fec("http://relay/fec", "min_adaptive", "8:2", "8:1",
                            1.0, client_loss_pct=1.25) is True
    assert seen["body"]["floor_ratio"] == "8:1"
    assert seen["body"]["fixed_ratio"] == "8:2"
    assert seen["body"]["mode"] == "min_adaptive"
    assert seen["body"]["enabled"] is True


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
