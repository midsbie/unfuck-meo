"""Tests against pages captured from a MEO TG789vac v2.

Captures:
  cfg_all_ipv4.html   sharing configuration with all three nas rules downgraded to IPv4
  cfg_one_ipv4.html   ... with only nas's Deluge UDP rule (id 64) on IPv4
  cfg_all_ipv6.html   ... with all nas rules on IPv6
  edit_one_ipv4.html  cfg_one_ipv4.html after clicking Edit on rule 64
  device_nas.html     nas's device overview
  login.html          the login page
"""

import contextlib
import dataclasses
import io
import ipaddress
import os
import pathlib
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import meo_ipv6_fix as m

CAPTURES = pathlib.Path(__file__).parent / "captures"
NAS_MAC = "02:00:00:00:00:01"
NAS_ARGS = f"key={NAS_MAC}&name=nas"
NAS_IPV6 = ipaddress.IPv6Address("2001:8a0:0:1:0:ff:fe00:1")
CANARY = "https://canary.example/"
CANARY_ERROR = "HTTP Error 522: Origin Connection Time-out"
CHECKING = f"canary failed: {CANARY_ERROR}; checking the router\n"


def capture(name: str) -> str:
    return (CAPTURES / name).read_text(encoding="utf-8")


def write_stamp(path, age):
    """Create or update the file with a modification time age seconds ago; return it."""
    mtime = time.time() - age
    path.touch()
    os.utime(path, (mtime, mtime))
    return path.stat().st_mtime


def replace(page: str, old: str, new: str) -> str:
    assert old in page, old
    return page.replace(old, new)


class FakeSession:
    """Serves captured pages in place of the router and records the posted forms."""

    def __init__(self, cfg_pages, edit_page="", device_page=""):
        self.cfg_pages = list(
            cfg_pages
        )  # successive pages; the last one keeps being served
        self.edit_page = edit_page
        self.device_page = device_page
        self.posts = []
        self.logged_out = False
        self.login_error = None  # raised by login() when set
        self.logout_error = None  # raised by logout() when set

    def login(self):
        if self.login_error:
            raise self.login_error

    def logout(self):
        self.logged_out = True
        if self.logout_error:
            raise self.logout_error

    def get(self, path):
        if path == m.CFG_URL:
            return (
                self.cfg_pages.pop(0) if len(self.cfg_pages) > 1 else self.cfg_pages[0]
            )
        if path == f"{m.DEVICE_PATH}?{m.MENU_ARGS}&{NAS_ARGS}":
            return self.device_page
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, fields):
        assert path == m.CFG_URL, path
        self.posts.append(dict(fields))
        return self.edit_page if dict(fields)["0"] == m.ACT_EDIT else ""


def run(session, device="nas", dry_run=False):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = m.restore_ipv6(session, device, dry_run)
    return result, out.getvalue()


class ParsingTest(unittest.TestCase):
    def test_rules(self):
        rules = m.parse_rules(capture("cfg_all_ipv4.html"))
        self.assertEqual(
            [
                (r.rule_id, r.game, r.device, r.ip_version, r.protocol, r.ports)
                for r in rules
            ],
            [
                ("59", "Deluge", "nas", "IPv4", "TCP", "59601 - 59601"),
                ("60", "Deluge", "nas", "IPv4", "UDP", "59601 - 59601"),
                ("17", "FTP Server", "192.168.1.253", "IPv4", "TCP", "21 - 21"),
                ("18", "FTP Server", "192.168.1.253", "IPv4", "TCP", "21800 - 21805"),
                ("26", "HTTPS Server", "nas", "IPv4", "TCP", "443 - 443"),
            ],
        )
        self.assertEqual(
            [r.device_args for r in rules],
            [NAS_ARGS, NAS_ARGS, None, None, NAS_ARGS],
        )
        self.assertEqual(
            [r.mac for r in rules], [NAS_MAC, NAS_MAC, None, None, NAS_MAC]
        )

    def test_form_fields_are_those_a_browser_submits(self):
        form = m.parse_form(capture("cfg_all_ipv4.html"), m.FORM)
        # Unchecked checkboxes (33, 46) are left out; selects submit their selected
        # option.
        self.assertEqual(
            [(name, value) for name, value in form.fields if name != "2"],
            [
                ("0", ""),
                ("1", ""),
                ("34", "1"),
                ("35", "1"),
                ("30", "AIM Talk"),
                ("31", "192.168.1.68"),
                ("39", "4"),
            ],
        )
        self.assertIn(m.Option("192.168.1.70", "nas", False), form.selects["31"])

    def test_submission_sets_action_and_key(self):
        form = m.parse_form(capture("cfg_all_ipv4.html"), m.FORM)
        fields = dict(form.submission(m.ACT_EDIT, "59"))
        self.assertEqual((fields["0"], fields["1"]), (m.ACT_EDIT, "59"))
        self.assertEqual(fields["2"], form.value("2"))

    def test_missing_form(self):
        with self.assertRaises(m.RouterError):
            m.parse_form("<html><body>Oops</body></html>", m.FORM)

    def test_global_ipv6_addresses(self):
        self.assertEqual(
            m.global_ipv6_addresses(capture("device_nas.html")), [NAS_IPV6]
        )

    def test_login_fields(self):
        self.assertEqual(
            m.login_fields(capture("login.html"), "meo", "meo"),
            {
                "rn": "377467226",
                "hidepw": "129197519d291af91daa5218f23e4f60",
                "user": "meo",
            },
        )


class EditSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.page = capture("edit_one_ipv4.html")
        self.rule = next(
            r for r in m.parse_rules(capture("cfg_one_ipv4.html")) if r.rule_id == "64"
        )

    def test_switches_to_ipv6(self):
        fields = dict(m.edit_submission(self.page, self.rule))
        self.assertEqual(
            (fields["0"], fields["1"], fields["39"]), (m.ACT_APPLY, "64", m.IPV6)
        )
        self.assertEqual(
            (fields["37"], fields["31"], fields["38"]), ("Deluge", "192.168.1.70", "17")
        )

    def test_refuses_when_device_is_not_selected(self):
        page = replace(self.page, " selected>nas", ">nas")
        with self.assertRaises(m.RouterError):
            m.edit_submission(page, self.rule)

    def test_refuses_another_rule(self):
        with self.assertRaises(m.RouterError):
            m.edit_submission(
                self.page, dataclasses.replace(self.rule, ports="443 - 443")
            )

    def test_refuses_rule_already_on_ipv6(self):
        page = replace(self.page, "<option value='4' selected>", "<option value='4'>")
        page = replace(page, "<option value='41' >", "<option value='41' selected>")
        with self.assertRaises(m.RouterError):
            m.edit_submission(page, self.rule)


class RestoreIpv6Test(unittest.TestCase):
    def test_nothing_to_do_is_silent(self):
        session = FakeSession([capture("cfg_all_ipv6.html")])
        self.assertEqual(run(session), (0, ""))
        self.assertEqual(session.posts, [])

    def test_switches_rule_to_ipv6(self):
        session = FakeSession(
            [capture("cfg_one_ipv4.html"), capture("cfg_all_ipv6.html")],
            capture("edit_one_ipv4.html"),
            capture("device_nas.html"),
        )
        self.assertEqual(
            run(session), (1, "nas: Deluge (UDP 59601 - 59601): IPv4 -> IPv6\n")
        )
        self.assertEqual(
            [(p["0"], p["1"], p.get("39")) for p in session.posts],
            [(m.ACT_EDIT, "64", "4"), (m.ACT_APPLY, "64", m.IPV6)],
        )

    def test_switches_rule_of_device_given_by_mac(self):
        session = FakeSession(
            [capture("cfg_one_ipv4.html"), capture("cfg_all_ipv6.html")],
            capture("edit_one_ipv4.html"),
            capture("device_nas.html"),
        )
        self.assertEqual(
            run(session, device=NAS_MAC),
            (1, f"{NAS_MAC}: Deluge (UDP 59601 - 59601): IPv4 -> IPv6\n"),
        )
        self.assertEqual(
            [(p["0"], p["1"], p.get("39")) for p in session.posts],
            [(m.ACT_EDIT, "64", "4"), (m.ACT_APPLY, "64", m.IPV6)],
        )

    def test_mac_is_case_insensitive(self):
        session = FakeSession(
            [capture("cfg_all_ipv4.html")], device_page=capture("device_nas.html")
        )
        count, _ = run(session, device=NAS_MAC.upper(), dry_run=True)
        self.assertEqual(count, 3)

    def test_hyphenated_mac_is_a_name(self):
        with self.assertRaisesRegex(m.RouterError, "no sharing rules"):
            run(
                FakeSession([capture("cfg_all_ipv6.html")]),
                device=NAS_MAC.replace(":", "-"),
            )

    def test_unknown_mac_is_an_error(self):
        with self.assertRaisesRegex(m.RouterError, "no sharing rules"):
            run(FakeSession([capture("cfg_all_ipv6.html")]), device="02:00:00:00:00:02")

    def test_dry_run_changes_nothing(self):
        session = FakeSession(
            [capture("cfg_all_ipv4.html")], device_page=capture("device_nas.html")
        )
        count, out = run(session, dry_run=True)
        self.assertEqual(count, 3)
        self.assertEqual(
            out.splitlines(),
            [
                "nas: Deluge (TCP 59601 - 59601): IPv4 -> IPv6 (dry run)",
                "nas: Deluge (UDP 59601 - 59601): IPv4 -> IPv6 (dry run)",
                "nas: HTTPS Server (TCP 443 - 443): IPv4 -> IPv6 (dry run)",
            ],
        )
        self.assertEqual(session.posts, [])

    def test_device_without_ipv6_is_left_alone(self):
        device_page = replace(capture("device_nas.html"), str(NAS_IPV6), "")
        session = FakeSession([capture("cfg_one_ipv4.html")], device_page=device_page)
        with self.assertRaisesRegex(m.RouterError, "no IPv6 address"):
            run(session)
        self.assertEqual(session.posts, [])

    def test_router_keeping_ipv4_is_an_error(self):
        session = FakeSession(
            [capture("cfg_one_ipv4.html")],
            capture("edit_one_ipv4.html"),
            capture("device_nas.html"),
        )
        with self.assertRaisesRegex(m.RouterError, "kept it IPv4"):
            run(session)
        self.assertEqual(len(session.posts), 2)

    def test_vanished_rule_is_an_error(self):
        # Each rule is on a line of its own; drop the IPv4 one.
        lines = capture("cfg_one_ipv4.html").splitlines(keepends=True)
        marker = "IPv4</td><td class='evenrow' align='left'>UDP"
        after = "".join(line for line in lines if marker not in line)
        self.assertEqual(len(after.splitlines()), len(lines) - 1)
        session = FakeSession(
            [capture("cfg_one_ipv4.html"), after],
            capture("edit_one_ipv4.html"),
            capture("device_nas.html"),
        )
        with self.assertRaisesRegex(m.RouterError, "vanished"):
            run(session)

    def test_unknown_device_is_an_error(self):
        with self.assertRaisesRegex(m.RouterError, "no sharing rules"):
            run(FakeSession([capture("cfg_all_ipv6.html")]), device="nsa")

    def test_unexpected_page_is_an_error(self):
        with self.assertRaises(m.RouterError):
            run(FakeSession(["<html><body>Busy</body></html>"]))


class MainTest(unittest.TestCase):
    def setUp(self):
        # Runs are stateless unless a test gives them a state directory.
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("STATE_DIRECTORY", None)

    def state_directory(self):
        # Set STATE_DIRECTORY to a new directory, as systemd does; return the stamp.
        state = self.enterContext(tempfile.TemporaryDirectory())
        os.environ["STATE_DIRECTORY"] = state
        return pathlib.Path(state, m.LOGIN_STAMP)

    def main(self, session, *argv):
        # Without a session, the router must be left alone.
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(m, "Session", return_value=session) as session_class,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            status = m.main(list(argv))
        if session is None:
            session_class.assert_not_called()
        return status, out.getvalue(), err.getvalue()

    def canary_main(self, error, session=None):
        # The canary fails with error, or answers if it is None.
        with mock.patch.object(m, "canary_error", return_value=error):
            return self.main(session, "--canary", CANARY, "nas")

    def test_silent_success(self):
        session = FakeSession([capture("cfg_all_ipv6.html")])
        self.assertEqual(self.main(session, "nas"), (0, "", ""))
        self.assertTrue(session.logged_out)

    def test_missing_devices_fails_before_contacting_router(self):
        err = io.StringIO()
        with (
            mock.patch.object(m, "Session") as session,
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            m.main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("DEVICE", err.getvalue())
        session.assert_not_called()

    def test_error_on_one_device_still_checks_the_others(self):
        session = FakeSession([capture("cfg_all_ipv6.html")])
        status, out, err = self.main(session, "nsa", "nas")
        self.assertEqual((status, out), (1, ""))
        self.assertEqual(err, "error: nsa: no sharing rules for this device\n")
        self.assertTrue(session.logged_out)

    def test_failed_logout_is_a_warning(self):
        session = FakeSession([capture("cfg_all_ipv6.html")])
        session.logout_error = OSError("timed out")
        self.assertEqual(
            self.main(session, "nas"), (0, "", "warning: logout failed: timed out\n")
        )

    def test_answering_canary_leaves_the_router_alone(self):
        for argv, environ in (
            (["--canary", CANARY, "nas"], {}),
            (["nas"], {"MEO_CANARY": CANARY}),
        ):
            with (
                self.subTest(argv=argv, environ=environ),
                mock.patch.dict(os.environ, environ),
                mock.patch.object(m, "canary_error", return_value=None) as canary,
                mock.patch.object(m, "Session") as session,
            ):
                self.assertEqual(m.main(argv), 0)
                canary.assert_called_once_with(CANARY)
                session.assert_not_called()

    def test_failed_canary_is_reported_and_checks_the_router(self):
        # Without a state directory, every failure checks.
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                session = FakeSession([capture("cfg_all_ipv6.html")])
                self.assertEqual(
                    self.canary_main(CANARY_ERROR, session), (0, CHECKING, "")
                )
                self.assertTrue(session.logged_out)

    def test_failed_canary_without_stamp_stamps_and_checks_the_router(self):
        stamp = self.state_directory()
        session = FakeSession([capture("cfg_all_ipv6.html")])
        self.assertEqual(self.canary_main(CANARY_ERROR, session), (0, CHECKING, ""))
        self.assertTrue(session.logged_out)
        self.assertTrue(stamp.exists())

    def test_fresh_stamp_defers_the_router_check(self):
        stamp = self.state_directory()
        last = write_stamp(stamp, 60)
        due = time.strftime("%H:%M", time.localtime(last + m.LOGIN_INTERVAL))
        self.assertEqual(
            self.canary_main(CANARY_ERROR),
            (
                0,
                f"canary failed: {CANARY_ERROR}; router check deferred until {due}\n",
                "",
            ),
        )
        # Deferring leaves the stamp alone, or checks would be deferred forever.
        self.assertEqual(stamp.stat().st_mtime, last)

    def test_stale_stamp_is_refreshed_and_checks_the_router(self):
        stamp = self.state_directory()
        last = write_stamp(stamp, m.LOGIN_INTERVAL + 1)
        session = FakeSession([capture("cfg_all_ipv6.html")])
        self.assertEqual(self.canary_main(CANARY_ERROR, session), (0, CHECKING, ""))
        self.assertTrue(session.logged_out)
        self.assertGreater(stamp.stat().st_mtime, last + m.LOGIN_INTERVAL)

    def test_stamp_in_the_future_checks_the_router(self):
        stamp = self.state_directory()
        write_stamp(stamp, -3600)
        session = FakeSession([capture("cfg_all_ipv6.html")])
        self.assertEqual(self.canary_main(CANARY_ERROR, session), (0, CHECKING, ""))
        self.assertTrue(session.logged_out)

    def test_failed_login_still_counts(self):
        stamp = self.state_directory()
        session = FakeSession([capture("cfg_all_ipv6.html")])
        session.login_error = OSError("timed out")
        self.assertEqual(
            self.canary_main(CANARY_ERROR, session),
            (1, CHECKING, "error: timed out\n"),
        )
        self.assertTrue(stamp.exists())

    def test_unusable_state_directory_leaves_the_router_alone(self):
        # An unrecorded login would lift the spacing, so the run fails instead.
        state = self.enterContext(tempfile.TemporaryDirectory())
        os.environ["STATE_DIRECTORY"] = os.path.join(state, "missing")
        status, out, err = self.canary_main(CANARY_ERROR)
        self.assertEqual((status, out), (1, ""))
        self.assertRegex(err, rf"^error: .*{m.LOGIN_STAMP}'\n$")

    def test_answering_canary_removes_the_stamp(self):
        stamp = self.state_directory()
        write_stamp(stamp, 60)
        self.assertEqual(self.canary_main(None), (0, "canary answers again\n", ""))
        self.assertFalse(stamp.exists())

    def test_answering_canary_without_stamp_is_silent(self):
        stamp = self.state_directory()
        self.assertEqual(self.canary_main(None), (0, "", ""))
        self.assertFalse(stamp.exists())

    def test_answering_canary_ignores_missing_state_directory(self):
        # No stamp to remove, so nothing needs the directory.
        state = self.enterContext(tempfile.TemporaryDirectory())
        os.environ["STATE_DIRECTORY"] = os.path.join(state, "missing")
        self.assertEqual(self.canary_main(None), (0, "", ""))


class CanaryTest(unittest.TestCase):
    def test_answer(self):
        with mock.patch.object(
            urllib.request, "urlopen", return_value=mock.MagicMock()
        ) as urlopen:
            self.assertIsNone(m.canary_error(CANARY))
        (request,), kwargs = urlopen.call_args
        self.assertEqual(
            (request.full_url, request.get_header("User-agent")), (CANARY, m.USER_AGENT)
        )
        self.assertEqual(kwargs, {"timeout": m.TIMEOUT})

    def test_errors_are_the_reason(self):
        http_error = urllib.error.HTTPError(
            CANARY, 522, "Origin Connection Time-out", {}, None
        )
        # Left open, it is freed by the garbage collector at some later point, whose
        # ResourceWarning then lands in whichever test is capturing stderr.
        self.addCleanup(http_error.close)
        errors = (
            http_error,
            urllib.error.URLError("Name or service not known"),
            TimeoutError("timed out"),
        )
        for error in errors:
            with (
                self.subTest(error=error),
                mock.patch.object(urllib.request, "urlopen", side_effect=error),
            ):
                self.assertEqual(m.canary_error(CANARY), str(error))


if __name__ == "__main__":
    unittest.main()
