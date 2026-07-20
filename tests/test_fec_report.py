import fec_report as R

# Real captured lines (see specs/2026-05-24-fec-phase2-report-capture.md)
C1 = "[2026-05-24 09:20:59][INFO][report]client-->server:(original:1899703 pkt;998214940 byte) (fec:1586592 pkt,1032837226 byte)  server-->client:(original:1774549 pkt;980517588 byte) (fec:1907814 pkt;1212643143 byte)"
S1 = "[2026-05-24 09:21:15][INFO][report][127.0.0.1:44479]client-->server:(original:1838962 pkt;951212792 byte) (fec:2215061 pkt;1470999064 byte)  server-->client:(original:1804475 pkt;1003244128 byte) (fec:1451048 pkt;1028694337 byte)"
NOISE = "[2026-05-24 09:20:49][INFO]got command [fec]"


def test_parse_client_line_both_directions():
    r = R.parse_report_line(C1)
    assert r["client_to_server"] == {"orig_pkt": 1899703, "orig_byte": 998214940,
                                     "fec_pkt": 1586592, "fec_byte": 1032837226}
    assert r["server_to_client"] == {"orig_pkt": 1774549, "orig_byte": 980517588,
                                     "fec_pkt": 1907814, "fec_byte": 1212643143}


def test_parse_server_line_ignores_peer_tag():
    r = R.parse_report_line(S1)
    assert r["client_to_server"]["orig_pkt"] == 1838962
    assert r["server_to_client"]["fec_byte"] == 1028694337


def test_parse_non_report_returns_none():
    assert R.parse_report_line(NOISE) is None
    assert R.parse_report_line("") is None


C2 = "[2026-05-24 09:21:09][INFO][report]client-->server:(original:1899756 pkt;998221904 byte) (fec:1586623 pkt,1032844668 byte)  server-->client:(original:1774622 pkt;980531016 byte) (fec:1907911 pkt;1212671533 byte)"


def test_tracker_no_snapshot_before_two_reports():
    t = R.FecWireTracker("client_to_server")
    assert t.snapshot(now=0.0) is None
    t.feed(C1, now=0.0)
    assert t.snapshot(now=0.0) is None  # need two reports for a rate


def test_tracker_computes_throughput_and_overhead():
    t = R.FecWireTracker("client_to_server")
    t.feed(C1, now=0.0)
    t.feed(C2, now=10.0)   # dt=10s
    snap = t.snapshot(now=10.0)
    # d_fec_byte = 1032844668-1032837226 = 7442 -> 7442*8/10/1e6 = 0.006 Mb/s
    assert snap["tx_mbps"] == 0.006
    # d_orig_byte = 998221904-998214940 = 6964 -> (7442-6964)/6964*100 = 6.9%
    assert snap["overhead_pct"] == 6.9
    assert snap["stale"] is False


def test_tracker_marks_stale_and_nulls_rates():
    t = R.FecWireTracker("client_to_server", stale_after_s=30.0)
    t.feed(C1, now=0.0)
    t.feed(C2, now=10.0)
    snap = t.snapshot(now=10.0 + 31)
    assert snap["stale"] is True
    assert snap["tx_mbps"] is None
    assert snap["overhead_pct"] is None


def test_tracker_handles_counter_reset_without_negative_spike():
    t = R.FecWireTracker("client_to_server")
    t.feed(C1, now=0.0)
    t.feed(C2, now=10.0)
    low = C1.replace("original:1899703", "original:100").replace("998214940", "50000") \
            .replace("fec:1586592", "fec:90").replace("1032837226", "60000")
    t.feed(low, now=20.0)           # decrease -> baseline reset, no rate update
    after_reset = t.snapshot(now=20.0)
    assert after_reset["tx_mbps"] == 0.006   # still last good, not a negative spike
    high = C1.replace("original:1899703", "original:200").replace("998214940", "57000") \
             .replace("fec:1586592", "fec:180").replace("1032837226", "67442")
    t.feed(high, now=30.0)
    snap = t.snapshot(now=30.0)
    assert snap["tx_mbps"] >= 0.0 and snap["tx_mbps"] < 1.0  # sane, not huge/negative


import threading


def test_start_wire_tailer_feeds_lines_from_injected_source():
    calls = []
    class FakeTracker:
        def feed(self, msg, now): calls.append(msg)
    src = iter([C1, C2, NOISE])
    stop = threading.Event()
    t = R.start_wire_tailer("unused-unit", FakeTracker(), stop_event=stop, line_source=src)
    t.join(timeout=2)
    assert not t.is_alive()
    assert calls == [C1, C2, NOISE]


RX1 = ("[2026-07-20 10:00:00][INFO][report_fec_rx]pkt_ok:1000 pkt_rec:50 "
       "grp_ok:120 grp_rec:10 grp_fail:2 shard_lost:9 par_waste:37")
RX2 = ("[2026-07-20 10:00:10][INFO][report_fec_rx]pkt_ok:2000 pkt_rec:130 "
       "grp_ok:240 grp_rec:26 grp_fail:4 shard_lost:16 par_waste:57")


def test_parse_fec_rx_line():
    r = R.parse_fec_rx_line(RX1)
    assert r == {"pkt_ok": 1000, "pkt_rec": 50, "grp_ok": 120, "grp_rec": 10,
                 "grp_fail": 2, "shard_lost": 9, "par_waste": 37}
    assert R.parse_fec_rx_line(NOISE) is None
    assert R.parse_fec_rx_line(C1) is None


def test_rx_snapshot_rates_and_lost_estimate():
    t = R.FecWireTracker("client_to_server")
    t.feed(RX1, now=0.0)
    assert t.rx_snapshot(now=0.0) is None      # need two reports for a rate
    t.feed(RX2, now=10.0)
    snap = t.rx_snapshot(now=10.0)
    assert snap["delivered_per_s"] == 100.0    # d pkt_ok 1000 / 10s
    assert snap["recovered_per_s"] == 8.0      # d pkt_rec 80 / 10s
    # avg pkts/group = (1000+80)/(120+16) = 7.94; lost = d grp_fail 2 * 7.94 / 10
    assert snap["lost_pkts_est_per_s"] == 1.6
    assert snap["par_waste_per_s"] == 2.0
    assert snap["totals"]["grp_fail"] == 4
    assert snap["stale"] is False


def test_rx_snapshot_stale_nulls_rates_keeps_totals():
    t = R.FecWireTracker("client_to_server", stale_after_s=30.0)
    t.feed(RX1, now=0.0)
    t.feed(RX2, now=10.0)
    snap = t.rx_snapshot(now=10.0 + 31)
    assert snap["stale"] is True
    assert snap["delivered_per_s"] is None
    assert snap["totals"]["pkt_ok"] == 2000


def test_rx_counter_reset_does_not_emit_negative_rates():
    t = R.FecWireTracker("client_to_server")
    t.feed(RX1, now=0.0)
    t.feed(RX2, now=10.0)
    t.feed(RX1, now=20.0)                      # counters went DOWN: process restart
    snap = t.rx_snapshot(now=20.0)
    assert snap["delivered_per_s"] == 100.0    # last good rate retained
    t.feed(RX2, now=30.0)
    snap = t.rx_snapshot(now=30.0)
    assert snap["delivered_per_s"] == 100.0    # clean resume from new baseline


def test_wire_and_rx_coexist_in_one_tracker():
    t = R.FecWireTracker("client_to_server")
    t.feed(C1, now=0.0)
    t.feed(RX1, now=0.0)
    t.feed(C2, now=10.0)
    t.feed(RX2, now=10.0)
    assert t.snapshot(now=10.0)["tx_mbps"] == 0.006
    assert t.rx_snapshot(now=10.0)["delivered_per_s"] == 100.0
