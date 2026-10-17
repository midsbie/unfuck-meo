#!/usr/bin/env python3
"""Keep sharing rules on IPv6 on a MEO TG789vac v2 router.

The firmware silently turns sharing rules into IPv4 rules whenever it momentarily does
not know the device's IPv6 address, breaking access from the Internet. Log in to the
router's web UI and restore IPv6 for the given devices, printing changes to stdout and
reporting failures on stderr.

Run ``meo-ipv6-fix [--dry-run] [--url URL] [--user USER] DEVICE...``.

Read defaults from MEO_URL, MEO_USER, and MEO_PASSWORD. Return status 0 on success and 1
if the router fails or cannot be reached. Only the Python standard library is used.

"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import html.parser
import http.cookiejar
import ipaddress
import os
import re
import sys
import urllib.parse
import urllib.request

DEFAULT_URL = "http://192.168.1.254"
MENU_ARGS = "be=0&l0=3&l1=4"
CFG_URL = f"/cgi/b/games/cfg/?{MENU_ARGS}"
DEVICE_PATH = "/cgi/b/_dev_/ov/"
FORM = "ServiceCfg"

# submitForm() action numbers and form field names used by the sharing configuration
# page.
ACT_APPLY = "10"
ACT_EDIT = "21"
FIELD_GAME = "37"
FIELD_DEVICE = "31"
FIELD_IP_VERSION = "39"
FIELD_PORT_START = "44"
FIELD_PORT_END = "45"
IPV4 = "4"
IPV6 = "41"

EDIT_RE = re.compile(rf"submitForm\(document\.{FORM},{ACT_EDIT},\d+,'([^']*)'")
DEVICE_LINK_RE = re.compile(rf"GoAndRemember\('{DEVICE_PATH}','([^']*)'")


class RouterError(Exception):
    """Report a rejected operation or an unexpected page from the router."""


@dataclasses.dataclass(frozen=True)
class Rule:
    """A row of the "Assigned Games & Applications" table."""

    rule_id: str
    game: str
    device: str
    ip_version: str
    protocol: str
    ports: str
    # query arguments of the device's page, if the device is known
    device_args: str | None

    @property
    def signature(self) -> tuple[str, str, str, str]:
        """Return the identity preserved when edits change the rule's id."""
        return (self.game, self.device, self.protocol, self.ports)

    def __str__(self) -> str:
        """Return the application's name, protocol, and ports for display."""
        return f"{self.game} ({self.protocol} {self.ports})"


@dataclasses.dataclass
class Option:
    """Represent an HTML select option and its explicit selection state."""

    value: str
    label: str
    selected: bool


@dataclasses.dataclass
class Form:
    """Preserve a form's fields in browser submission order."""

    # [name, value] pairs in document order, as a browser would submit them.
    fields: list[list[str]] = dataclasses.field(default_factory=list)
    selects: dict[str, list[Option]] = dataclasses.field(default_factory=dict)

    def value(self, name: str) -> str | None:
        """Return the first named field's value, or None if it is absent."""
        return next((value for field, value in self.fields if field == name), None)

    def submission(
        self, action: str, key: str, **overrides: str
    ) -> list[tuple[str, str]]:
        """Return submission fields with the action, key, and overrides set."""
        values = {"0": action, "1": key, **overrides}
        return [(name, values.get(name, value)) for name, value in self.fields]


@dataclasses.dataclass
class _Row:
    cells: list[str] = dataclasses.field(default_factory=list)
    hrefs: list[str] = dataclasses.field(default_factory=list)
    onclicks: list[str] = dataclasses.field(default_factory=list)


class _PageParser(html.parser.HTMLParser):
    """Collect a page's forms and table rows."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, Form] = {}
        self.rows: list[_Row] = []
        self._open_rows: list[_Row] = []  # innermost last, as tables nest
        self._form: Form | None = None
        # the [name, value] field of the open <select>
        self._select: list[str] | None = None
        self._option: Option | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Collect fields and row attributes from an opening HTML tag."""
        a = {name: value or "" for name, value in attrs}
        self._start_row_tag(tag, a)
        self._start_form_tag(tag, a)

    def _start_row_tag(self, tag: str, a: dict[str, str]) -> None:
        row = self._open_rows[-1] if self._open_rows else None
        if tag == "tr":
            self._open_rows.append(_Row())
        elif row and tag == "td":
            row.cells.append("")
        elif row and tag == "a" and "href" in a:
            row.hrefs.append(a["href"])
        elif row and tag == "input" and "onclick" in a:
            row.onclicks.append(a["onclick"])

    def _start_form_tag(self, tag: str, a: dict[str, str]) -> None:
        if tag == "form":
            self._form = self.forms.setdefault(a.get("name", ""), Form())
        elif self._form is None:
            return
        elif tag == "input":
            kind = a.get("type", "text").lower()
            if not a.get("name") or kind in ("button", "submit", "reset", "image"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self._form.fields.append([a["name"], a.get("value", "")])
        elif tag == "select":
            self._select = [a.get("name", ""), ""]
            self._form.fields.append(self._select)
            self._form.selects[self._select[0]] = []
        elif tag == "option" and self._select:
            self._option = Option(a.get("value", ""), "", "selected" in a)
            self._form.selects[self._select[0]].append(self._option)

    def handle_endtag(self, tag: str) -> None:
        """Finish rows and record the values of selected options."""
        if tag == "tr" and self._open_rows:
            self.rows.append(self._open_rows.pop())
        elif tag == "option":
            self._option = None
        elif tag == "select" and self._form and self._select:
            # Browsers submit the selected option, or the first one if none is.
            options = self._form.selects[self._select[0]]
            chosen = next(
                (o for o in options if o.selected), options[0] if options else None
            )
            self._select[1] = chosen.value if chosen else ""
            self._select = self._option = None
        elif tag == "form":
            self._form = None

    def handle_data(self, data: str) -> None:
        """Append text to the current option and table cell."""
        if self._option:
            self._option.label += data
        if self._open_rows and self._open_rows[-1].cells:
            self._open_rows[-1].cells[-1] += data


def _parse(page: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(page)
    parser.close()
    return parser


def _text(s: str) -> str:
    return " ".join(s.split())


def parse_form(page: str, name: str) -> Form:
    """Return the named form, raising RouterError if it is absent."""
    form = _parse(page).forms.get(name)
    if form is None:
        raise RouterError(f"unexpected page from router: no {name} form")
    return form


def parse_rules(page: str) -> list[Rule]:
    """Return sharing rules, raising RouterError for malformed rule rows."""
    rules = []
    for row in _parse(page).rows:
        ids = [m.group(1) for onclick in row.onclicks if (m := EDIT_RE.search(onclick))]
        if not ids:
            continue
        cells = [_text(cell) for cell in row.cells]
        if len(cells) < 7:
            raise RouterError(f"unexpected sharing rule row: {cells}")
        links = [m.group(1) for href in row.hrefs if (m := DEVICE_LINK_RE.search(href))]
        rules.append(
            Rule(
                rule_id=ids[0],
                game=cells[0],
                device=cells[1],
                ip_version=cells[4],
                protocol=cells[5],
                ports=cells[6],
                device_args=links[0] if links else None,
            )
        )
    return rules


def global_ipv6_addresses(page: str) -> list[ipaddress.IPv6Address]:
    """Return global IPv6 addresses found in the page's table cells."""
    addresses = []
    for row in _parse(page).rows:
        for cell in row.cells:
            try:
                address = ipaddress.IPv6Address(cell.strip())
            except ValueError:
                continue
            if address.is_global:
                addresses.append(address)
    return addresses


def edit_submission(page: str, rule: Rule) -> list[tuple[str, str]]:
    """Return edit-form fields that switch the given rule to IPv6.

    Raise RouterError unless the form matches the rule and has its device
    explicitly selected. Browsers otherwise fall back to the first device,
    which would hand the rule over to another device.
    """
    form = parse_form(page, FORM)
    selected = [o for o in form.selects.get(FIELD_DEVICE, []) if o.selected]
    ports = f"{form.value(FIELD_PORT_START)} - {form.value(FIELD_PORT_END)}"
    if (
        form.value(FIELD_GAME) != rule.game
        or [_text(o.label) for o in selected] != [rule.device]
        or ports != rule.ports
        or form.value(FIELD_IP_VERSION) != IPV4
    ):
        raise RouterError(
            f"edit form does not match {rule.device}: {rule}; left it alone"
        )
    return form.submission(ACT_APPLY, rule.rule_id, **{FIELD_IP_VERSION: IPV6})


def login_fields(page: str, user: str, password: str) -> dict[str, str]:
    """Return login fields with the router's password digest.

    Raise RouterError if the page lacks the required login parameters.
    """

    def find(pattern: str) -> str:
        m = re.search(pattern, page, re.DOTALL)
        if not m:
            raise RouterError("unexpected login page from router")
        return m.group(1)

    realm = find(r'var realm = "([^"]*)"')
    nonce = find(r'var nonce = "([^"]*)"')
    uri = find(r'var uri = "([^"]*)"')
    rn = find(r'name="authform".*?name="rn" value="([^"]*)"')
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"GET:{uri}")
    return {
        "rn": rn,
        "hidepw": _md5(f"{ha1}:{nonce}:00000001:xyz:auth:{ha2}"),
        "user": user,
    }


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class Session:
    """A logged-in session with the router's web UI."""

    def __init__(self, url: str, user: str, password: str, timeout: float = 15) -> None:
        """Create a cookie-backed session with a request timeout in seconds."""
        self._url = url.rstrip("/")
        self._user = user
        self._password = password
        self._timeout = timeout
        cookies = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        self._opener = urllib.request.build_opener(cookies)

    def _request(
        self, path: str, fields: dict[str, str] | list[tuple[str, str]] | None = None
    ) -> str:
        data = None if fields is None else urllib.parse.urlencode(fields).encode()
        with self._opener.open(
            self._url + path, data=data, timeout=self._timeout
        ) as response:
            return response.read().decode("utf-8", "replace")

    @staticmethod
    def _logged_in(page: str) -> str:
        if 'name="authform"' in page:
            raise RouterError("router rejected the login or the session expired")
        return page

    def login(self) -> None:
        """Authenticate, raising RouterError if login is rejected."""
        page = self._request("/login.lp")
        self._logged_in(
            self._request("/login.lp", login_fields(page, self._user, self._password))
        )

    def logout(self) -> None:
        """End the session, ignoring connection errors during cleanup."""
        with contextlib.suppress(OSError):
            self._request("/logout.lp")

    def get(self, path: str) -> str:
        """Fetch a page, raising RouterError if the session has expired."""
        return self._logged_in(self._request(path))

    def post(self, path: str, fields: list[tuple[str, str]]) -> str:
        """Submit fields, raising RouterError if the session has expired."""
        return self._logged_in(self._request(path, fields))


def _ipv4_rules(page: str, device: str) -> list[Rule]:
    parse_form(
        page, FORM
    )  # Make sure this is the configuration page, not an error page.
    rules = [rule for rule in parse_rules(page) if rule.device == device]
    if not rules:
        raise RouterError(f"{device}: no sharing rules for this device")
    return [rule for rule in rules if rule.ip_version == "IPv4"]


def restore_ipv6(session: Session, device: str, dry_run: bool = False) -> int:
    """Switch the device's IPv4 sharing rules to IPv6 and print each change.

    Return the number of changes, or proposed changes when dry_run is true.
    Raise RouterError if a prerequisite or verification check fails.
    """
    page = session.get(CFG_URL)
    pending = _ipv4_rules(page, device)
    if not pending:
        return 0

    # Without an IPv6 address the router would just recreate the rules as IPv4.
    args = pending[0].device_args
    if args and not global_ipv6_addresses(
        session.get(f"{DEVICE_PATH}?{MENU_ARGS}&{args}")
    ):
        raise RouterError(
            f"{device}: router knows no IPv6 address for this device; "
            f"left {len(pending)} IPv4 rule(s) alone"
        )

    if dry_run:
        for rule in pending:
            print(f"{device}: {rule}: IPv4 -> IPv6 (dry run)")
        return len(pending)

    done: set[tuple[str, str, str, str]] = set()
    while pending:
        rule = pending[0]
        if rule.signature in done:
            raise RouterError(
                f"{device}: {rule}: still IPv4 after switching it to IPv6"
            )
        done.add(rule.signature)
        edit_page = session.post(
            CFG_URL, parse_form(page, FORM).submission(ACT_EDIT, rule.rule_id)
        )
        session.post(CFG_URL, edit_submission(edit_page, rule))
        page = session.get(CFG_URL)
        versions = {
            r.ip_version for r in parse_rules(page) if r.signature == rule.signature
        }
        if "IPv6" not in versions:
            outcome = "router kept it IPv4" if versions else "rule vanished when edited"
            raise RouterError(f"{device}: {rule}: {outcome}")
        print(f"{device}: {rule}: IPv4 -> IPv6")
        pending = _ipv4_rules(page, device)
    return len(done)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI, reading sys.argv when argv is None.

    Print changes to stdout and failures to stderr. Return 0 on success or
    1 if the router fails or cannot be reached; argparse handles help and
    invalid arguments.
    """
    parser = argparse.ArgumentParser(
        description="Switch the router's IPv4 Game & Application Sharing rules of the given "
        "devices back to IPv6, printing a line per rule changed.",
        epilog="The router password is read from the MEO_PASSWORD environment variable "
        "(default: meo).",
    )
    parser.add_argument(
        "devices", nargs="+", metavar="DEVICE", help="device name, e.g. nas"
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report what would change, change nothing",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MEO_URL", DEFAULT_URL),
        help="router web UI (env MEO_URL, default: %(default)s)",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("MEO_USER", "meo"),
        help="router user (env MEO_USER, default: %(default)s)",
    )
    args = parser.parse_args(argv)

    session = Session(args.url, args.user, os.environ.get("MEO_PASSWORD", "meo"))
    failed = False
    try:
        session.login()
        try:
            for device in args.devices:
                try:
                    restore_ipv6(session, device, args.dry_run)
                except RouterError as e:
                    print(f"error: {e}", file=sys.stderr)
                    failed = True
        finally:
            session.logout()
    except (RouterError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
