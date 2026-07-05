import station_tracker as ST


def make(**kw):
    args = dict(radius_m=150.0, dwell_speed_ms=1.0, dwell_min_s=600.0,
                hold_s=900.0, max_stations=16, predict_n=2)
    args.update(kw)
    return ST.StationTracker(**args)


def dwell(t, lat, lon, t0, dur=700.0, step=60.0, jitter=0.0):
    """Feed stationary fixes lat/lon for dur seconds; returns end time."""
    now = t0
    i = 0
    while now <= t0 + dur:
        j = jitter * (1 if i % 2 else -1)
        t.update((lat + j, lon + j, 0.0), now)
        now += step
        i += 1
    return now


def drive(t, lat, lon, t0):
    t.update((lat, lon, 15.0), t0)
    return t0 + 60.0


def test_haversine_known_distance():
    # ~111.19 km per degree latitude
    assert abs(ST.haversine_m(35.0, -97.0, 36.0, -97.0) - 111195) < 200


def test_dwell_creates_station_after_min_duration():
    t = make()
    now = 1000.0
    t.update((35.0, -97.0, 0.0), now)
    t.update((35.0, -97.0, 0.0), now + 300.0)
    assert t.stations == {}                       # not yet
    t.update((35.0, -97.0, 0.0), now + 601.0)
    assert len(t.stations) == 1
    st = list(t.stations.values())[0]
    assert st["visits"] == 1


def test_short_stop_is_not_a_station():
    t = make()
    now = dwell(t, 35.0, -97.0, 1000.0, dur=300.0)  # only 5 min
    drive(t, 35.001, -97.001, now)
    assert t.stations == {}


def test_revisit_joins_and_averages_centroid():
    t = make()
    now = dwell(t, 35.0000, -97.0000, 1000.0)
    now = drive(t, 35.01, -97.01, now)
    # second visit ~45 m east, scattered fixes
    now = dwell(t, 35.0000, -96.9995, now + 3600.0, jitter=0.0003)
    assert len(t.stations) == 1                   # joined, not new
    st = list(t.stations.values())[0]
    assert 35.0 - 0.001 < st["lat"] < 35.0 + 0.001
    assert -97.0 < st["lon"] < -96.999            # centroid pulled east a bit
    assert st["visits"] == 2
    # pre-arrival dwell fixes contribute via their mean (1 absorb per arrival),
    # post-arrival fixes absorb individually: >= 3 after two visits
    assert st["n_fixes"] >= 3


def test_far_dwell_creates_second_station():
    t = make()
    now = dwell(t, 35.0, -97.0, 1000.0)
    now = drive(t, 35.1, -97.1, now)
    dwell(t, 35.1, -97.1, now + 600.0)
    assert len(t.stations) == 2


def test_transitions_and_prediction():
    t = make()
    now = dwell(t, 35.0, -97.0, 1000.0)           # A
    now = drive(t, 35.05, -97.0, now)
    now = dwell(t, 35.1, -97.0, now + 600.0)      # B  (A->B)
    now = drive(t, 35.15, -97.0, now)
    now = dwell(t, 35.2, -97.0, now + 600.0)      # C  (B->C)
    now = drive(t, 35.1, -97.0, now)
    now = dwell(t, 35.0, -97.0, now + 600.0)      # A  (C->A)
    now = drive(t, 35.05, -97.0, now)
    now = dwell(t, 35.1, -97.0, now + 600.0)      # B  (A->B again)
    # currently at B; history says B->C
    pts = t.predict_points()
    assert len(pts) >= 1
    assert abs(pts[0][0] - 35.2) < 0.01           # C predicted first


def test_no_history_predicts_nothing():
    t = make()
    dwell(t, 35.0, -97.0, 1000.0)
    assert t.predict_points() == []


def test_same_station_revisit_records_no_self_transition():
    t = make()
    now = dwell(t, 35.0, -97.0, 1000.0)
    now = drive(t, 35.002, -97.0, now)            # short hop, comes right back
    dwell(t, 35.0, -97.0, now + 120.0)
    assert t.transitions == {}
