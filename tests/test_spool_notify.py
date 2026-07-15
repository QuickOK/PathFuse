"""Tests for the deploy/spool-notify shell scripts (topic-override extension).

Uses the scripts' env-override hooks (NOTIFY_CONFIG, NOTIFY_AUTH,
NOTIFY_SPOOL, CURL_BIN) plus a fake curl that records its argv."""
import json
import os
import stat
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPOOL_NOTIFY = REPO / "deploy" / "spool-notify" / "spool-notify"
DRAIN = REPO / "deploy" / "spool-notify" / "spool-notify-drain"


def setup_env(tmp_path, curl_exit=0):
    tmp_path.mkdir(parents=True, exist_ok=True)   # callers pass subdirs too
    config = tmp_path / "topic-config.json"
    config.write_text(json.dumps({"ntfy_topic": "configtopic"}))
    auth = tmp_path / "auth"
    auth.write_text('NTFY_USER=u\nNTFY_PASS=p\n'
                    'NTFY_BASE=http://ntfy.invalid\n')
    spool = tmp_path / "spool"
    curl_log = tmp_path / "curl.log"
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/bash\n'
                    f'printf \'%s\\n\' "$*" >> "{curl_log}"\n'
                    f'exit {curl_exit}\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ,
               NOTIFY_CONFIG=str(config),
               NOTIFY_AUTH=str(auth),
               NOTIFY_SPOOL=str(spool),
               CURL_BIN=str(curl),
               NOTIFY_HOSTTAG="")     # opt out of hostname prefix
    env.pop("NOTIFY_TOPIC", None)
    return env, spool, curl_log


def run_notify(env, *args):
    return subprocess.run([str(SPOOL_NOTIFY), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def run_drain(env):
    return subprocess.run([str(DRAIN)], env=env,
                          capture_output=True, text=True, timeout=30)


def curl_lines(curl_log):
    return curl_log.read_text().splitlines() if curl_log.exists() else []


def test_default_topic_unchanged(tmp_path):
    env, spool, curl_log = setup_env(tmp_path)
    r = run_notify(env, "hello", "default", "body")
    assert r.returncode == 0
    lines = curl_lines(curl_log)
    assert len(lines) == 1
    assert lines[0].endswith("http://ntfy.invalid/configtopic")


def test_topic_env_overrides_config(tmp_path):
    env, spool, curl_log = setup_env(tmp_path)
    env["NOTIFY_TOPIC"] = "pathfuse"
    r = run_notify(env, "hello", "high", "body")
    assert r.returncode == 0
    assert curl_lines(curl_log)[0].endswith("http://ntfy.invalid/pathfuse")


def test_topic_env_works_without_config_file(tmp_path):
    env, spool, curl_log = setup_env(tmp_path)
    env["NOTIFY_TOPIC"] = "pathfuse"
    env["NOTIFY_CONFIG"] = str(tmp_path / "missing.json")
    r = run_notify(env, "hello", "high", "body")
    assert r.returncode == 0
    assert curl_lines(curl_log)[0].endswith("/pathfuse")
    assert not list(spool.glob("*.msg")) if spool.exists() else True


def test_spool_records_topic_on_send_failure(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    env["NOTIFY_TOPIC"] = "pathfuse"
    r = run_notify(env, "hello", "high", "body")
    assert r.returncode == 0                     # spooled counts as success
    files = list(spool.glob("*.msg"))
    assert len(files) == 1
    assert "TOPIC=pathfuse\n" in files[0].read_text()


def test_spool_has_no_topic_line_without_override(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    run_notify(env, "hello", "high", "body")
    files = list(spool.glob("*.msg"))
    assert len(files) == 1
    assert "TOPIC=" not in files[0].read_text()


def test_drain_honors_spooled_topic(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    env2 = dict(env, NOTIFY_TOPIC="pathfuse")
    run_notify(env2, "delayed alert", "high", "body")
    assert len(list(spool.glob("*.msg"))) == 1
    # Now the network is back: replace curl with a succeeding one.
    env_ok, _, curl_log_ok = setup_env(tmp_path / "ok", curl_exit=0)
    env_ok["NOTIFY_SPOOL"] = str(spool)
    r = run_drain(env_ok)
    assert r.returncode == 0
    lines = curl_lines(curl_log_ok)
    assert len(lines) == 1
    assert lines[0].endswith("/pathfuse")
    assert list(spool.glob("*.msg")) == []


def test_both_unreadable_spools_without_sending(tmp_path):
    # With no override and BOTH config and auth unreadable, the original
    # script spools (reason no-config) without ever calling curl; the patched
    # script must do the same — config is still checked before auth.
    # Intercept `logger` (same pattern as the fake curl) to assert the spool
    # reason: no-config proves CONFIG was checked before AUTH.
    env, spool, curl_log = setup_env(tmp_path)
    logger_log = tmp_path / "logger.log"
    logger = tmp_path / "logger"
    logger.write_text('#!/bin/bash\n'
                      f'printf \'%s\\n\' "$*" >> "{logger_log}"\n')
    logger.chmod(logger.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{tmp_path}:" + env["PATH"]
    env["NOTIFY_CONFIG"] = str(tmp_path / "missing.json")
    env["NOTIFY_AUTH"] = str(tmp_path / "missing.auth")
    r = run_notify(env, "hello", "high", "body")
    assert r.returncode == 0
    assert curl_lines(curl_log) == []
    assert len(list(spool.glob("*.msg"))) == 1
    logged = logger_log.read_text()
    assert "no-config" in logged
    assert "no-auth" not in logged


def test_drain_falls_back_to_config_topic(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    run_notify(env, "old-style alert", "high", "body")   # no TOPIC= header
    env_ok, _, curl_log_ok = setup_env(tmp_path / "ok", curl_exit=0)
    env_ok["NOTIFY_SPOOL"] = str(spool)
    r = run_drain(env_ok)
    assert r.returncode == 0
    assert curl_lines(curl_log_ok)[0].endswith("/configtopic")


def test_actions_header_sent(tmp_path):
    env, spool, curl_log = setup_env(tmp_path)
    env["NOTIFY_ACTIONS"] = "http, Reboot, https://x/c, body=reboot-wan1"
    r = run_notify(env, "t", "default", "m")
    assert r.returncode == 0
    lines = curl_lines(curl_log)
    assert len(lines) == 1
    assert "Actions: http, Reboot, https://x/c, body=reboot-wan1" in lines[0]


def test_no_actions_header_when_unset(tmp_path):
    env, spool, curl_log = setup_env(tmp_path)
    r = run_notify(env, "t", "default", "m")
    assert r.returncode == 0
    lines = curl_lines(curl_log)
    assert len(lines) == 1
    assert "Actions:" not in lines[0]


def test_spool_records_actions_on_send_failure(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    env["NOTIFY_ACTIONS"] = "http, Reboot, https://x/c, body=reboot-wan1"
    r = run_notify(env, "hello", "high", "body")
    assert r.returncode == 0
    files = list(spool.glob("*.msg"))
    assert len(files) == 1
    assert "ACTIONS=http, Reboot, https://x/c, body=reboot-wan1\n" in files[0].read_text()


def test_spool_has_no_actions_line_without_override(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    run_notify(env, "hello", "high", "body")
    files = list(spool.glob("*.msg"))
    assert len(files) == 1
    assert "ACTIONS=" not in files[0].read_text()


def test_drain_honors_spooled_actions(tmp_path):
    env, spool, curl_log = setup_env(tmp_path, curl_exit=22)
    spool.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    msg = spool / f"{ts}-1-1.msg"
    msg.write_text(
        f"TS={ts}\n"
        "PRIORITY=high\n"
        "TITLE=delayed alert\n"
        "ACTIONS=http, Reboot, https://x/c, body=reboot-wan1\n"
        "---\n"
        "body"
    )
    env_ok, _, curl_log_ok = setup_env(tmp_path / "ok", curl_exit=0)
    env_ok["NOTIFY_SPOOL"] = str(spool)
    r = run_drain(env_ok)
    assert r.returncode == 0
    lines = curl_lines(curl_log_ok)
    assert len(lines) == 1
    assert "Actions: http, Reboot, https://x/c, body=reboot-wan1" in lines[0]
    assert list(spool.glob("*.msg")) == []
