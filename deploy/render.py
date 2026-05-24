#!/usr/bin/env python3
"""PathFuse deploy renderer (stdlib only).

Fills templates from a values file. `templates/manifest.json` maps each template to
the role(s) it applies to and its install destination. Rendering uses string.Template
(`$name` / `${name}`); a literal `$` in a template must be written `$$`. Substitution
is STRICT: a placeholder with no value raises KeyError, so an incomplete values file
fails loudly instead of shipping a half-filled unit."""
import argparse, json, sys, tempfile
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent

def load_values(path):
    return json.loads(Path(path).read_text())

def template_vars(values):
    """Flatten values for templates: scalars as-is; nested dict scalars as
    `<key>_<subkey>`; any dict/list also exposed as `<key>_json` (compact JSON)."""
    out = {}
    for k, v in values.items():
        if isinstance(v, dict):
            out[f"{k}_json"] = json.dumps(v)
            for k2, v2 in v.items():
                if not isinstance(v2, (dict, list)):
                    out[f"{k}_{k2}"] = v2
        elif isinstance(v, list):
            out[f"{k}_json"] = json.dumps(v)
        else:
            out[k] = v
    return {k: str(v) for k, v in out.items()}

def render_text(text, tvars):
    """Strict render of one template string. Raises KeyError on a missing placeholder."""
    return Template(text).substitute(tvars)

def manifest():
    return json.loads((HERE / "templates" / "manifest.json").read_text())

def sbfd_sessions(values, role):
    """Build the sbfd daemon's per-WAN sessions from values['wans'] so the daemon
    config tracks ANY number of links with their real interfaces (not a fixed pair).
    Client sessions bind to the WAN iface (SO_BINDTODEVICE) and target the relay's
    public IP; relay sessions bind to any iface and accept from any peer. The per-
    session port is bind_base + session_id (symmetric peer_port)."""
    base = int(values["ports"]["sbfd_base"])
    out = []
    for name, w in values["wans"].items():
        sid = int(w["session_id"])
        out.append({
            "session_id": sid,
            "name": name,
            "local_iface": (w["iface"] if role == "client" else None),
            "peer_host": (values["relay_public_ip"] if role == "client" else "0.0.0.0"),
            "peer_port": base + sid,
            "tx_interval_ms": 500,
            "detect_mult": 3,
        })
    return out

def render_all(values, out_dir):
    """Render every manifest template whose roles include values['role'] into out_dir
    (mirroring each entry's dest path under out_dir). Returns list of (entry, out_path)."""
    role = values["role"]
    tvars = template_vars(values)
    # Role-aware computed blocks (the sbfd daemon's sessions are generated from wans).
    tvars["sbfd_sessions_json"] = json.dumps(sbfd_sessions(values, role))
    results = []
    for entry in manifest():
        if role not in entry["roles"]:
            continue
        tpl = (HERE / "templates" / entry["template"]).read_text()
        rendered = render_text(tpl, tvars)
        out_path = Path(out_dir) / Path(entry["dest"]).relative_to("/")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
        results.append((entry, out_path))
    return results

def check(values_path):
    """Render the example values for BOTH roles to temp dirs; return True iff every
    template resolves with no missing placeholder."""
    base = load_values(values_path)
    ok = True
    for role in ("client", "relay"):
        v = dict(base); v["role"] = role
        with tempfile.TemporaryDirectory() as td:
            try:
                n = render_all(v, td)
                print(f"role={role}: rendered {len(n)} files OK")
            except KeyError as e:
                print(f"role={role}: MISSING placeholder {e}"); ok = False
    return ok

def main():
    ap = argparse.ArgumentParser(description="PathFuse deploy template renderer")
    ap.add_argument("-c", "--values", default=str(HERE / "values.example.json"))
    ap.add_argument("-o", "--out", default=str(HERE / "out"))
    ap.add_argument("--check", action="store_true",
                    help="render example values for all roles; nonzero exit if any placeholder unresolved")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check(args.values) else 1)
    values = load_values(args.values)
    for entry, p in render_all(values, Path(args.out) / values["role"]):
        print(f"{entry['template']} -> {p}")

if __name__ == "__main__":
    main()
