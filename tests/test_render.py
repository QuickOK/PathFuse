import importlib.util, json, subprocess, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / "deploy" / "render.py"

def _render():
    spec = importlib.util.spec_from_file_location("pf_render", RENDER)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_template_vars_flattens_scalars_and_nested():
    r = _render()
    tv = r.template_vars({"role": "client", "relay_public_ip": "198.51.100.10",
                          "wg": {"listen_port": 51820, "mtu": 1280},
                          "wans": {"wan1": {"iface": "eth0", "session_id": 1}}})
    assert tv["role"] == "client"
    assert tv["relay_public_ip"] == "198.51.100.10"
    assert tv["wg_listen_port"] == "51820"
    assert tv["wg_mtu"] == "1280"
    assert json.loads(tv["wans_json"]) == {"wan1": {"iface": "eth0", "session_id": 1}}

def test_render_text_strict_missing_placeholder_raises():
    r = _render()
    with pytest.raises(KeyError):
        r.render_text("hello $missing", {"present": "1"})

def test_render_text_escaped_dollar_is_literal():
    r = _render()
    assert r.render_text("k=$${UDPSPEEDER_KEY} v=$x", {"x": "7"}) == "k=${UDPSPEEDER_KEY} v=7"

def test_render_check_passes_for_all_roles():
    res = subprocess.run([sys.executable, str(RENDER), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, "render --check failed:\n" + res.stdout + res.stderr
    assert "client: rendered" in res.stdout.replace("role=", "")
    assert "relay: rendered" in res.stdout.replace("role=", "")


def test_sbfd_sessions_client_uses_iface_and_relay_ip():
    r = _render()
    v = {"role": "client", "relay_public_ip": "198.51.100.10", "ports": {"sbfd_base": 3784},
         "wans": {"wan1": {"iface": "eth0", "session_id": 1}, "fiveg": {"iface": "wwan0", "session_id": 2}}}
    s = r.sbfd_sessions(v, "client")
    assert {x["name"]: x["local_iface"] for x in s} == {"wan1": "eth0", "fiveg": "wwan0"}
    assert all(x["peer_host"] == "198.51.100.10" for x in s)
    assert {x["session_id"]: x["peer_port"] for x in s} == {1: 3785, 2: 3786}


def test_sbfd_sessions_server_null_iface_any_peer():
    r = _render()
    v = {"role": "relay", "relay_public_ip": "198.51.100.10", "ports": {"sbfd_base": 3784},
         "wans": {"wan1": {"iface": "eth0", "session_id": 1}}}
    s = r.sbfd_sessions(v, "relay")
    assert s[0]["local_iface"] is None and s[0]["peer_host"] == "0.0.0.0"


def test_sbfd_sessions_count_agnostic_three_wans():
    r = _render()
    v = {"role": "client", "relay_public_ip": "198.51.100.10", "ports": {"sbfd_base": 3784},
         "wans": {"a": {"iface": "e0", "session_id": 1}, "b": {"iface": "e1", "session_id": 2},
                  "c": {"iface": "e2", "session_id": 3}}}
    assert len(r.sbfd_sessions(v, "client")) == 3
