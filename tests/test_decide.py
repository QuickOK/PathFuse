import sbfd_ctl as M


def make_cfg():
    return M.Config(
        wans={"wan1": M.WanCfg("wan1", 1, "Cellular"),
              "wan2": M.WanCfg("wan2", 2, "Satellite")},
        relay=M.RelayCfg("http://x/state"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(),
        policy=M.PolicyCfg(default_mode="master_backup",
                           default_master_policy="static_primary",
                           default_master_wan="wan2",
                           failback_hold_s=30),
        ui_listen="0.0.0.0:8081",
        sbfd_local_state="", runtime_state="", persist_state="", published_state="",
    )


def D(mode="master_backup", policy="static_primary", master_wan="wan2",
      eff=None, master_up_since=None, currently_active=("wan1","wan2"), now=1000.0,
      rtt=None, loss=None, dyn_master=None, dyn_cand=None, dyn_cand_since=None,
      cfg=None):
    cfg = cfg or make_cfg()
    inp = M.DecideInput(
        mode=mode,
        policy=policy,
        master_wan_cfg=master_wan,
        eff_state=eff or {"wan1":"UP","wan2":"UP"},
        rtt_ms={"wan1":50.0,"wan2":50.0} if rtt is None else rtt,
        loss_pct={"wan1":0.0,"wan2":0.0} if loss is None else loss,
        master_up_since=master_up_since,
        currently_active=set(currently_active),
        now=now,
        dynamic_master_current=dyn_master,
        dynamic_candidate=dyn_cand,
        dynamic_candidate_since=dyn_cand_since,
    )
    return M.decide(cfg, inp)


def test_full_mode_returns_both_always():
    out = D(mode="full", eff={"wan1":"DOWN","wan2":"DOWN"})
    assert out.desired_active == {"wan1","wan2"}
    assert "full redundancy" in out.reason.lower() or "both" in out.reason.lower()


def test_master_backup_steady_state_on_master():
    out = D(eff={"wan1":"UP","wan2":"UP"}, currently_active=("wan2",))
    assert out.desired_active == {"wan2"}


def test_master_down_immediate_failover():
    out = D(eff={"wan1":"UP","wan2":"DOWN"}, currently_active=("wan2",))
    assert out.desired_active == {"wan1"}
    assert "fail-over" in out.reason.lower() or "master" in out.reason.lower()


def test_master_recovers_within_hysteresis_stays_on_backup():
    out = D(eff={"wan1":"UP","wan2":"UP"}, currently_active=("wan1",),
            master_up_since=1000.0, now=1015.0)
    assert out.desired_active == {"wan1"}
    assert "hysteresis" in out.reason.lower() or "hold" in out.reason.lower()


def test_master_recovers_past_hysteresis_failback():
    out = D(eff={"wan1":"UP","wan2":"UP"}, currently_active=("wan1",),
            master_up_since=1000.0, now=1031.0)
    assert out.desired_active == {"wan2"}
    assert "fail-back" in out.reason.lower() or "failback" in out.reason.lower()


def test_both_down_falls_back_to_full_redundancy():
    out = D(eff={"wan1":"DOWN","wan2":"DOWN"}, currently_active=("wan2",))
    assert out.desired_active == {"wan1","wan2"}
    assert "both" in out.reason.lower()


def test_static_configured_uses_master_wan_cfg():
    out = D(policy="static_configured", master_wan="wan1",
            eff={"wan1":"UP","wan2":"UP"}, currently_active=("wan1",))
    assert out.desired_active == {"wan1"}


def test_static_configured_failover_to_other_when_master_down():
    out = D(policy="static_configured", master_wan="wan1",
            eff={"wan1":"DOWN","wan2":"UP"}, currently_active=("wan1",))
    assert out.desired_active == {"wan2"}


def test_dynamic_when_both_up_picks_lower_rtt():
    out = D(policy="dynamic", eff={"wan1":"UP","wan2":"UP"},
            rtt={"wan1": 30.0, "wan2": 80.0}, currently_active=("wan1",))
    assert out.desired_active == {"wan1"}


def test_dynamic_when_one_down_picks_the_up_one():
    out = D(policy="dynamic", eff={"wan1":"DOWN","wan2":"UP"},
            rtt={"wan1": 30.0, "wan2": 80.0}, currently_active=("wan2",))
    assert out.desired_active == {"wan2"}


def test_dynamic_tiebreaks_to_configured_master_when_metrics_equal():
    out = D(policy="dynamic", eff={"wan1":"UP","wan2":"UP"},
            rtt={"wan1": 50.0, "wan2": 50.0},
            loss={"wan1": 0.0, "wan2": 0.0},
            currently_active=("wan2",))
    assert out.desired_active == {"wan2"}


def test_master_up_since_set_to_now_on_first_up_seen():
    out = D(eff={"wan1":"UP","wan2":"UP"}, currently_active=("wan1",),
            master_up_since=None, now=2000.0)
    assert out.desired_active == {"wan1"}
    assert out.master_up_since == 2000.0


def test_master_up_since_cleared_when_master_down():
    out = D(eff={"wan1":"UP","wan2":"DOWN"}, currently_active=("wan2",),
            master_up_since=1000.0, now=2000.0)
    assert out.desired_active == {"wan1"}
    assert out.master_up_since is None


# -- Dynamic-mode hysteresis -------------------------------------------------
# Default PolicyCfg: rtt_margin=25ms, loss_margin=1.0pct, swap_dwell=10s.

def test_dynamic_first_tick_picks_lowest_loss_then_rtt():
    out = D(policy="dynamic",
            rtt={"wan1": 30.0, "wan2": 80.0},
            loss={"wan1": 0.0, "wan2": 0.0},
            dyn_master=None, currently_active=("wan2",))
    assert out.desired_active == {"wan1"}
    assert out.dynamic_master_current == "wan1"


def test_dynamic_first_tick_loss_outranks_rtt():
    out = D(policy="dynamic",
            rtt={"wan1": 30.0, "wan2": 80.0},
            loss={"wan1": 5.0, "wan2": 0.0},
            dyn_master=None, currently_active=("wan2",))
    assert out.desired_active == {"wan2"}
    assert out.dynamic_master_current == "wan2"


def test_dynamic_brief_rtt_spike_does_not_flap():
    # wan2 (Satellite) is current. wan1 briefly looks better but no candidate yet.
    out = D(policy="dynamic",
            rtt={"wan1": 60.0, "wan2": 200.0},
            currently_active=("wan2",), dyn_master="wan2",
            now=1000.0)
    # Sticky master holds; candidate now tracked.
    assert out.desired_active == {"wan2"}
    assert out.dynamic_candidate == "wan1"
    assert out.dynamic_candidate_since == 1000.0


def test_dynamic_candidate_clears_on_recovery():
    # Candidate was set, but next tick the spike is gone — clear candidate.
    out = D(policy="dynamic",
            rtt={"wan1": 116.0, "wan2": 56.0},
            currently_active=("wan2",), dyn_master="wan2",
            dyn_cand="wan1", dyn_cand_since=999.0, now=1003.0)
    assert out.desired_active == {"wan2"}
    assert out.dynamic_candidate is None
    assert out.dynamic_candidate_since is None


def test_dynamic_swap_after_dwell_elapsed():
    # Candidate has been winning for >= 10s — swap.
    out = D(policy="dynamic",
            rtt={"wan1": 50.0, "wan2": 200.0},
            currently_active=("wan2",), dyn_master="wan2",
            dyn_cand="wan1", dyn_cand_since=1000.0, now=1011.0)
    assert out.desired_active == {"wan1"}
    assert out.dynamic_master_current == "wan1"
    assert out.dynamic_candidate is None


def test_dynamic_swap_dwell_not_yet_elapsed():
    # Candidate winning, but only 5s of 10s dwell — do not swap.
    out = D(policy="dynamic",
            rtt={"wan1": 50.0, "wan2": 200.0},
            currently_active=("wan2",), dyn_master="wan2",
            dyn_cand="wan1", dyn_cand_since=1000.0, now=1005.0)
    assert out.desired_active == {"wan2"}
    assert out.dynamic_candidate == "wan1"
    assert out.dynamic_candidate_since == 1000.0


def test_dynamic_rtt_within_margin_does_not_create_candidate():
    # wan1 is only 10ms better — under 25ms margin. No candidate.
    out = D(policy="dynamic",
            rtt={"wan1": 50.0, "wan2": 60.0},
            currently_active=("wan2",), dyn_master="wan2")
    assert out.desired_active == {"wan2"}
    assert out.dynamic_candidate is None


def test_dynamic_master_down_swaps_immediately():
    # Sticky master goes DOWN — no dwell, immediate swap.
    out = D(policy="dynamic",
            eff={"wan1":"UP","wan2":"DOWN"},
            currently_active=("wan2",), dyn_master="wan2",
            now=1000.0)
    assert out.desired_active == {"wan1"}
    assert out.dynamic_master_current == "wan1"
    assert out.dynamic_candidate is None


def test_dynamic_loss_outranks_rtt_for_swap():
    # Current master has lower RTT but materially worse loss — swap should
    # be considered (challenger meets loss-margin), then dwell applies.
    out = D(policy="dynamic",
            rtt={"wan1": 100.0, "wan2": 50.0},
            loss={"wan1": 0.0, "wan2": 5.0},
            currently_active=("wan2",), dyn_master="wan2",
            now=1000.0)
    assert out.desired_active == {"wan2"}  # dwell not yet started
    assert out.dynamic_candidate == "wan1"
    assert out.dynamic_candidate_since == 1000.0


# -- Asymmetric dwell preservation (jitter vs. spike-recovery) ---------------
# When master is NOT the configured master, we are trying to return home; brief
# margin failures are jitter and must not reset the dwell timer. When master IS
# the configured master, brief margin failures mean the spike ended and the
# candidate should be wiped.

def test_returning_home_jitter_preserves_dwell():
    # master=wan1 (off configured), candidate=wan2 (configured_master).
    # Brief tick where wan2 RTT is too close to wan1 — jitter, not signal end.
    # candidate_since must be preserved so the dwell timer keeps running.
    out = D(policy="dynamic",
            rtt={"wan1": 88.0, "wan2": 70.0},  # gap=18ms < 25ms margin
            currently_active=("wan1",), dyn_master="wan1",
            dyn_cand="wan2", dyn_cand_since=1000.0, now=1003.0)
    assert out.desired_active == {"wan1"}
    assert out.dynamic_candidate == "wan2"
    assert out.dynamic_candidate_since == 1000.0


def test_returning_home_resumes_dwell_completion():
    # master=wan1 (off configured), candidate=wan2 (configured_master).
    # After 10s of dwell, even with one jitter tick, swap fires when next
    # margin-met tick lands and total elapsed >= dwell_s.
    out = D(policy="dynamic",
            rtt={"wan1": 90.0, "wan2": 55.0},  # gap=35ms ≥ 25ms margin
            currently_active=("wan1",), dyn_master="wan1",
            dyn_cand="wan2", dyn_cand_since=1000.0, now=1011.0)
    assert out.desired_active == {"wan2"}
    assert out.dynamic_master_current == "wan2"


def test_spike_recovery_still_clears_candidate():
    # master=wan2 (configured), candidate=wan1 (off-configured).
    # wan1 was challenger because of a Satellite spike; spike is over now.
    # candidate must be wiped (this is the original spike-recovery semantic).
    out = D(policy="dynamic",
            rtt={"wan1": 116.0, "wan2": 56.0},
            currently_active=("wan2",), dyn_master="wan2",
            dyn_cand="wan1", dyn_cand_since=999.0, now=1003.0)
    assert out.desired_active == {"wan2"}
    assert out.dynamic_candidate is None
    assert out.dynamic_candidate_since is None


def test_returning_home_eff_down_clears_candidate():
    # master=wan1, candidate=wan2 (configured), but wan2 just went eff DOWN.
    # The candidate must clear because there's nothing to dwell toward.
    out = D(policy="dynamic",
            eff={"wan1":"UP","wan2":"DOWN"},
            rtt={"wan1": 90.0, "wan2": 55.0},
            currently_active=("wan1",), dyn_master="wan1",
            dyn_cand="wan2", dyn_cand_since=1000.0, now=1003.0)
    assert out.desired_active == {"wan1"}
    assert out.dynamic_candidate is None


# -- Asymmetric pull-back to configured_master ------------------------------
# When master is off configured_master and configured is "good enough"
# (not catastrophically worse), configured is an implicit challenger and
# we start the dwell to swap back, even without strict margin advantage.

def test_off_configured_small_gap_pulls_back():
    # master=wan1 (off), configured=wan2. wan2 is slightly worse but within
    # margin. Without pull-back this would be stuck on wan1 forever.
    out = D(policy="dynamic",
            rtt={"wan1": 67.0, "wan2": 70.0},  # configured 3ms worse
            currently_active=("wan1",), dyn_master="wan1",
            now=1000.0)
    assert out.dynamic_candidate == "wan2"  # implicit challenger
    assert out.dynamic_candidate_since == 1000.0
    assert out.desired_active == {"wan1"}  # not yet swapped (dwell)


def test_off_configured_when_configured_better_pulls_back():
    # master=wan1 (off), configured=wan2 with lower RTT.
    out = D(policy="dynamic",
            rtt={"wan1": 90.0, "wan2": 55.0},
            currently_active=("wan1",), dyn_master="wan1",
            now=1000.0)
    assert out.dynamic_candidate == "wan2"
    assert out.desired_active == {"wan1"}


def test_off_configured_when_current_clearly_better_no_pullback():
    # master=wan1 (off), configured=wan2 with much higher RTT (Satellite spike).
    # current_clearly_better → no pull-back; stay on wan1 without candidate.
    out = D(policy="dynamic",
            rtt={"wan1": 50.0, "wan2": 200.0},
            currently_active=("wan1",), dyn_master="wan1",
            now=1000.0)
    assert out.dynamic_candidate is None
    assert out.desired_active == {"wan1"}


def test_off_configured_pull_back_dwell_completes():
    # master=wan1, candidate=wan2 (configured), 11s elapsed → swap back.
    out = D(policy="dynamic",
            rtt={"wan1": 67.0, "wan2": 70.0},
            currently_active=("wan1",), dyn_master="wan1",
            dyn_cand="wan2", dyn_cand_since=1000.0, now=1011.0)
    assert out.dynamic_master_current == "wan2"
    assert out.desired_active == {"wan2"}


def test_off_configured_persistent_spike_holds_candidate():
    # master=wan1, candidate=wan2 (set earlier), but configured spikes very
    # high — current_clearly_better=True → challenger=None this tick. Old
    # candidate must persist (we still want to return home eventually).
    out = D(policy="dynamic",
            rtt={"wan1": 50.0, "wan2": 250.0},
            currently_active=("wan1",), dyn_master="wan1",
            dyn_cand="wan2", dyn_cand_since=1000.0, now=1003.0)
    assert out.dynamic_candidate == "wan2"
    assert out.dynamic_candidate_since == 1000.0
    assert out.desired_active == {"wan1"}


def test_on_configured_no_implicit_challenger():
    # master=wan2 (configured). The "off-configured pull-back" logic must NOT
    # apply here. Without a margin-beating challenger, no candidate.
    out = D(policy="dynamic",
            rtt={"wan1": 65.0, "wan2": 60.0},  # wan1 5ms worse, no margin
            currently_active=("wan2",), dyn_master="wan2",
            now=1000.0)
    assert out.dynamic_candidate is None
    assert out.desired_active == {"wan2"}


def test_off_configured_loss_better_blocks_pullback():
    # master=wan1, configured=wan2. wan2 has much higher loss → current is
    # clearly better via loss; no pull-back.
    out = D(policy="dynamic",
            rtt={"wan1": 60.0, "wan2": 50.0},  # wan2 better rtt
            loss={"wan1": 0.0, "wan2": 5.0},   # but wan2 much worse loss
            currently_active=("wan1",), dyn_master="wan1",
            now=1000.0)
    assert out.dynamic_candidate is None
    assert out.desired_active == {"wan1"}
