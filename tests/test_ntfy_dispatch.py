import os, shlex, subprocess
from pathlib import Path

DISPATCH = str(Path(__file__).resolve().parent.parent /
               "deploy" / "ntfy-control" / "ntfy-dispatch")


def run(msg, mid, state, extra_env=None):
    log = Path(state) / "runlog"
    # A fake reboot command that appends a non-empty line per invocation to a
    # log path baked into the command string itself (so the dispatcher never
    # needs a RUN_LOG seam). Shell-quote the path and the inner command so a
    # tmp path with shell metacharacters can't corrupt the constructed
    # REBOOT_CMD string.
    command = f'echo "ran: $@" >> {shlex.quote(str(log))}'
    env = dict(os.environ,
               NTFY_MESSAGE=msg, NTFY_ID=mid,
               DISPATCH_STATE_DIR=state,
               REBOOT_CMD=f"/bin/sh -c {shlex.quote(command)} _")
    env.update(extra_env or {})
    r = subprocess.run(["/bin/bash", DISPATCH], env=env,
                       capture_output=True, text=True)
    ran = log.read_text().strip().splitlines() if log.exists() else []
    return r.returncode, ran


def test_allowed_command_runs(tmp_path):
    rc, ran = run("reboot-wan1", "id1", str(tmp_path))
    assert rc == 0
    assert ran == ["ran: --only wan1"]


def test_unknown_command_ignored(tmp_path):
    rc, ran = run("rm -rf /", "id2", str(tmp_path))
    assert rc == 0            # fail-safe: the dispatcher always exits 0
    assert ran == []          # nothing executed


def test_cycle_has_no_only_flag(tmp_path):
    rc, ran = run("reboot-cycle", "id3", str(tmp_path))
    assert len(ran) == 1              # exactly one recorded invocation
    assert "--only" not in ran[0]     # full cycle: no --only flag


def test_duplicate_id_ignored(tmp_path):
    # Disable the rate-limit (0-second window: now-last < 0 is always false,
    # so nothing is ever throttled) so the ONLY thing that can block the
    # second same-id call is the seen-${MID} dedupe marker. Without this,
    # the default 1200 s rate-limit would block the repeat regardless of
    # dedupe, making the test vacuous.
    norate = {"RATE_LIMIT_S": "0"}
    _, first = run("reboot-wan1", "dup", str(tmp_path), extra_env=norate)
    assert first == ["ran: --only wan1"]                 # first call ran
    _, ran = run("reboot-wan1", "dup", str(tmp_path), extra_env=norate)  # same id again
    assert ran == first        # second call deduped: log unchanged


def test_rate_limited(tmp_path):
    # Pin the rate-limit explicitly so an inherited os.environ["RATE_LIMIT_S"]
    # can't perturb the test.
    rl = {"RATE_LIMIT_S": "1200"}
    _, first = run("reboot-wan1", "a", str(tmp_path), extra_env=rl)
    assert first == ["ran: --only wan1"]                 # first call ran
    _, ran = run("reboot-wan1", "b", str(tmp_path), extra_env=rl)  # new id, too soon
    assert ran == first        # second call throttled: log unchanged


def test_rate_limit_per_command(tmp_path):
    _, first = run("reboot-wan1", "a", str(tmp_path))
    assert first == ["ran: --only wan1"]                 # first call ran
    _, ran = run("reboot-wan2", "b", str(tmp_path))      # different command, allowed
    assert ran == first + ["ran: --only wan2"]           # wan2 leg ran too
