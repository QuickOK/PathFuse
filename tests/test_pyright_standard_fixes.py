"""Behaviour that the fixes for pyright's `standard` findings touch, pinned around them.

sbfd's state-publishing warnings keep a broken/working state. It lived on an attribute
of write_state_file, a function, and moved to an ordinary module variable: these tests
pin what it drives, one warning when publishing breaks and one when it recovers.

The HTTP handlers' log_message overrides named their first parameter `fmt` where
BaseHTTPRequestHandler.log_message names it `format`. They now match the base, so every
call the base accepts works on them, and each still logs at debug level under its own
prefix.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import BaseServer
from typing import cast

import pytest

import sbfd
import sbfd_ctl
import udpspeeder_fec

BROKEN = (" — WAN state is not being published; readers will see no fresh state "
          "until this recovers")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _publish(cfg: sbfd.DaemonConfig, times: int = 1) -> None:
    no_sessions: list[sbfd.Session] = []
    for _ in range(times):
        # Its `sessions: list` is not typed yet: strict pyright calls the function
        # partially unknown.
        sbfd.write_state_file(cfg, no_sessions)  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture
def state_cfg(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> sbfd.DaemonConfig:
    """A config whose state file is tmp_path/run/state.json, publishing known to work.

    The broken/working state belongs to the module, so an earlier test may have left
    it broken: a first good write resets it (announcing a recovery if so), and the
    test sees the log from there on."""
    caplog.set_level(logging.WARNING)
    cfg = sbfd.DaemonConfig(state_file=str(tmp_path / "run" / "state.json"))
    _publish(cfg)
    caplog.clear()
    return cfg


def _block_the_state_dir(cfg: sbfd.DaemonConfig) -> Path:
    """Put a file where the state dir goes, so creating the dir fails."""
    state_dir = Path(cfg.state_file).parent
    shutil.rmtree(state_dir)
    state_dir.write_text("")
    return state_dir


def test_state_publishing_warns_once_when_it_breaks_and_once_when_it_recovers(
        state_cfg: sbfd.DaemonConfig, caplog: pytest.LogCaptureFixture) -> None:
    state_dir = _block_the_state_dir(state_cfg)
    _publish(state_cfg, times=3)   # ~1/s in the daemon: one warning, not one a second
    (broken,) = _warnings(caplog)
    assert broken.startswith(f"cannot create state dir {state_dir}: ")
    assert broken.endswith(BROKEN)

    caplog.clear()
    state_dir.unlink()
    _publish(state_cfg, times=3)
    assert _warnings(caplog) == [f"state publishing recovered: writing {state_cfg.state_file} "
                                 f"again"]
    assert json.loads(Path(state_cfg.state_file).read_text())["sessions"] == {}

    # The recovery reset the state: the next failure, a write this time, warns again.
    caplog.clear()
    Path(state_cfg.state_file).with_suffix(".tmp").mkdir()   # opening it to write fails
    _publish(state_cfg, times=2)
    (again,) = _warnings(caplog)
    assert again.startswith(f"state file write to {state_cfg.state_file} failed: ")
    assert again.endswith(BROKEN)


def test_both_kinds_of_failure_share_one_broken_state(
        state_cfg: sbfd.DaemonConfig, caplog: pytest.LogCaptureFixture) -> None:
    tmp = Path(state_cfg.state_file).with_suffix(".tmp")
    tmp.mkdir()                      # the write fails
    _publish(state_cfg)
    _block_the_state_dir(state_cfg)  # then, still broken, creating the dir fails instead
    _publish(state_cfg)
    (broken,) = _warnings(caplog)
    assert broken.startswith(f"state file write to {state_cfg.state_file} failed: ")

    caplog.clear()
    Path(state_cfg.state_file).parent.unlink()
    _publish(state_cfg)
    assert _warnings(caplog) == [f"state publishing recovered: writing {state_cfg.state_file} "
                                 f"again"]


def _sbfd_state_listener(tmp_path: Path) -> BaseServer | None:
    return sbfd.start_state_listener(sbfd.DaemonConfig(
        state_file=str(tmp_path / "state.json"), state_listen="127.0.0.1:0"))


def _sbfd_ctl_ui_server(tmp_path: Path) -> BaseServer | None:
    cfg = sbfd_ctl.Config(
        wans={"wan1": sbfd_ctl.WanCfg("wan1", 1, "Cellular")},
        relay=sbfd_ctl.RelayCfg("http://x"),
        engarde=sbfd_ctl.EngardeCfg("198.51.100.10", 59402),
        nft=sbfd_ctl.NftCfg(),
        policy=sbfd_ctl.PolicyCfg(),
        ui_listen="127.0.0.1:0",
        sbfd_local_state=str(tmp_path / "sbfd-state.json"),
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "published.json"),
    )
    # fec_hist is not typed yet: strict pyright calls the function partially unknown.
    return sbfd_ctl.start_ui_server(  # pyright: ignore[reportUnknownMemberType]
        cfg, threading.Event())


def _udpspeeder_fec_listener(tmp_path: Path) -> BaseServer | None:
    # Its parameters are not typed yet: strict pyright calls the function partially unknown.
    return udpspeeder_fec.start_fec_http(  # pyright: ignore[reportUnknownMemberType]
        "127.0.0.1:0", udpspeeder_fec.FecState())


@pytest.mark.parametrize("start, prefix", [
    (_sbfd_state_listener, "state-http"),
    (_sbfd_ctl_ui_server, "ui"),
    (_udpspeeder_fec_listener, "fec-http"),
], ids=["sbfd", "sbfd-ctl", "udpspeeder-fec"])
def test_log_message_takes_the_base_signature_and_logs_at_debug(
        tmp_path: Path, caplog: pytest.LogCaptureFixture,
        start: Callable[[Path], BaseServer | None], prefix: str) -> None:
    httpd = start(tmp_path)
    assert httpd is not None
    try:
        handler_class = cast("type[BaseHTTPRequestHandler]", httpd.RequestHandlerClass)
        # An instance that handles no request: log_message needs only the peer address.
        handler = handler_class.__new__(handler_class)
        handler.client_address = ("192.0.2.7", 40000)
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            handler.log_message('"%s" %s %s', "GET /x HTTP/1.1", "200", "-")  # as log_request
            handler.log_message(format="code 404, message not found")   # by the base's name
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, f'{prefix} 192.0.2.7 - "GET /x HTTP/1.1" 200 -'),
        (logging.DEBUG, f"{prefix} 192.0.2.7 - code 404, message not found")]
