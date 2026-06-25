import sbfd_ctl as M


def cfg():
    return M.Config(
        wans={"wan1": M.WanCfg("wan1", 1, "T-Mo"),
              "wan2": M.WanCfg("wan2", 2, "Satellite")},
        relay=M.RelayCfg("http://x"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(table="sbfd_ctl", family="inet"),
        policy=M.PolicyCfg(),
        ui_listen="", sbfd_local_state="", runtime_state="",
        persist_state="", published_state="",
    )


def test_init_table_emits_create_and_chain():
    lines = M.compute_nft_init(cfg())
    text = "\n".join(lines)
    assert "create table inet sbfd_ctl" in text
    assert "create chain inet sbfd_ctl egress_filter" in text
    assert "type filter hook output priority 0" in text


def test_diff_full_redundancy_when_no_drops_present():
    actions = M.compute_nft_diff(cfg(), desired_active={"wan1","wan2"}, current_drops=set())
    assert actions == []


def test_diff_full_redundancy_when_drops_present_removes_them():
    actions = M.compute_nft_diff(cfg(), desired_active={"wan1","wan2"}, current_drops={"wan1"})
    assert any(a.startswith("delete rule") and "wan1" in a for a in actions)


def test_diff_master_only_starts_from_full():
    actions = M.compute_nft_diff(cfg(), desired_active={"wan2"}, current_drops=set())
    assert len(actions) == 1
    line = actions[0]
    assert "add rule" in line
    assert "oifname wan1" in line
    assert "ip daddr 198.51.100.10" in line
    assert "udp dport 59402" in line
    assert "drop" in line


def test_diff_master_only_failover_swaps_drop():
    actions = M.compute_nft_diff(cfg(), desired_active={"wan1"}, current_drops={"wan1"})
    assert any(a.startswith("delete rule") and "wan1" in a for a in actions)
    assert any(a.startswith("add rule") and "wan2" in a for a in actions)


def test_diff_idempotent_on_steady_state():
    actions = M.compute_nft_diff(cfg(), desired_active={"wan2"}, current_drops={"wan1"})
    assert actions == []


class _FakeProc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_apply_nft_diff_delete_of_absent_rule_is_noop(monkeypatch):
    # Greptile P1: when unblocking a WAN whose drop rule is already gone
    # (nft flush / restart / external change), reconciliation must converge,
    # not raise a RuntimeError every 500ms tick.
    line = ("delete rule inet sbfd_ctl egress_filter oifname wan1 "
            "ip daddr 198.51.100.10 udp dport 59402 drop")

    def fake_run(argv, **kwargs):
        # batch `nft -f -` fails (rule absent), per-line delete also fails.
        return _FakeProc(1, stderr="Error: No such file or directory; no such rule")

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    # rule genuinely absent -> handle lookup finds nothing
    monkeypatch.setattr(M, "_find_drop_handle", lambda cfg, wan: None)

    # Must NOT raise: the desired end-state (rule gone) is already achieved.
    M.apply_nft_diff(cfg(), [line])


def test_apply_nft_diff_failing_add_still_raises(monkeypatch):
    # The no-op convergence must only cover absent deletes; a genuinely
    # failing add (e.g. bad syntax / table missing) must still surface.
    line = ("add rule inet sbfd_ctl egress_filter oifname wan1 "
            "ip daddr 198.51.100.10 udp dport 59402 drop")

    monkeypatch.setattr(M.subprocess, "run",
                        lambda argv, **kw: _FakeProc(1, stderr="could not process rule"))
    import pytest
    with pytest.raises(RuntimeError):
        M.apply_nft_diff(cfg(), [line])


def test_apply_nft_diff_delete_with_present_handle_failing_raises(monkeypatch):
    # If the rule IS present (handle found) but delete-by-handle still fails,
    # that's a real error, not convergence.
    line = ("delete rule inet sbfd_ctl egress_filter oifname wan1 "
            "ip daddr 198.51.100.10 udp dport 59402 drop")

    monkeypatch.setattr(M.subprocess, "run",
                        lambda argv, **kw: _FakeProc(1, stderr="no such rule"))
    monkeypatch.setattr(M, "_find_drop_handle", lambda cfg, wan: 7)
    import pytest
    with pytest.raises(RuntimeError):
        M.apply_nft_diff(cfg(), [line])
