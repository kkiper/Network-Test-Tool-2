"""The parts of the GUI that don't need a screen.

Kept separate from :mod:`nettest.gui` so they can be tested anywhere (tkinter
isn't installed on every Python) and so the window code stays about layout.
"""

import datetime
import json
import os
import re
import sys

from .checker import Status
from .collectors import OfflineCollector, SshCollector

STATUS_TEXT = {
    Status.FAIL: "FAIL",
    Status.WARN: "WARN",
    Status.PASS: "PASS",
    Status.NOT_CONFIGURED: "N/A",
}

BANNER_TEXT = {
    Status.PASS: "PASS: everything is cabled the way the drawing says",
    Status.WARN: "WARNINGS: it works, but check the yellow items",
    Status.FAIL: "FAIL: fix the red items, then run the test again",
    Status.NOT_CONFIGURED: "PASS: some items aren't set up in the config yet",
}

_SORT = {Status.FAIL: 0, Status.WARN: 1, Status.NOT_CONFIGURED: 2, Status.PASS: 3}


def app_dir():
    """Folder the tool lives in: next to the .exe when packaged, else the repo root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def configs_dir():
    return os.path.join(app_dir(), "configs")


def reports_dir(site):
    return os.path.join(app_dir(), "reports", site)


def list_sites(root=None):
    """Site folders under ``configs/`` that contain at least one config."""
    root = root or configs_dir()
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d))
                  and any(f.endswith(".json") for f in os.listdir(os.path.join(root, d))))


def counts_text(counts):
    """``{"fail": 1, "warn": 2, "pass": 9}`` -> ``1 failed, 2 warnings, 9 passed``."""
    parts = ["%d failed" % counts.get("fail", 0),
             "%d warning%s" % (counts.get("warn", 0), "" if counts.get("warn") == 1 else "s"),
             "%d passed" % counts.get("pass", 0)]
    if counts.get("not_configured"):
        parts.append("%d not set up" % counts["not_configured"])
    return ", ".join(parts)


def sorted_rows(report):
    """Port results for the results table: problems first, then config order."""
    rows = []
    for order, r in enumerate(report.ports()):
        rows.append((_SORT[r.status], order, r))
    return [r for _, _, r in sorted(rows, key=lambda t: (t[0], t[1]))]


def details_text(result):
    """The text shown under the table when a port is selected."""
    lines = ["%s on %s  ->  %s" % (result.diagram_label, result.switch_name, result.target), ""]
    for f in result.findings:
        lines.append("%s  %s" % (STATUS_TEXT[f.status], f.message))
        if f.action and f.status in (Status.WARN, Status.FAIL, Status.NOT_CONFIGURED):
            lines.append("      How to fix: %s" % f.action)
    if result.note:
        lines += ["", "Note: %s" % result.note]
    obs = result.observed or {}
    seen = []
    if obs.get("status"):
        seen.append("switch says '%s'" % obs["status"])
    if obs.get("neighbors"):
        seen.append("neighbor %s" % ", ".join(
            "%s port %s" % (d, p) if p else d for d, p in result.neighbors()))
    if obs.get("ips"):
        seen.append("IP %s" % ", ".join(obs["ips"]))
    if obs.get("macs"):
        seen.append("MAC %s" % ", ".join(obs["macs"][:3]))
    if obs.get("tx_dbm") is not None or obs.get("rx_dbm") is not None:
        seen.append("light Tx %s / Rx %s dBm" % (obs.get("tx_dbm", "-"), obs.get("rx_dbm", "-")))
    if seen:
        lines += ["", "Read from the switch: " + "; ".join(seen)]
    return "\n".join(lines)


EXTRA_LABELS = {
    "copper": "Copper test (test switch turns each bulkhead port on and off)",
    "fiber": "Fiber test (fiber to the test switch, one link at a time)",
}


def test_switch_warning(plan, extras):
    """The confirmation shown before the copper/fiber tests change test-switch ports."""
    ts = plan.test_switch
    names = " and ".join(n for n in ("copper", "fiber") if n in extras)
    lines = ["The %s test%s log in to %s and turn its ports off and on. The changes "
             "aren't saved, and every port is turned back on at the end."
             % (names, "s" if len(extras) > 1 else "", ts.name), "",
             "Before you start:",
             "  - the laptop is plugged into a spare port on %s" % ts.name]
    control = ts.copper.control if ts.copper else None
    if control:
        lines.append("  - %s %s is cabled to %s (it carries the laptop's connection)"
                     % (ts.name, control.test_switch_port, control.jack))
    if "copper" in extras:
        lines.append("  - each other bulkhead jack is cabled to its %s port" % ts.name)
    if "fiber" in extras:
        lines.append("  - both fiber pairs are cabled from the fiber bulkhead to %s" % ts.name)
    lines += ["", "Run the test?"]
    return "\n".join(lines)


class FormError(Exception):
    """Something the tech needs to fill in before the test can run."""


def build_collectors(plan, offline, addresses, username, password, enable=None, extras=()):
    """Turn what's typed in the form into collectors for :func:`run_test`.

    ``addresses`` maps switch id to an IP (live) or a file/folder (offline).
    ``extras`` are the ticked test-switch tests; they add the test switch.
    """
    if offline and extras:
        raise FormError("The copper and fiber tests turn test-switch ports on and off, so "
                        "they need live switches. Choose Live switch.")
    switches = list(plan.switches)
    if extras:
        switches.append(plan.test_switch)
    collectors = {}
    for sw in switches:
        value = (addresses.get(sw.id) or "").strip()
        if offline:
            if not value:
                raise FormError("Choose the self test file for %s." % sw.name)
            if not os.path.exists(value):
                raise FormError("Can't find the self test file for %s:\n%s" % (sw.name, value))
            collectors[sw.id] = OfflineCollector(value)
        else:
            if not value:
                raise FormError("Enter the IP address for %s." % sw.name)
            if not username:
                raise FormError("Enter the switch username.")
            if not password:
                raise FormError("Enter the switch password.")
            collectors[sw.id] = SshCollector(value, username, password, secret=enable or None)
    return collectors


SERIAL_MAX = 10
_SERIAL_OK = re.compile(r"^[A-Za-z0-9-]+$")


def serial_fits(text):
    """True if ``text`` can be typed into a serial number box (length cap)."""
    return len(text) <= SERIAL_MAX


def check_serials(plan, serials):
    """Validate the serial numbers typed in; return ``{id: serial}``.

    Serial numbers are optional; blank boxes are left out.
    """
    clean = {}
    for sw in plan.switches:
        value = (serials.get(sw.id) or "").strip().upper()
        if not value:
            continue
        if len(value) > SERIAL_MAX:
            raise FormError("The serial number for %s is longer than %d characters."
                            % (sw.name, SERIAL_MAX))
        if not _SERIAL_OK.match(value):
            raise FormError("The serial number for %s can only have letters, numbers "
                            "and dashes." % sw.name)
        clean[sw.id] = value
    return clean


DATE_FORMAT = "%Y-%m-%d"


def today():
    return datetime.date.today().strftime(DATE_FORMAT)


def check_date(text):
    """Validate the test date box (YYYY-MM-DD); blank means today."""
    text = (text or "").strip()
    if not text:
        return today()
    try:
        return datetime.datetime.strptime(text, DATE_FORMAT).strftime(DATE_FORMAT)
    except ValueError:
        raise FormError("Enter the test date as year-month-day, for example %s." % today())


# ---- remembered settings (never the password) -----------------------------

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".nettest-gui.json")


def load_settings(path=SETTINGS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings, path=SETTINGS_PATH):
    clean = {k: v for k, v in settings.items() if "password" not in k}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
    except OSError:
        pass  # remembering the last IP is a convenience, not worth an error
