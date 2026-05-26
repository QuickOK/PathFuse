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
    assert M.post_relay_fec("", "adaptive", "20:1", 1.0) is False


def test_post_relay_fec_success(monkeypatch):
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(M.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    assert M.post_relay_fec("http://relay/fec", "off", "20:1", 1.0) is True


def test_post_relay_fec_transport_error_returns_false(monkeypatch):
    def boom(req, timeout=None):
        raise M.urllib.error.URLError("down")
    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    assert M.post_relay_fec("http://relay/fec", "adaptive", "20:1", 1.0) is False


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
    assert M.post_relay_fec("http://relay/fec", "fixed", "20:1", 1.0) is True
    assert seen["body"]["mode"] == "fixed"
    assert seen["body"]["fixed_ratio"] == "20:1"
    assert seen["body"]["enabled"] is True   # legacy field for older relays
    seen.clear()
    assert M.post_relay_fec("http://relay/fec", "off", "20:1", 1.0) is True
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
