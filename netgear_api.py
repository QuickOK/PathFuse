#!/usr/bin/env python3
"""netgear_api.py — Nighthawk M6 Pro admin-API client (curl subprocess).

Extracted verbatim from hotspot_watchdog.py so the cell-telemetry daemon can
share the client. hotspot_watchdog re-exports these names; its tests exercise
this code through that namespace and must keep passing untouched."""
import json
import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("netgear_api")

REBOOT_NOT_POSTED = "not_posted"
REBOOT_UNKNOWN = "unknown"
REBOOT_CONFIRMED = "confirmed"


class _Stdin:
    """Marker for a field value curl must read from stdin, never from argv."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class NetgearClient:
    """Nighthawk M6 Pro admin API via curl subprocess (API v2.0, verified live).

    curl is used (not urllib) because reaching the hotspot's admin address
    requires binding to the wan iface (SO_BINDTODEVICE) — the main routing
    table sends that prefix out the default WAN.

    The admin password is handed to curl on STDIN (`--data-urlencode name@-`),
    never in argv: argv is world-readable in /proc, so an inline `name=value`
    leaks the secret to every process table on the box. curl URL-encodes the
    stdin bytes exactly as it would an inline value and keeps the field in its
    argv position, so the request on the wire is byte-for-byte what it was.
    """

    # A /Forms/config POST answers 302 for BOTH outcomes — the M6 honours the
    # posted ok_redirect and err_redirect — so "curl exited 0" says only that
    # the hotspot replied, not that it accepted the form. Judge the Location it
    # hands back instead: an error redirect names an error target.
    _ERROR_REDIRECT_RE = re.compile(r"error|err_|fail", re.IGNORECASE)

    def __init__(self, admin_url, iface, cookie_jar, curl_bin="curl",
                 timeout_s=10.0, runner=subprocess.run):
        self.admin_url = admin_url.rstrip("/")
        self.iface = iface
        self.cookie_jar = cookie_jar
        self.curl_bin = curl_bin
        self.timeout_s = timeout_s
        self.runner = runner

    @classmethod
    def _is_error_redirect(cls, url) -> bool:
        """True iff the hotspot redirected the POST at an error target. An empty
        Location is NOT an error: absence of evidence only."""
        return bool(url) and bool(cls._ERROR_REDIRECT_RE.search(url))

    def _curl(self, extra, stdin_data=None):
        # --fail: HTTP 4xx/5xx exits 22 instead of 0, so an error page from the
        # hotspot can't masquerade as a successful POST. 3xx still passes --
        # hence _is_error_redirect() above for the POSTs that answer with one.
        argv = [self.curl_bin, "-s", "--fail", "-m", str(self.timeout_s),
                "--interface", self.iface,
                "-c", self.cookie_jar, "-b", self.cookie_jar] + extra
        kw = {"capture_output": True, "text": True,
              "timeout": self.timeout_s + 5}
        if stdin_data is not None:
            kw["input"] = stdin_data     # no trailing newline: curl would encode it
        try:
            out = self.runner(argv, **kw)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("curl failed: %s", e)
            return None
        return out.stdout if out.returncode == 0 else None

    def _reset_cookies(self):
        """Drop any existing session cookie. Without this a STALE Admin cookie
        in the jar makes the post-login model.json read back userRole=Admin even
        when the password we just posted was rejected — a failed login reported
        as a success, which then credits a reboot that never happened."""
        try:
            Path(self.cookie_jar).unlink()
        except OSError:
            pass                          # absent (the normal case) or unremovable

    def fetch_model(self):
        out = self._curl(["-L", f"{self.admin_url}/api/model.json?internalapi=1"])
        if out is None:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            log.warning("model.json response was not JSON")
            return None

    def _sec_token(self):
        m = self.fetch_model()
        try:
            return m["session"]["secToken"], m
        except (TypeError, KeyError):
            return None, m

    def _post_config(self, token, fields):
        """POST fields to /Forms/config. Returns the redirect target the hotspot
        handed back (possibly ""), or None if the request itself failed."""
        data, stdin_data = [], None
        for k, v in [("token", token)] + fields:
            if isinstance(v, _Stdin):
                data += ["--data-urlencode", f"{k}@-"]
                stdin_data = v.value      # at most one such field per request
            else:
                data += ["--data-urlencode", f"{k}={v}"]
        # -o devnull + -w: the form's response body is a redirect stub we never
        # read, so swap stdout for the one thing we DO need to judge it.
        return self._curl(data + ["-o", os.devnull, "-w", "%{redirect_url}",
                                  f"{self.admin_url}/Forms/config"],
                          stdin_data=stdin_data)

    def login(self, password) -> bool:
        self._reset_cookies()
        token, _ = self._sec_token()
        if token is None:
            return False
        redirect = self._post_config(token, [
            ("session.password", _Stdin(password)),
            ("err_redirect", "/error.json"),
            ("ok_redirect", "/success.json")])
        if redirect is None or self._is_error_redirect(redirect):
            log.warning("admin login rejected (redirect=%r)", redirect)
            return False
        m = self.fetch_model()
        role = (m or {}).get("session", {}).get("userRole")
        return role == "Admin"

    def reboot_ex(self) -> str:
        """Reboot the hotspot, distinguishing the three outcomes a caller must
        NOT confuse:

          REBOOT_NOT_POSTED — the restart never left this box. Nothing was sent,
            so the link is exactly where it was and a caller may say so.
          REBOOT_UNKNOWN    — the restart WAS sent and we cannot tell whether it
            landed. The hotspot tears the connection down as it goes down, so a
            lost answer is what a SUCCESSFUL reboot looks like from here. The
            caller must watch the link, never assume.
          REBOOT_CONFIRMED  — the hotspot acknowledged it.

        Collapsing the first two into one `False` is what let a never-sent
        reboot burn the full recovery deadline waiting for a drop that could
        never come."""
        token, _ = self._sec_token()
        if token is None:
            # We never got a token, so no restart was POSTed. Nothing was sent.
            log.warning("reboot: no security token — restart was NOT sent")
            return REBOOT_NOT_POSTED
        redirect = self._post_config(token, [("general.shutdown", "restart")])
        # Always log the redirect: this device's reboot response has never been
        # captured, so _is_error_redirect() below is a heuristic. This line is
        # what lets a real reboot teach us the true value.
        log.info("reboot POST answered with redirect=%r", redirect)
        if redirect is None:
            log.warning("reboot POST got no usable HTTP response — it may well "
                        "have landed and the hotspot may be going down now")
            return REBOOT_UNKNOWN
        if self._is_error_redirect(redirect):
            # It LOOKS like the hotspot bounced the POST at its error page. But
            # the pattern is a guess against an uncaptured response, so we do
            # NOT downgrade this to NOT_POSTED: if the guess is wrong, we would
            # be telling the caller "wan1 is untouched" while it reboots — and
            # the caller would go on to reboot the other WAN. Stay UNKNOWN and
            # let the link settle it.
            log.warning("reboot POST redirected to what looks like an error "
                        "target: %r", redirect)
            return REBOOT_UNKNOWN
        return REBOOT_CONFIRMED

    def reboot(self) -> bool:
        """True only for a CONFIRMED reboot. Kept for the reactive daemon, whose
        Executor treats anything else as a failed attempt."""
        return self.reboot_ex() == REBOOT_CONFIRMED

    @staticmethod
    def diagnostics(model) -> str:
        if not model:
            return "hotspot state unavailable"
        wwan = model.get("wwan", {})
        power = model.get("power", {})
        return (f"wwan={wwan.get('connection')}/{wwan.get('connectionText')} "
                f"bars={wwan.get('signalStrength', {}).get('bars')} "
                f"battTemp={power.get('batteryTemperature')}C "
                f"tempCritical={power.get('deviceTempCritical')} "
                f"charge={power.get('battChargeLevel')}%")
