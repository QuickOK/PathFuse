import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def test_wizard_check_runs_and_names_scripts():
    res = subprocess.run(["bash", str(ROOT/"deploy"/"wizard.sh"), "--check"], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    for s in ("detect-os", "fetch-deps", "gen-secrets", "install", "healthcheck"):
        assert s in res.stdout, f"wizard --check should name {s}"

def test_wizard_help_exits_zero():
    res = subprocess.run(["bash", str(ROOT/"deploy"/"wizard.sh"), "--help"], capture_output=True, text=True)
    assert res.returncode == 0 and "wizard" in res.stdout.lower()
