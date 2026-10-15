"""Run a test end to end: collect from each switch, check, build a report.

This is the one function a GUI needs::

    from nettest import load_test, run_test, SshCollector

    plan = load_test("configs/standard-build/switch1.json")
    report = run_test(plan, {"switch1": SshCollector("192.168.1.1", "admin", "pw")},
                      on_progress=status_bar.set_text)
    for port in report.ports():
        print(port.status.value, port.diagram_label, port.summary)
"""

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass, field

from .checker import Status, SwitchChecker, worst
from .collectors import OPTIONAL_COMMANDS, CollectionError


@dataclass
class SwitchReport:
    id: str
    name: str
    serial: str = None
    source: str = None
    hostname: str = None
    error: str = None
    results: list = field(default_factory=list)
    unavailable: dict = field(default_factory=dict)

    @property
    def status(self):
        if self.error:
            return Status.FAIL
        return worst(r.status for r in self.results)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "serial_number": self.serial,
            "source": self.source,
            "hostname": self.hostname, "error": self.error,
            "status": self.status.value,
            "commands_unavailable": self.unavailable,
            "ports": [r.to_dict() for r in self.results],
        }


@dataclass
class TestReport:
    test_name: str
    mode: str
    config_path: str
    started_at: str
    finished_at: str = None
    test_date: str = None
    switches: list = field(default_factory=list)

    @property
    def status(self):
        return worst(s.status for s in self.switches)

    def ports(self):
        """Every port result across all switches, in config order."""
        return [r for s in self.switches for r in s.results]

    def counts(self):
        c = {s.value: 0 for s in Status}
        for r in self.ports():
            c[r.status.value] += 1
        return c

    def to_dict(self):
        return {
            "test_name": self.test_name, "mode": self.mode,
            "config_path": self.config_path,
            "test_date": self.test_date,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "status": self.status.value, "counts": self.counts(),
            "switches": [s.to_dict() for s in self.switches],
        }

    def to_text(self):
        """A plain-text report a tech can read or print."""
        icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "not_configured": "N/A "}
        lines = ["Network test: %s" % self.test_name,
                 "Test date:    %s" % (self.test_date or self.started_at[:10]),
                 "Run at:       %s" % self.started_at,
                 "Result:       %s" % self.status.value.upper().replace("_", " "),
                 ""]
        for sw in self.switches:
            head = "%s%s" % (sw.name, " (%s)" % sw.hostname if sw.hostname else "")
            lines += [head, "-" * len(head)]
            if sw.serial:
                lines.append("  Serial number: %s" % sw.serial)
            if sw.error:
                lines += ["  FAIL  Could not test this switch: %s" % sw.error, ""]
                continue
            for cmd, why in sw.unavailable.items():
                lines.append("  NOTE  '%s' was not available (%s)" % (cmd, why))
            for r in sw.results:
                lines.append("  %s  %-8s -> %-30s %-15s %s" % (
                    icon[r.status.value], r.diagram_label, r.target, r.ip or "-", r.summary))
                if r.status in (Status.WARN, Status.FAIL):
                    for f in r.findings:
                        if f.status in (Status.WARN, Status.FAIL):
                            if f.message != r.summary:
                                lines.append("          - %s" % f.message)
                            if f.action:
                                lines.append("          Fix: %s" % f.action)
            lines.append("")
        c = self.counts()
        lines.append("%d passed, %d warnings, %d failed, %d not configured"
                     % (c["pass"], c["warn"], c["fail"], c["not_configured"]))
        return "\n".join(lines)

    def save(self, folder, site=None):
        """Write ``<site>_<test>_<timestamp>.json`` and ``.txt`` into ``folder``.

        Returns the two paths.
        """
        os.makedirs(folder, exist_ok=True)
        stamp = (self.started_at or "").replace(":", "").replace("-", "")[:15]
        serials = "-".join(s.serial for s in self.switches if s.serial)
        base = "_".join(_slug(p) for p in (site, self.test_name, serials, stamp) if p)
        json_path = os.path.join(folder, base + ".json")
        txt_path = os.path.join(folder, base + ".txt")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(self.to_text() + "\n")
        return json_path, txt_path


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def _now():
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def run_test(plan, collectors, on_progress=None, serials=None, test_date=None, extras=()):
    """Collect from every switch in ``plan`` and check it.

    ``collectors`` maps switch id (``"switch1"``) to an object with a
    ``collect(on_progress)`` method, i.e. :class:`~nettest.SshCollector` or
    :class:`~nettest.OfflineCollector`. ``on_progress`` gets short status
    strings while the test runs. A switch that can't be reached is reported
    as an error on that switch rather than raising. ``serials`` optionally
    maps switch id to the serial number the tech typed in; it is only
    recorded in the report, as is ``test_date`` (defaults to today).

    ``extras`` picks optional tests that use the plan's external test switch:
    ``"copper"`` and/or ``"fiber"`` (see :mod:`nettest.testswitch`). They run
    after the normal checks, turn test-switch ports off and on, and need a
    live collector for the test switch under its id in ``collectors``.
    """
    say = on_progress or (lambda msg: None)
    report = TestReport(test_name=plan.name, mode=plan.mode, config_path=plan.path,
                        started_at=_now())
    report.test_date = test_date or report.started_at[:10]

    snapshots = {}
    for sw in plan.switches:
        sr = SwitchReport(id=sw.id, name=sw.name, serial=(serials or {}).get(sw.id) or None)
        report.switches.append(sr)
        collector = collectors.get(sw.id)
        if collector is None:
            sr.error = "No connection details were given for %s." % sw.name
            continue
        sr.source = collector.describe()
        try:
            snap = collector.collect(on_progress=say)
        except CollectionError as e:
            sr.error = str(e)
            say("%s: %s" % (sw.name, e))
            continue
        snapshots[sw.id] = snap
        sr.hostname = snap.hostname
        sr.unavailable = {k: v for k, v in snap.unavailable.items()
                          if k not in OPTIONAL_COMMANDS}

    hostnames = {sid: s.hostname for sid, s in snapshots.items() if s.hostname}
    for sw, sr in zip(plan.switches, report.switches):
        if sw.id not in snapshots:
            continue
        say("Checking %s..." % sw.name)
        try:
            sr.results = SwitchChecker(sw, snapshots[sw.id], hostnames).run()
        except ValueError as e:
            sr.error = str(e)

    if extras:
        from .testswitch import run_test_switch_tests
        run_test_switch_tests(plan, collectors, extras, report, on_progress=say)

    report.finished_at = _now()
    say("Done: %s" % report.status.value.upper())
    return report
