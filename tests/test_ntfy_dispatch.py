import os, subprocess, textwrap
from pathlib import Path

DISPATCH = str(Path(__file__).resolve().parent.parent /
               "deploy" / "ntfy-control" / "ntfy-dispatch")


def run(msg, mid, state, extra_env=None):
    log = Path(state) / "runlog"
    env = dict(os.environ,
               NTFY_MESSAGE=msg, NTFY_ID=mid,
               DISPATCH_STATE_DIR=state,
               # a fake reboot command that appends a non-empty line per
               # invocation to a log path baked into the command string
               # itself (so the dispatcher never needs a RUN_LOG seam).
               REBOOT_CMD=f'/bin/sh -c \'echo "ran: $@" >> {log}\' _')
    env.update(extra_env or {})
    r = subprocess.run(["/bin/bash", DISPATCH], env=env,
                       capture_output=True, text=True)
    ran = log.read_text().strip().splitlines() if log.exists() else []
    return r.returncode, ran


def test_allowed_command_runs(tmp_path):
    rc, ran = run("reboot-wan1", "id1", str(tmp_path))
    assert rc == 0
    assert any("--only wan1" in line for line in ran)


def test_unknown_command_ignored(tmp_path):
    rc, ran = run("rm -rf /", "id2", str(tmp_path))
    assert ran == []          # nothing executed


def test_cycle_has_no_only_flag(tmp_path):
    rc, ran = run("reboot-cycle", "id3", str(tmp_path))
    assert ran and "--only" not in ran[-1]


def test_duplicate_id_ignored(tmp_path):
    run("reboot-wan1", "dup", str(tmp_path))
    _, ran = run("reboot-wan1", "dup", str(tmp_path))   # same id again
    assert len(ran) == 1       # second call deduped, did not run


def test_rate_limited(tmp_path):
    run("reboot-wan1", "a", str(tmp_path))
    _, ran = run("reboot-wan1", "b", str(tmp_path))     # new id, too soon
    assert len(ran) == 1       # throttled


def test_rate_limit_per_command(tmp_path):
    run("reboot-wan1", "a", str(tmp_path))
    _, ran = run("reboot-wan2", "b", str(tmp_path))     # different command, allowed
    assert any("--only wan2" in line for line in ran)
