import importlib.util, json, re, subprocess, sys
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

def test_template_vars_renders_booleans_as_json_not_python():
    # str(True) is "True", which is neither JSON nor systemd — a `"dry_run": $x`
    # field in a .json template would render an unparseable config. Booleans must
    # come out as true/false. (bool is a subclass of int, so order matters.)
    r = _render()
    tv = r.template_vars({"maintenance": {"enabled": False, "dry_run": True,
                                          "hour": 3}})
    assert tv["maintenance_enabled"] == "false"
    assert tv["maintenance_dry_run"] == "true"
    assert tv["maintenance_hour"] == "3"          # ints stay bare numbers


def test_rendered_client_configs_are_valid_json_with_right_types():
    # The whole maintenance-reboot feature is dead on a fresh node if sbfd-ctl's
    # config has no maintenance_reboot section (cfg.maintenance is None ->
    # published {"configured": false} -> the timer fires hourly and does
    # nothing, forever, with the UI toggle greyed out).
    import tempfile
    r = _render()
    values = json.loads((ROOT / "deploy" / "values.example.json").read_text())
    values["role"] = "client"
    with tempfile.TemporaryDirectory() as td:
        r.render_all(values, td)
        ctl = json.loads((Path(td) / "etc/sbfd-ctl/config.json").read_text())
        maint = json.loads((Path(td) / "etc/sbfd-ctl/maintenance.json").read_text())

    m = ctl["maintenance_reboot"]
    assert isinstance(m["enabled"], bool)
    assert isinstance(m["hour"], int) and not isinstance(m["hour"], bool)
    assert 0 <= m["hour"] <= 23
    assert m["window"]["path"] == "/run/sbfd-ctl/maintenance_window.json"
    # dry_run must default to TRUE: a deployed node must not reboot real WANs
    # until the operator deliberately says so.
    assert maint["dry_run"] is True
    assert maint["lock_path"] == "/run/sbfd-ctl/maintenance.lock"
    assert isinstance(maint["recovery_deadline_s"], int)


def _rendered_relay_nft():
    import tempfile
    r = _render()
    values = json.loads((ROOT / "deploy" / "values.example.json").read_text())
    values["role"] = "relay"
    with tempfile.TemporaryDirectory() as td:
        r.render_all(values, td)
        return (Path(td) / "etc/nftables.d/pathfuse.nft").read_text(), values


def test_relay_nft_dropin_never_auto_inserts_an_ssh_accept():
    """The drop-in must not weaken an operator-managed chain it cannot read.

    An overlay SSH exemption has to sit BEFORE the port-22 rate limiter (or it
    is never reached once the limit trips) but AFTER any access controls -- a
    source-address restriction like `ip saddr != <admin> tcp dport 22 drop`. A
    drop-in knows neither position, and `insert` puts the rule at the absolute
    head, ahead of every control: an unconditional accept there lets ANY overlay
    host reach sshd, defeating the operator's restriction.

    Since correct placement is operator-specific, the drop-in ships the guidance
    and no active rule. Greptile flagged the bypass on PR #9; T-Rex reproduced
    it against a chain with a source restriction.
    """
    nft, _values = _rendered_relay_nft()

    # Matched as a whole token, and tcp-specific: a substring test for
    # "dport 22" also catches `udp dport 22` and `tcp dport 220`.
    active = [rule for rule in nft.splitlines()
              if re.search(r"\btcp dport 22\b", rule)
              and not rule.lstrip().startswith("#")]

    assert active == [], (
        "drop-in must not auto-insert a port-22 rule -- it cannot know where "
        f"the operator's access controls sit. Got: {active}")


def test_relay_nft_dropin_documents_the_ssh_rate_limit_lockout():
    """Removing the rule must not lose the knowledge that cost us the incident.

    An operator hitting the lockout sees a silent hang, not a refusal, and
    retrying sustains it -- so the drop-in has to explain the failure mode and
    give the handle-based command that places the rule correctly.
    """
    nft, values = _rendered_relay_nft()
    iface = values["overlay_iface"]

    # Join shell continuations and strip comment markers first: the recipe
    # legitimately spans lines, and the test should not dictate wrapping.
    prose = re.sub(r"\\\s*\n\s*#?\s*", " ", nft)
    prose = re.sub(r"^\s*#\s?", "", prose, flags=re.MULTILINE)

    # Pin the two load-bearing concepts, not the prose: the rule form an
    # operator greps for, and the symptom that makes this hard to recognise.
    # A bare `"rate" in nft` would survive deleting the whole rationale.
    assert re.search(r"\blimit rate\b", prose), (
        "should name the actual rule form (`limit rate`) so it is greppable")
    assert re.search(r"silent hang", prose, re.I), (
        "should name the symptom -- it presents as a hang, not a refusal")

    # And the command itself, by shape: a bare `handle`+`dport 22` match would
    # accept prose that merely mentions both without giving a usable command.
    assert re.search(
        r"nft insert rule\b[^\n]*\bhandle\b[^\n]*"
        rf'iifname "{re.escape(iface)}"[^\n]*\btcp dport 22\b[^\n]*\baccept\b',
        prose), "no usable handle-based `nft insert rule` command documented"


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


UNITS = ROOT / "deploy" / "templates" / "systemd"


def test_every_unit_sharing_run_sbfd_ctl_preserves_it():
    """RuntimeDirectoryPreserve is PER-UNIT, and /run/sbfd-ctl is SHARED.

    systemd deletes a RuntimeDirectory when the unit that declares it stops —
    even if another unit that declares the same one is still running. So a unit
    that names RuntimeDirectory=sbfd-ctl without Preserve=yes wipes the whole
    directory on every `systemctl restart` of ITSELF, taking three live things
    with it:

      runtime.json            the operator's WAN-mode overlay — its loss
                              silently reverts the mode to policy.default_mode.
      maintenance_window.json alert suppression, killed mid-window.
      maintenance.lock        the worst. flock is PER-INODE: a second
                              maintenance run does not find the lock missing and
                              wait, it CREATES A FRESH INODE at the same path and
                              locks that. Both runs then believe they hold the
                              lock, and the mutual exclusion that stops two runs
                              from taking both WANs down at once is gone.

    Fixing sbfd-ctl.service alone does not cover the others. Every unit that
    declares the directory must preserve it."""
    sharers = []
    for tmpl in sorted(UNITS.glob("*.service.tmpl")):
        # real directives only: maintenance-reboot.service.tmpl DISCUSSES
        # RuntimeDirectory=sbfd-ctl at length in a comment explaining why it
        # must not declare one.
        directives = [ln.strip() for ln in tmpl.read_text().splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")]
        if "RuntimeDirectory=sbfd-ctl" not in directives:
            continue
        sharers.append(tmpl.name)
        assert "RuntimeDirectoryPreserve=yes" in directives, (
            f"{tmpl.name} declares RuntimeDirectory=sbfd-ctl but does not "
            f"preserve it — restarting it would wipe /run/sbfd-ctl out from "
            f"under sbfd-ctl.service (WAN overlay, maintenance window, run lock)")
    # the two units known to share it; a new one must come here deliberately
    assert sharers == ["environ-ctl.service.tmpl", "sbfd-ctl.service.tmpl"]
