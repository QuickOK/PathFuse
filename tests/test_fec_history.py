import fec_history as H

DIRS = {
    "client_to_relay": {
        "wire": {"tx_mbps": 1.5, "overhead_pct": 50.0},
        "rx": {"delivered_per_s": 100.0, "recovered_per_s": 2.0,
               "lost_pkts_est_per_s": 0.5, "par_waste_per_s": 30.0,
               "totals": {}, "sample_age_s": 0.1, "stale": False},
    },
    "relay_to_client": {"wire": None, "rx": None},
}


def test_append_and_snapshot_flatten():
    h = H.FecHistory()
    h.append_from_directions(1000.0, DIRS)
    s = h.snapshot()
    assert len(s) == 1
    assert s[0]["t"] == 1000.0
    assert s[0]["c2r"]["delivered_per_s"] == 100.0
    assert s[0]["c2r"]["tx_mbps"] == 1.5
    assert s[0]["r2c"]["delivered_per_s"] is None


def test_throttle_one_sample_per_second():
    h = H.FecHistory()
    h.append_from_directions(1000.0, DIRS)
    h.append_from_directions(1000.5, DIRS)   # 0.5s controller tick: dropped
    h.append_from_directions(1001.1, DIRS)
    assert [s["t"] for s in h.snapshot()] == [1000.0, 1001.1]


def test_maxlen_bounds_memory():
    h = H.FecHistory(maxlen=3)
    for i in range(5):
        h.append_from_directions(1000.0 + i, DIRS)
    assert [s["t"] for s in h.snapshot()] == [1002.0, 1003.0, 1004.0]


def test_missing_directions_are_null_not_crash():
    h = H.FecHistory()
    h.append_from_directions(1.0, None)
    h.append_from_directions(2.5, {})
    assert h.snapshot()[0]["c2r"]["tx_mbps"] is None


def test_backwards_time_step_resets_baseline_instead_of_stalling():
    h = H.FecHistory()
    h.append_from_directions(1000.0, DIRS)
    h.append_from_directions(500.0, DIRS)    # wall-clock stepped back: accepted, baseline reset
    h.append_from_directions(500.5, DIRS)    # within min_interval_s of new baseline: throttled
    assert [s["t"] for s in h.snapshot()] == [1000.0, 500.0]
