"""sbfd config loading."""
import dataclasses
import json

import sbfd as M


def _cfg(tmp_path, session_extra=None):
    session = {"session_id": 1, "name": "wan1", "local_iface": "eth0",
               "peer_host": "198.51.100.10", "peer_port": 3785,
               "tx_interval_ms": 750, "detect_mult": 4}
    session.update(session_extra or {})
    p = tmp_path / "sbfd.json"
    p.write_text(json.dumps({
        "bind_host": "0.0.0.0", "bind_port": 3784,
        "state_file": "/run/sbfd/state.json",
        "sessions": [session],
    }))
    return p


def test_session_config_has_no_dead_up_threshold_knob():
    """`up_threshold` was parsed but never referenced by any state transition.

    A config field that silently does nothing is worse than no field: an
    operator can set it, observe no effect, and have no way to tell whether the
    value or their understanding is wrong. Flap damping is provided by
    policy.failback_hold_s and the dynamic-policy hysteresis instead.
    """
    fields = {f.name for f in dataclasses.fields(M.SessionConfig)}

    assert "up_threshold" not in fields, (
        "dead config knob is back; nothing in sbfd.py reads it")


def test_load_config_ignores_a_legacy_up_threshold_key(tmp_path):
    """Deployed configs still carry the key, so loading must not regress.

    load_config builds SessionConfig from explicit kwargs rather than **s, which
    is what makes an unknown key harmless -- pin that, because switching to **s
    would turn every already-deployed config into a TypeError at startup.
    """
    path = _cfg(tmp_path, {"up_threshold": None})

    cfg = M.load_config(str(path))

    assert len(cfg.sessions) == 1
    assert cfg.sessions[0].name == "wan1"
    assert cfg.sessions[0].tx_interval_ms == 750
    assert cfg.sessions[0].detect_mult == 4


def test_load_config_tolerates_a_legacy_up_threshold_with_a_real_value(tmp_path):
    path = _cfg(tmp_path, {"up_threshold": 6})

    cfg = M.load_config(str(path))

    assert cfg.sessions[0].session_id == 1
