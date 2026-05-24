"""Tests for sbfd-ctl's engarde-PBR-table action computer."""
import sbfd_ctl as M


def _cfg():
    return M.EgressCfg(engarde_table="engarde", wg_iface="wg0", default_mode="relay_vpn")


def test_warp_returns_dev_wg0():
    out = M.compute_engarde_table_action(
        egress_mode="relay_vpn", master_iface="wan2", master_gw="192.0.2.1",
        current={"via": None, "dev": "wg0"}, cfg=_cfg())
    assert out is None  # already matches


def test_warp_replaces_when_table_currently_points_at_wan():
    out = M.compute_engarde_table_action(
        egress_mode="relay_vpn", master_iface="wan2", master_gw="192.0.2.1",
        current={"via": "192.0.2.1", "dev": "wan2"}, cfg=_cfg())
    assert out == {"op": "replace", "via": None, "dev": "wg0", "table": "engarde"}


def test_relay_direct_same_as_warp_returns_dev_wg0():
    out = M.compute_engarde_table_action(
        egress_mode="relay_direct", master_iface="wan2", master_gw="192.0.2.1",
        current={"via": "192.0.2.1", "dev": "wan2"}, cfg=_cfg())
    assert out == {"op": "replace", "via": None, "dev": "wg0", "table": "engarde"}


def test_local_direct_returns_via_master_gw():
    out = M.compute_engarde_table_action(
        egress_mode="local_direct", master_iface="wan2", master_gw="192.0.2.1",
        current={"via": None, "dev": "wg0"}, cfg=_cfg())
    assert out == {"op": "replace", "via": "192.0.2.1", "dev": "wan2", "table": "engarde"}


def test_local_direct_no_op_when_already_pointed_at_master():
    out = M.compute_engarde_table_action(
        egress_mode="local_direct", master_iface="wan2", master_gw="192.0.2.1",
        current={"via": "192.0.2.1", "dev": "wan2"}, cfg=_cfg())
    assert out is None


def test_local_direct_master_change_targets_new_master():
    # Was on wan2, operator switched master to wan1.
    out = M.compute_engarde_table_action(
        egress_mode="local_direct", master_iface="wan1", master_gw="25.13.210.169",
        current={"via": "192.0.2.1", "dev": "wan2"}, cfg=_cfg())
    assert out == {"op": "replace", "via": "25.13.210.169", "dev": "wan1", "table": "engarde"}


def test_local_direct_with_unknown_gateway_refuses_to_act():
    # master_gw is None — read_wan_gateway failed (link DOWN). Don't black-hole.
    out = M.compute_engarde_table_action(
        egress_mode="local_direct", master_iface="wan2", master_gw=None,
        current={"via": "192.0.2.1", "dev": "wan2"}, cfg=_cfg())
    assert out is None


def test_unknown_egress_mode_returns_none_safely():
    out = M.compute_engarde_table_action(
        egress_mode="banana", master_iface="wan2", master_gw="192.0.2.1",
        current={"via": None, "dev": "wg0"}, cfg=_cfg())
    assert out is None


def test_apply_engarde_replace_via_and_dev_calls_correct_ip_command(monkeypatch):
    calls = []
    class _R:
        returncode = 0
        stdout = ""
        stderr = ""
    def fake_run(cmd, capture_output=False, text=False, check=False, **kw):
        calls.append(cmd)
        return _R()
    monkeypatch.setattr(M.subprocess, "run", fake_run)
    M.apply_engarde_table_action(
        {"op": "replace", "via": "192.0.2.1", "dev": "wan2", "table": "engarde"})
    assert calls == [["ip", "route", "replace", "default",
                     "via", "192.0.2.1", "dev", "wan2", "table", "engarde"]]


def test_apply_engarde_replace_dev_only_omits_via(monkeypatch):
    calls = []
    class _R:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(M.subprocess, "run",
                        lambda cmd, **kw: (calls.append(cmd) or _R()))
    M.apply_engarde_table_action(
        {"op": "replace", "via": None, "dev": "wg0", "table": "engarde"})
    assert calls == [["ip", "route", "replace", "default",
                     "dev", "wg0", "table", "engarde"]]


def test_apply_engarde_none_action_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(M.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    M.apply_engarde_table_action(None)
    assert calls == []


def test_apply_engarde_raises_on_nonzero_rc(monkeypatch):
    class _R:
        returncode = 2
        stdout = ""
        stderr = "RTNETLINK answers: No such file"
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, **kw: _R())
    import pytest
    with pytest.raises(RuntimeError, match="ip route replace failed"):
        M.apply_engarde_table_action(
            {"op": "replace", "via": None, "dev": "wg0", "table": "engarde"})


def test_read_engarde_table_default_parses_dev_only(monkeypatch):
    class _R:
        returncode = 0
        stdout = '[{"dst":"default","dev":"wg0","scope":"link","flags":[]}]'
        stderr = ""
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, **kw: _R())
    out = M.read_engarde_table_default("engarde")
    assert out == {"via": None, "dev": "wg0"}


def test_read_engarde_table_default_parses_via_dev(monkeypatch):
    class _R:
        returncode = 0
        stdout = '[{"dst":"default","gateway":"192.0.2.1","dev":"wan2","flags":[]}]'
        stderr = ""
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, **kw: _R())
    out = M.read_engarde_table_default("engarde")
    assert out == {"via": "192.0.2.1", "dev": "wan2"}


def test_read_engarde_table_default_returns_none_when_table_empty(monkeypatch):
    class _R:
        returncode = 0
        stdout = '[]'
        stderr = ""
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, **kw: _R())
    out = M.read_engarde_table_default("engarde")
    assert out is None


def test_read_engarde_table_default_returns_none_on_ip_failure(monkeypatch):
    class _R:
        returncode = 2
        stdout = ""
        stderr = "Table does not exist"
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, **kw: _R())
    out = M.read_engarde_table_default("engarde")
    assert out is None


def test_read_engarde_table_default_returns_none_on_invalid_json(monkeypatch):
    class _R:
        returncode = 0
        stdout = "not json at all"
        stderr = ""
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, **kw: _R())
    out = M.read_engarde_table_default("engarde")
    assert out is None
