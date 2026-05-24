"""Tests for sbfd-ctl's kernel-default-route management."""
import sbfd_ctl as M


# -- pick_default_wan --------------------------------------------------------

def P(eff, desired_active=("wan1", "wan2"), configured_master="wan2",
      dynamic_master_current=None):
    return M.pick_default_wan(
        eff_state=eff,
        desired_active=set(desired_active),
        configured_master=configured_master,
        dynamic_master_current=dynamic_master_current,
    )


def test_pick_all_down_returns_none():
    assert P({"wan1": "DOWN", "wan2": "DOWN"}) is None


def test_pick_single_up_returns_that_wan():
    assert P({"wan1": "DOWN", "wan2": "UP"}) == "wan2"


def test_pick_master_backup_singleton_active_up_uses_it():
    assert P({"wan1": "UP", "wan2": "UP"}, desired_active=("wan2",)) == "wan2"
    assert P({"wan1": "UP", "wan2": "UP"}, desired_active=("wan1",)) == "wan1"


def test_pick_master_backup_singleton_but_down_falls_back():
    # Engarde decided wan2, but wan2 is now DOWN — pick something UP.
    assert P({"wan1": "UP", "wan2": "DOWN"}, desired_active=("wan2",)) == "wan2" \
        or True
    # Actually: spec says return None / fallback? We say: skip the singleton
    # if it's DOWN and pick from all UP.
    out = P({"wan1": "UP", "wan2": "DOWN"}, desired_active=("wan2",))
    assert out == "wan1"


def test_pick_full_mode_uses_dynamic_master_when_set():
    # Both UP, both desired-active: dynamic picker says wan1.
    out = P({"wan1": "UP", "wan2": "UP"},
            desired_active=("wan1", "wan2"),
            configured_master="wan2",
            dynamic_master_current="wan1")
    assert out == "wan1"


def test_pick_full_mode_falls_back_to_configured_when_no_dynamic():
    out = P({"wan1": "UP", "wan2": "UP"},
            desired_active=("wan1", "wan2"),
            configured_master="wan2",
            dynamic_master_current=None)
    assert out == "wan2"


def test_pick_full_mode_configured_down_uses_first_up_alphabetical():
    out = P({"wan1": "UP", "wan2": "DOWN"},
            desired_active=("wan1", "wan2"),
            configured_master="wan2",
            dynamic_master_current=None)
    assert out == "wan1"


def test_pick_dynamic_master_currently_down_falls_back_to_configured():
    # Dynamic picker says wan1 but wan1 just went DOWN.
    out = P({"wan1": "DOWN", "wan2": "UP"},
            desired_active=("wan1", "wan2"),
            configured_master="wan2",
            dynamic_master_current="wan1")
    assert out == "wan2"


# -- parse_default_gateway ---------------------------------------------------

def test_parse_gateway_single_route():
    j = '[{"dst":"default","gateway":"30.224.188.121","dev":"wan1","metric":200,"flags":[]}]'
    assert M.parse_default_gateway(j) == "30.224.188.121"


def test_parse_gateway_skips_routes_without_gateway():
    # Onlink-style entries without a gateway should be skipped.
    j = '[{"dst":"default","dev":"tun0","flags":[]}, {"dst":"default","gateway":"10.0.0.1","dev":"wan1","metric":100,"flags":[]}]'
    assert M.parse_default_gateway(j) == "10.0.0.1"


def test_parse_gateway_invalid_json_returns_none():
    assert M.parse_default_gateway("not json") is None


def test_parse_gateway_empty_returns_none():
    assert M.parse_default_gateway("[]") is None


# -- parse_managed_default ---------------------------------------------------

MULTI = '''[
  {"dst":"default","gateway":"30.224.188.121","dev":"wan1","metric":50,"flags":[]},
  {"dst":"default","gateway":"30.224.188.121","dev":"wan1","protocol":"dhcp","prefsrc":"30.224.188.122","metric":200,"flags":[]},
  {"dst":"default","gateway":"192.0.2.195","dev":"wan2","protocol":"dhcp","prefsrc":"192.0.2.194","metric":100,"flags":[]}
]'''


def test_parse_managed_finds_metric_50_entry():
    assert M.parse_managed_default(MULTI, metric=50) == ("wan1", "30.224.188.121")


def test_parse_managed_returns_none_when_absent():
    j = '[{"dst":"default","gateway":"10.0.0.1","dev":"wan1","metric":200,"flags":[]}]'
    assert M.parse_managed_default(j, metric=50) is None


def test_parse_managed_handles_metric_zero_implicit():
    # `ip -j` may emit no "metric" key when metric is 0.
    j = '[{"dst":"default","gateway":"10.0.0.1","dev":"wan1","flags":[]}]'
    assert M.parse_managed_default(j, metric=0) == ("wan1", "10.0.0.1")


# -- compute_route_action ----------------------------------------------------

def test_action_none_when_desired_matches_current():
    out = M.compute_route_action(
        desired_iface="wan1", desired_gateway="10.0.0.1",
        current=("wan1", "10.0.0.1"))
    assert out is None


def test_action_replace_when_no_current():
    out = M.compute_route_action(
        desired_iface="wan1", desired_gateway="10.0.0.1",
        current=None)
    assert out == ("replace", "wan1", "10.0.0.1")


def test_action_replace_when_iface_differs():
    out = M.compute_route_action(
        desired_iface="wan1", desired_gateway="10.0.0.1",
        current=("wan2", "10.0.0.2"))
    assert out == ("replace", "wan1", "10.0.0.1")


def test_action_replace_when_gateway_differs_dhcp_renewal():
    # Same iface, but DHCP rotated the gateway IP.
    out = M.compute_route_action(
        desired_iface="wan1", desired_gateway="10.0.0.99",
        current=("wan1", "10.0.0.1"))
    assert out == ("replace", "wan1", "10.0.0.99")


def test_action_delete_when_no_desired_but_have_current():
    out = M.compute_route_action(
        desired_iface=None, desired_gateway=None,
        current=("wan1", "10.0.0.1"))
    assert out == ("delete", "wan1", "10.0.0.1")


def test_action_none_when_no_desired_and_no_current():
    out = M.compute_route_action(
        desired_iface=None, desired_gateway=None,
        current=None)
    assert out is None


def test_action_none_when_iface_known_but_gateway_unknown():
    # Safety: if we can't read a gateway for the chosen WAN, do nothing
    # (don't black-hole by removing what's there).
    out = M.compute_route_action(
        desired_iface="wan1", desired_gateway=None,
        current=("wan2", "10.0.0.2"))
    assert out is None
