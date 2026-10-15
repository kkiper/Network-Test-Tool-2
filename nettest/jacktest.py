"""Bulkhead jack test: the test switch turns its ports on and off, one at a time.

The laptop logs in to Switch 1 and to the external test switch. Every
test-switch port cabled to a bulkhead jack is turned off. Then, for each
jack, the tool turns that one test-switch port on, waits for a link, reads
``show interfaces status`` on Switch 1, and turns the port off again. A jack
passes when exactly the expected Switch 1 port comes up (at the expected
speed and duplex) and goes down again.

The jack marked ``control`` carries the laptop's connection to Switch 1, so it
stays on for the whole test and is tested last: a jack that already passed is
turned on to carry the connection while the control port is turned off and on.

Changes on the test switch are never saved (no ``write memory``), and the
control port is always turned back on before the test ends.
"""

import time

from .checker import Finding, PortResult, Status
from .collectors import CollectionError
from .engine import SwitchReport, TestReport, _now
from .interfaces import drawing_label
from .parsers import parse_interfaces_status

STATUS_COMMAND = "show interfaces status"
# Before the control port is turned off, give the helper jack's link time to
# finish spanning tree's listening/learning (2 x 15 s forward delay) on Switch 1,
# so the laptop's connection has a forwarding path to move to.
SETTLE_SECONDS = 32


class _Lab:
    """The two open logins plus helpers to read and change port state."""

    def __init__(self, switch, test_switch, sleep, clock, poll):
        self.switch = switch
        self.test_switch = test_switch
        self.sleep = sleep
        self.clock = clock
        self.poll = poll

    def status(self):
        return parse_interfaces_status(self.switch.run(STATUS_COMMAND))

    def set_ports(self, ports, on):
        lines = []
        for p in ports:
            lines += ["interface %s" % p, "no shutdown" if on else "shutdown"]
        if lines:
            self.test_switch.configure(lines)

    def wait(self, done, timeout):
        """Read Switch 1's port status until ``done(status)`` or ``timeout`` seconds."""
        end = self.clock() + timeout
        while True:
            st = self.status()
            if done(st) or self.clock() >= end:
                return st
            self.sleep(self.poll)


def _up(status):
    return {p for p, row in status.items() if row["status"] == "connected"}


def _seen(row):
    if not row:
        return {}
    return {"status": row["status"], "speed": row.get("speed_raw"),
            "duplex": row.get("duplex_raw")}


def run_jack_test(plan, collectors, on_progress=None, serials=None, test_date=None,
                  sleep=time.sleep, clock=time.monotonic, poll_seconds=1.0):
    """Run a ``jack_test`` plan. Same arguments and result as :func:`run_test`.

    ``collectors`` must be live (:class:`~nettest.SshCollector`) for both the
    switch and the test switch, since the test changes test-switch ports.
    """
    say = on_progress or (lambda msg: None)
    jt = plan.jack_test
    report = TestReport(test_name=plan.name, mode=plan.mode, config_path=plan.path,
                        started_at=_now())
    report.test_date = test_date or report.started_at[:10]
    reports = {}
    for sw in plan.switches:
        reports[sw.id] = SwitchReport(id=sw.id, name=sw.name,
                                      serial=(serials or {}).get(sw.id) or None)
        report.switches.append(reports[sw.id])
    sw_report = reports[jt.switch_id]
    names = {sw.id: sw.name for sw in plan.switches}

    sessions = {}
    try:
        for sid in (jt.test_switch_id, jt.switch_id):
            sr, collector = reports[sid], collectors.get(sid)
            if collector is None:
                sr.error = "No connection details were given for %s." % names[sid]
                continue
            sr.source = collector.describe()
            if not hasattr(collector, "open"):
                sr.error = ("This test turns test-switch ports on and off, so it needs "
                            "live switches. It can't run from a self test file.")
                continue
            try:
                sessions[sid] = collector.open(on_progress=say)
            except CollectionError as e:
                sr.error = str(e)
                say("%s: %s" % (names[sid], e))
        if len(sessions) == 2:
            lab = _Lab(sessions[jt.switch_id], sessions[jt.test_switch_id],
                       sleep, clock, poll_seconds)
            try:
                sw_report.results = _JackRun(jt, lab, names, say).run()
            except Exception as e:  # a dropped login mid-test, etc.
                sw_report.error = "The test stopped part way through: %s" % e
                say(sw_report.error)
    finally:
        for s in sessions.values():
            s.close()

    report.finished_at = _now()
    say("Done: %s" % report.status.value.upper())
    return report


class _JackRun:
    def __init__(self, jt, lab, names, say):
        self.jt = jt
        self.lab = lab
        self.say = say
        self.switch_name = names[jt.switch_id]
        self.ts_name = names[jt.test_switch_id]
        self.by_port = {j.switch_port: j for j in jt.jacks}

    def result(self, jack, findings, row=None):
        return PortResult(
            switch_id=self.jt.switch_id, switch_name=self.switch_name,
            interface=jack.switch_port, diagram_label=drawing_label(jack.switch_port),
            target="Bulkhead %s (%s %s)" % (jack.jack, self.ts_name,
                                            drawing_label(jack.test_switch_port)),
            media="copper", findings=findings, observed=_seen(row), note=jack.note)

    def run(self):
        jt, lab = self.jt, self.lab
        normal = [j for j in jt.jacks if not j.control]
        control = jt.control
        try:
            self.say("Turning off the test switch's bulkhead ports...")
            lab.set_ports([j.test_switch_port for j in normal] + jt.also_shut, on=False)
            watched = {j.switch_port for j in normal}
            baseline = lab.wait(lambda st: not (_up(st) & watched), jt.link_timeout)
            results = [self.test_jack(j, _up(baseline)) for j in normal]
            if control:
                passed = [j for j, r in zip(normal, results) if r.status == Status.PASS]
                results.append(self.test_control(control, passed[0] if passed else None))
            return results
        finally:
            if control:   # never leave the laptop cut off from Switch 1
                try:
                    lab.set_ports([control.test_switch_port], on=True)
                except Exception:
                    pass

    def check_link(self, jack, row):
        """Findings for a port that came up: speed and duplex against the config."""
        label = drawing_label(jack.switch_port)
        out = []
        if self.jt.speed_mbps and row.get("speed_mbps") and row["speed_mbps"] != self.jt.speed_mbps:
            if row["speed_mbps"] < self.jt.speed_mbps:
                out.append(Finding(
                    Status.FAIL, "speed",
                    "%s came up at %s instead of %d Mb/s." % (label, row["speed_raw"],
                                                              self.jt.speed_mbps),
                    "One of the four wire pairs in the run to %s is broken or miswired "
                    "(gigabit needs all four). Re-terminate or replace the cable." % jack.jack))
            else:
                out.append(Finding(Status.WARN, "speed", "%s came up at %s." % (
                    label, row["speed_raw"])))
        if self.jt.duplex and row.get("duplex") and row["duplex"] != self.jt.duplex:
            out.append(Finding(
                Status.FAIL, "duplex", "%s came up at %s duplex." % (label, row["duplex_raw"]),
                "Usually a damaged pair or a bad patch cable on %s. Replace the patch "
                "cable and re-test." % jack.jack))
        return out

    def test_jack(self, jack, baseline_up):
        lab, label = self.lab, drawing_label(jack.switch_port)
        ts_label = drawing_label(jack.test_switch_port)
        self.say("Testing %s: turning on %s %s" % (jack.jack, self.ts_name, ts_label))
        if jack.switch_port in baseline_up:
            return self.result(jack, [Finding(
                Status.FAIL, "jack",
                "%s has link even with every test-switch port turned off." % label,
                "%s is plugged into something other than the test switch. Check the "
                "cable on the outside of the bulkhead." % jack.jack)], lab.status().get(jack.switch_port))

        lab.set_ports([jack.test_switch_port], on=True)
        st = lab.wait(lambda s: jack.switch_port in _up(s), self.jt.link_timeout)
        came_up = sorted(_up(st) - baseline_up)
        row = st.get(jack.switch_port)
        findings = []
        if jack.switch_port in came_up:
            findings.append(Finding(Status.PASS, "jack",
                                    "Came up when %s %s was turned on." % (self.ts_name, ts_label)))
            findings += self.check_link(jack, row)
            others = [p for p in came_up if p != jack.switch_port]
            if others:
                findings.append(Finding(
                    Status.FAIL, "jack",
                    "%s also came up at the same time." % ", ".join(map(drawing_label, others)),
                    "Two Switch 1 ports are wired to the same test-switch port. Check "
                    "the patching at the bulkheads."))
        elif came_up:
            wrong = came_up[0]
            other = self.by_port.get(wrong)
            where = " (bulkhead %s)" % other.jack if other else ""
            findings.append(Finding(
                Status.FAIL, "jack",
                "Turning on %s %s brought up %s%s instead of %s."
                % (self.ts_name, ts_label, drawing_label(wrong), where, label),
                ("The cables for %s and %s are probably swapped." % (jack.jack, other.jack))
                if other else
                "The cable at %s goes to the wrong Switch 1 port. Trace it from the "
                "bulkhead back to the switch." % jack.jack))
            row = st.get(wrong)
        else:
            findings.append(Finding(
                Status.FAIL, "jack",
                "No Switch 1 port came up when %s %s was turned on." % (self.ts_name, ts_label),
                "Check the patch cable from the test switch to %s, then the cable from "
                "%s to Switch 1 %s (open cable, wrong jack, or bad termination)."
                % (jack.jack, jack.jack, label)))

        lab.set_ports([jack.test_switch_port], on=False)
        if came_up:
            st = lab.wait(lambda s: not (_up(s) & set(came_up)), self.jt.link_timeout)
            stuck = sorted(_up(st) & set(came_up))
            if stuck:
                findings.append(Finding(
                    Status.FAIL, "jack",
                    "%s stayed up after %s %s was turned off."
                    % (", ".join(map(drawing_label, stuck)), self.ts_name, ts_label),
                    "Something other than the test switch is also connected. Check the "
                    "outside of %s." % jack.jack))
        return self.result(jack, findings, row)

    def test_control(self, jack, helper):
        lab, label = self.lab, drawing_label(jack.switch_port)
        ts_label = drawing_label(jack.test_switch_port)
        if helper is None:
            return self.result(jack, [Finding(
                Status.WARN, "jack",
                "Not tested: %s carries the laptop's connection to Switch 1, and no other "
                "jack passed to carry it while %s is turned off." % (jack.jack, jack.jack),
                "Fix the other jacks and run the test again.")])

        self.say("Testing %s: moving the connection to %s first" % (jack.jack, helper.jack))
        lab.set_ports([helper.test_switch_port], on=True)
        lab.wait(lambda s: helper.switch_port in _up(s), self.jt.link_timeout)
        self.say("Waiting about %d seconds for the switches to accept the new path..."
                 % SETTLE_SECONDS)
        lab.sleep(SETTLE_SECONDS)
        findings = []
        try:
            self.say("Testing %s: turning off %s %s" % (jack.jack, self.ts_name, ts_label))
            lab.set_ports([jack.test_switch_port], on=False)
            st = lab.wait(lambda s: jack.switch_port not in _up(s), self.jt.link_timeout)
            if jack.switch_port in _up(st):
                findings.append(Finding(
                    Status.FAIL, "jack",
                    "%s stayed up after %s %s was turned off." % (label, self.ts_name, ts_label),
                    "%s isn't cabled to %s %s. Check the control cable."
                    % (jack.jack, self.ts_name, ts_label)))
        except Exception as e:
            lab.set_ports([jack.test_switch_port], on=True)
            return self.result(jack, [Finding(
                Status.FAIL, "jack",
                "Lost the connection to Switch 1 when %s was turned off (%s)." % (jack.jack, e),
                "%s didn't carry the connection. Run the test again; if it repeats, "
                "check %s." % (helper.jack, helper.jack))])

        lab.set_ports([jack.test_switch_port], on=True)
        st = lab.wait(lambda s: jack.switch_port in _up(s), self.jt.link_timeout)
        row = st.get(jack.switch_port)
        if jack.switch_port in _up(st):
            if not findings:
                findings.append(Finding(
                    Status.PASS, "jack",
                    "Went down and came back up with %s %s." % (self.ts_name, ts_label)))
            findings += self.check_link(jack, row)
        else:
            findings.append(Finding(
                Status.FAIL, "jack", "%s didn't come back up." % label,
                "Check the control cable from %s %s to %s."
                % (self.ts_name, ts_label, jack.jack)))
        lab.set_ports([helper.test_switch_port], on=False)
        return self.result(jack, findings, row)
