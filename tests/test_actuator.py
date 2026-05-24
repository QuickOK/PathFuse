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
