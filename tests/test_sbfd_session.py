"""Tests for sbfd.py's per-session state machine and tx path.

These cover the failure mode where a link is up in one direction only: we
still hear the peer, so our detect timer never fires, but the peer has
stopped hearing us. The peer says so in every packet (FLAG_UP == "I'm
hearing you"), and a session that ignores that can sit in UP forever while
the other end has been DOWN for hours.
"""
import logging
import socket

import pytest

import sbfd as M


def _session(state=M.State.UP, sid=1, **kw):
    """A session already established with a peer, ready to receive."""
    cfg = M.SessionConfig(session_id=sid, name="wan1", local_iface=None,
                          peer_host="198.51.100.10", peer_port=3785, **kw)
    return M.Session(cfg=cfg, state=state, state_since=M.now_s(),
                     last_rx_seq=10, last_rx_time=M.now_s(),
                     peer_sockaddr=("198.51.100.10", 3785))


def _packet(flags, seq=11, sid=1):
    return (M.MAGIC, M.VERSION, flags, sid, seq, M.now_us(), 0, 0)


PEER = ("198.51.100.10", 3785)


# -- Peer-reported liveness --------------------------------------------------

def test_up_session_goes_down_when_peer_stops_reporting_up():
    """The peer clearing FLAG_UP means it no longer hears us: the path is
    one-way, so the session is not UP however well we hear the peer."""
    sess = _session(state=M.State.UP)

    M.on_rx(sess, _packet(flags=0), PEER)

    assert sess.state == M.State.DOWN


def test_up_session_stays_up_while_peer_reports_up():
    sess = _session(state=M.State.UP)

    M.on_rx(sess, _packet(flags=M.FLAG_UP), PEER)

    assert sess.state == M.State.UP


def test_one_way_path_parks_in_init_rather_than_flapping():
    """Once demoted, a peer that still cannot hear us must not drive an
    endless DOWN/INIT log storm: the session settles in INIT."""
    sess = _session(state=M.State.UP)
    seq = 11

    states = []
    for _ in range(5):
        M.on_rx(sess, _packet(flags=0, seq=seq), PEER)
        states.append(sess.state)
        seq += 1

    assert states == [M.State.DOWN, M.State.INIT, M.State.INIT,
                      M.State.INIT, M.State.INIT]


def test_session_recovers_to_up_once_peer_hears_us_again():
    sess = _session(state=M.State.UP)

    M.on_rx(sess, _packet(flags=0, seq=11), PEER)      # UP -> DOWN
    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=12), PEER)  # DOWN -> INIT
    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=13), PEER)  # INIT -> UP

    assert sess.state == M.State.UP


# -- Send-failure visibility -------------------------------------------------

class _FailingSock:
    """A socket whose sendto always fails, as it does when the bound
    interface has no usable route or an nft rule rejects the packet."""

    def __init__(self, err=None):
        self.err = err or OSError(101, "Network is unreachable")
        self.sends = 0

    def sendto(self, pkt, dest):
        self.sends += 1
        raise self.err


class _OkSock:
    def __init__(self):
        self.sends = 0

    def sendto(self, pkt, dest):
        self.sends += 1
        return len(pkt)


@pytest.fixture
def clock(monkeypatch):
    """A controllable now_s() so rate-limit windows are exact, not timed."""
    t = [1_000.0]
    monkeypatch.setattr(M, "now_s", lambda: t[0])
    return t


def _sending_session(sock):
    sess = _session(state=M.State.UP)
    sess.sock = sock
    sess.last_rx_time = 0.0
    return sess


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_send_failure_is_logged_at_warning(caplog, clock):
    """A silent tx path is the whole reason a one-way break went unnoticed
    for hours; DEBUG is not visible in production."""
    sess = _sending_session(_FailingSock())

    with caplog.at_level(logging.WARNING):
        M.send_packet(sess)

    warns = _warnings(caplog)
    assert len(warns) == 1
    assert "sendto failed" in warns[0].getMessage()
    assert "wan1" in warns[0].getMessage()


def test_repeated_send_failures_log_once_per_interval(caplog, clock):
    sess = _sending_session(_FailingSock())

    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            M.send_packet(sess)
            clock[0] += 0.75

    assert len(_warnings(caplog)) == 1


def test_send_failures_log_again_after_the_interval_with_a_count(caplog, clock):
    sess = _sending_session(_FailingSock())

    with caplog.at_level(logging.WARNING):
        M.send_packet(sess)                       # logs immediately
        clock[0] += M.SEND_ERROR_LOG_INTERVAL_S + 1
        M.send_packet(sess)                       # window elapsed, logs again

    warns = _warnings(caplog)
    assert len(warns) == 2
    assert "2 failures" in warns[1].getMessage()


def test_recovered_send_is_logged_and_resets_the_counter(caplog, clock):
    sess = _sending_session(_FailingSock())
    M.send_packet(sess)
    sess.sock = _OkSock()

    with caplog.at_level(logging.INFO):
        M.send_packet(sess)

    assert sess.consecutive_send_errors == 0
    assert any("sendto recovered" in r.getMessage() for r in caplog.records)


def test_successful_sends_log_nothing(caplog, clock):
    sess = _sending_session(_OkSock())

    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            M.send_packet(sess)

    assert caplog.records == []


# -- Socket self-repair ------------------------------------------------------

@pytest.fixture
def free_port():
    """A port range no one else holds, for real bind() calls."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port - 1          # make_session_socket adds session_id (1)


@pytest.fixture
def rebuildable(free_port):
    """A session holding a real socket, plus the daemon config that made it."""
    cfg = M.SessionConfig(session_id=1, name="wan1", local_iface=None,
                          peer_host="198.51.100.10", peer_port=3785)
    dcfg = M.DaemonConfig(bind_host="127.0.0.1", bind_port=free_port)
    sess = M.Session(cfg=cfg, state=M.State.UP, state_since=M.now_s())
    sess.sock = M.make_session_socket(cfg, dcfg.bind_host, dcfg.bind_port)
    yield sess, dcfg
    if sess.sock is not None:
        sess.sock.close()


def test_socket_is_not_rebuilt_below_the_failure_threshold(rebuildable, clock):
    sess, dcfg = rebuildable
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD - 1
    before = sess.sock

    assert M.maybe_rebuild_socket(sess, dcfg) is False
    assert sess.sock is before


def test_sustained_send_failures_re_create_the_socket(rebuildable, clock, caplog):
    """Only a restart cleared the real incident. Rebuilding the socket is
    what the restart did; do it without losing the process."""
    sess, dcfg = rebuildable
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    before = sess.sock

    with caplog.at_level(logging.WARNING):
        rebuilt = M.maybe_rebuild_socket(sess, dcfg)

    assert rebuilt is True
    assert sess.sock is not before
    assert sess.consecutive_send_errors == 0
    assert before.fileno() == -1, "old socket must be closed"
    assert any("re-created socket" in r.getMessage() for r in _warnings(caplog))


def test_rebuilt_socket_has_a_new_fileno(rebuildable, clock):
    """The run loop maps fileno -> session; a rebuild invalidates that map."""
    sess, dcfg = rebuildable
    old_fd = sess.sock.fileno()
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD

    M.maybe_rebuild_socket(sess, dcfg)

    assert sess.sock.fileno() != old_fd


def test_socket_rebuild_is_rate_limited(rebuildable, clock):
    sess, dcfg = rebuildable
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    assert M.maybe_rebuild_socket(sess, dcfg) is True

    clock[0] += M.SOCKET_REBUILD_INTERVAL_S / 2
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    assert M.maybe_rebuild_socket(sess, dcfg) is False


def test_socket_rebuild_resumes_after_the_interval(rebuildable, clock):
    sess, dcfg = rebuildable
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    M.maybe_rebuild_socket(sess, dcfg)

    clock[0] += M.SOCKET_REBUILD_INTERVAL_S + 1
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    assert M.maybe_rebuild_socket(sess, dcfg) is True


def test_failed_rebuild_keeps_the_existing_socket(rebuildable, clock, monkeypatch):
    sess, dcfg = rebuildable
    before = sess.sock
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD

    def boom(*a, **kw):
        raise OSError(99, "Cannot assign requested address")
    monkeypatch.setattr(M, "make_session_socket", boom)

    assert M.maybe_rebuild_socket(sess, dcfg) is False
    assert sess.sock is before
    assert before.fileno() != -1


# -- Reordering around a demotion --------------------------------------------

def test_delayed_packets_from_before_a_demotion_do_not_restore_up():
    """Reordered FLAG_UP packets that predate the demotion say nothing about
    whether the peer hears us *now*; they must not walk us back to UP."""
    sess = _session(state=M.State.UP)          # last_rx_seq == 10

    M.on_rx(sess, _packet(flags=0, seq=13), PEER)           # UP -> DOWN
    assert sess.state == M.State.DOWN

    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=11), PEER)   # stale, reordered
    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=12), PEER)   # stale, reordered

    assert sess.state == M.State.DOWN


def test_peer_that_restarts_its_sequence_space_still_resyncs():
    """The replay guard must not outlive the peer that set it: a restarted
    peer begins again at a low seq and has to be able to come back."""
    sess = _session(state=M.State.UP)
    sess.last_rx_seq = 4_851_829

    M.on_rx(sess, _packet(flags=0, seq=4_851_830), PEER)    # UP -> DOWN
    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=1), PEER)    # peer restarted
    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=2), PEER)

    assert sess.state == M.State.UP


def test_duplicate_packet_is_still_filtered():
    sess = _session(state=M.State.UP)
    sess.last_rx_seq = 20
    sess.rx_count_window = 0

    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=20), PEER)

    assert sess.rx_count_window == 0


# -- Clock-origin independence -----------------------------------------------

def test_first_send_failure_logs_even_at_time_zero(caplog, clock):
    """Eligibility must not depend on the wall clock being far from zero."""
    clock[0] = 0.0
    sess = _sending_session(_FailingSock())

    with caplog.at_level(logging.WARNING):
        M.send_packet(sess)

    assert len(_warnings(caplog)) == 1


def test_first_socket_rebuild_happens_even_at_time_zero(rebuildable, clock):
    clock[0] = 0.0
    sess, dcfg = rebuildable
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD

    assert M.maybe_rebuild_socket(sess, dcfg) is True


def test_rewind_exactly_at_the_restart_threshold_is_treated_as_reordering():
    """SEQ_RESTART_REWIND is the largest rewind still called reordering."""
    sess = _session(state=M.State.UP)
    sess.last_rx_seq = 10_000

    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=10_000 - M.SEQ_RESTART_REWIND), PEER)

    assert sess.last_rx_seq == 10_000


def test_rewind_past_the_restart_threshold_is_still_accepted():
    sess = _session(state=M.State.UP)
    sess.last_rx_seq = 10_000

    M.on_rx(sess, _packet(flags=M.FLAG_UP, seq=10_000 - M.SEQ_RESTART_REWIND - 1), PEER)

    assert sess.last_rx_seq == 10_000 - M.SEQ_RESTART_REWIND - 1


def test_second_send_failure_stays_quiet_after_a_first_at_time_zero(caplog, clock):
    """An action recorded at t=0 must not read back as 'never happened'."""
    clock[0] = 0.0
    sess = _sending_session(_FailingSock())

    with caplog.at_level(logging.WARNING):
        M.send_packet(sess)
        clock[0] = 30.0
        M.send_packet(sess)

    assert len(_warnings(caplog)) == 1


def test_second_rebuild_is_refused_after_a_rebuild_at_time_zero(rebuildable, clock):
    clock[0] = 0.0
    sess, dcfg = rebuildable
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    assert M.maybe_rebuild_socket(sess, dcfg) is True

    clock[0] = 30.0
    sess.consecutive_send_errors = M.SEND_ERROR_REBUILD_THRESHOLD
    assert M.maybe_rebuild_socket(sess, dcfg) is False
