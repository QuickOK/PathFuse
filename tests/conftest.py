# tests/conftest.py
import os
import socket
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class HttpServers:
    """The HTTP servers one test runs, stopped at its end together with their handlers.

    socketserver's ThreadingMixIn handles each connection in its own thread, and
    ThreadingHTTPServer makes those daemon threads, which server_close() does not
    join. A handler still busy when its test ends (one told to hang, say) runs on
    into later tests; when it then fails because its client is long gone, the
    default handle_error prints a traceback into the output of whichever test is
    running. One landed in a dead-man test's captured stderr and failed it.

    So every server started or adopted here keeps its handler threads and records a
    handler's error instead of printing it. At teardown `stopping` is set (a handler
    that hangs on purpose waits on it rather than sleeping), each server is shut down
    and closed, a connection still open is shut down so a handler blocked reading it
    sees EOF, and every handler thread is joined. A handler still running after that,
    or one that raised anything but a client disconnect, fails the test it belongs to.
    """

    JOIN_S = 5.0

    def __init__(self):
        self.stopping = threading.Event()
        self._servers = []

    def start(self, handler, server_cls=ThreadingHTTPServer):
        """Serve `handler` on an ephemeral 127.0.0.1 port until the test ends."""
        httpd = self.adopt(server_cls(("127.0.0.1", 0), handler))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def adopt(self, httpd):
        """Take over a running ThreadingMixIn server, before its first request."""
        handlers, errors = [], []

        def process_request(request, client_address):
            # ThreadingMixIn.process_request, but keeping the thread so it can be joined.
            t = threading.Thread(target=httpd.process_request_thread,
                                 args=(request, client_address), daemon=True)
            handlers.append((t, request))
            t.start()

        def handle_error(request, client_address):
            errors.append(sys.exc_info()[1])

        httpd.process_request = process_request
        httpd.handle_error = handle_error
        self._servers.append((httpd, handlers, errors))
        return httpd

    def stop_all(self):
        self.stopping.set()
        problems = []
        for httpd, handlers, errors in self._servers:
            httpd.shutdown()
            httpd.server_close()
            for t, conn in handlers:
                if t.is_alive():
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass  # its handler closed it meanwhile
            deadline = time.monotonic() + self.JOIN_S
            for t, _conn in handlers:
                t.join(max(0.0, deadline - time.monotonic()))
            port = httpd.server_address[1]
            alive = [t.name for t, _conn in handlers if t.is_alive()]
            if alive:
                problems.append(f"server on port {port}: {alive} still running "
                                f"{self.JOIN_S:g} s after it stopped")
            problems += [f"server on port {port}: a request handler raised\n"
                         + "".join(traceback.format_exception(e))
                         for e in errors if not isinstance(e, ConnectionError)]
        if problems:
            pytest.fail("\n".join(problems), pytrace=False)


@pytest.fixture
def http_servers():
    """HTTP servers whose request handlers cannot outlive the test (see HttpServers)."""
    servers = HttpServers()
    yield servers
    servers.stop_all()


# The box's own config and state. A test that opens a file under one of these reads
# what the running daemons read, and a test that writes there would overwrite their
# data if the suite ran with enough privilege.
LIVE_STATE_DIRS = ("/etc/sbfd-ctl", "/var/lib/sbfd-ctl", "/run/sbfd-ctl")


class LiveStateGuard:
    """Records each path under LIVE_STATE_DIRS that is opened while a test runs.

    It is an audit hook, so it sees every thread: a request handler or a controller
    loop working for the test counts as the test. The `open` event fires before the
    file is opened, so an open that then fails (a missing file, or a write the user
    may not make) is recorded too. Paths are made absolute but symlinks are not
    followed. An audit hook cannot be removed, so this one is installed once per
    session and records nothing between tests.
    """

    def __init__(self):
        self.opened = None   # a list while a test runs

    def audit(self, event, args):
        opened = self.opened
        if event != "open" or opened is None:
            return
        try:
            path = os.path.abspath(os.fsdecode(args[0]))
        except TypeError:   # open(fd) names no path
            return
        if any(path == d or path.startswith(d + "/") for d in LIVE_STATE_DIRS):
            opened.append(path)


@pytest.fixture(scope="session")
def live_state_guard():
    """The session's LiveStateGuard, its audit hook installed."""
    guard = LiveStateGuard()
    sys.addaudithook(guard.audit)
    return guard


@pytest.fixture(autouse=True)
def no_live_state(live_state_guard):
    """Fails a test that opened a file under the box's live config or state dirs.

    Being autouse, this is set up before the test's other function-scoped fixtures
    and torn down after them, so what they open counts too, at teardown included.
    """
    live_state_guard.opened = []
    yield
    opened, live_state_guard.opened = live_state_guard.opened, None
    if opened:
        pytest.fail("the test opened files under the box's live state dirs; "
                    "point these paths at tmp_path:\n"
                    + "\n".join(f"  {p}" for p in sorted(set(opened))), pytrace=False)
