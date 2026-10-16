"""Copper and fiber tests that use an external test switch.

These run inside the Switch 1 + Switch 2 connected test, after the normal
read-only checks, when the tech ticks them. The laptop logs in to Switch 1 and
to the test switch, and the tool turns test-switch ports off and on.

**Copper test.** Every test-switch port cabled to a bulkhead jack is turned off,
along with the test switch's fiber ports (so a failed fiber doesn't matter).
Then each jack's port is turned on, Switch 1's ``show interfaces status`` is
read, and the port is turned off again. A jack passes when exactly the
expected Switch 1 port comes up (at the expected speed and duplex) and goes
down again. The jack marked ``control`` carries the laptop's connection to
Switch 1, so it stays on and is tested last, while a jack that already passed
carries the connection.

**Fiber test.** The other bulkhead ports stay off and the fiber ports are
turned on. Each link must come up at 10G with good light levels, see the test
switch's matching port over CDP, and be bundled in the EtherChannel. Then each
fiber port is turned off in turn to check traffic keeps flowing over the other.

Changes on the test switch are never saved (no ``write memory``). Every port the
tests touch is turned back on at the end, even if a test stops part way.
"""

import time

from .checker import Finding, PortResult, Status, SwitchChecker
from .collectors import CollectionError, build_snapshot
from .config import PortExpectation, SwitchPlan
from .engine import SwitchReport
from .interfaces import drawing_label
from .parsers import (parse_etherchannel_summary, parse_interfaces_status,
                      parse_ping_success)

STATUS_COMMAND = "show interfaces status"
FIBER_COMMANDS = ("show interfaces status", "show cdp neighbors detail",
                  "show lldp neighbors detail", "show interfaces transceiver detail")
ETHERCHANNEL_COMMAND = "show etherchannel summary"

# Before the control port is turned off, give the helper jack's link time to
# finish spanning tree's listening/learning (2 x 15 s forward delay) on Switch 1,
# so the laptop's connection has a forwarding path to move to.
SETTLE_SECONDS = 32
# CDP announces every 60 s, and LACP needs a few seconds to bundle a link.
FIBER_SETTLE_SECONDS = 75

COPPER_NAME = "Copper test"
FIBER_NAME = "Fiber test"


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

    def until(self, read, done, timeout):
        """Call ``read()`` until ``done(value)`` or ``timeout`` seconds; return the last value."""
        end = self.clock() + timeout
        while True:
            value = read()
            if done(value) or self.clock() >= end:
                return value
            self.sleep(self.poll)

    def wait(self, done, timeout):
        """Read Switch 1's port status until ``done(status)`` or ``timeout`` seconds."""
        return self.until(self.status, done, timeout)


def _up(status):
    return {p for p, row in status.items() if row["status"] == "connected"}


def _seen(row):
    if not row:
        return {}
    return {"status": row["status"], "speed": row.get("speed_raw"),
            "duplex": row.get("duplex_raw")}


def run_test_switch_tests(plan, collectors, extras, report, on_progress=None,
                          sleep=None, clock=None, poll_seconds=1.0):
    """Run the ticked test-switch tests (``extras``: "copper", "fiber") into ``report``.

    Adds a "Copper test" and/or "Fiber test" section to ``report.switches``.
    ``collectors`` must be live (:class:`~nettest.SshCollector`) for the switch
    and the test switch, since the tests change test-switch ports.
    """
    say = on_progress or (lambda msg: None)
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    ts = plan.test_switch
    extras = [e for e in ("copper", "fiber") if e in extras and e in plan.extras]
    if not extras:
        return
    sections = {}
    for e, name in (("copper", COPPER_NAME), ("fiber", FIBER_NAME)):
        if e in extras:
            sections[e] = SwitchReport(id="%s_test" % e, name=name)
            report.switches.append(sections[e])
    switch_id = (ts.copper or ts.fiber).switch_id
    names = {sw.id: sw.name for sw in plan.switches}
    names[ts.id] = ts.name

    def fail_all(message):
        for sr in sections.values():
            sr.error = message
        say(message)

    sessions = {}
    try:
        for sid in (ts.id, switch_id):
            collector = collectors.get(sid)
            if collector is None:
                return fail_all("No connection details were given for %s." % names[sid])
            if not hasattr(collector, "open"):
                return fail_all("The copper and fiber tests turn test-switch ports on and "
                                "off, so they need live switches. They can't run from a "
                                "self test file.")
            for sr in sections.values():
                sr.source = (sr.source + ", " if sr.source else "") + collector.describe()
            try:
                sessions[sid] = collector.open(on_progress=say)
            except CollectionError as e:
                return fail_all("%s: %s" % (names[sid], e))

        lab = _Lab(sessions[switch_id], sessions[ts.id], sleep, clock, poll_seconds)
        try:
            ts_hostname = sessions[ts.id].hostname()
        except Exception:
            ts_hostname = None
        for sr in sections.values():
            sr.hostname = ts_hostname
        try:
            if "copper" in sections:
                try:
                    sections["copper"].results = _CopperRun(plan, lab, names, say).run()
                except Exception as e:  # a dropped login mid-test, etc.
                    sections["copper"].error = "The test stopped part way through: %s" % e
                    say(sections["copper"].error)
            if "fiber" in sections:
                try:
                    host = getattr(collectors[switch_id], "host", None)
                    sections["fiber"].results = _FiberRun(plan, lab, names, say, host,
                                                          ts_hostname).run()
                except Exception as e:
                    sections["fiber"].error = "The test stopped part way through: %s" % e
                    say(sections["fiber"].error)
        finally:
            say("Turning the test switch's ports back on...")
            try:
                lab.set_ports(ts.all_ports(), on=True)
            except Exception:
                pass
    finally:
        for s in sessions.values():
            s.close()


# --------------------------------------------------------------------------
# Copper
# --------------------------------------------------------------------------

class _CopperRun:
    def __init__(self, plan, lab, names, say):
        self.ts = plan.test_switch
        self.cu = self.ts.copper
        self.lab = lab
        self.say = say
        self.timeout = self.ts.link_timeout
        self.switch_name = names[self.cu.switch_id]
        self.ts_name = self.ts.name
        self.by_port = {j.switch_port: j for j in self.cu.jacks}

    def result(self, jack, findings, row=None):
        return PortResult(
            switch_id=self.cu.switch_id, switch_name=COPPER_NAME,
            interface=jack.switch_port, diagram_label=drawing_label(jack.switch_port),
            target="Bulkhead %s (%s %s)" % (jack.jack, self.ts_name,
                                            drawing_label(jack.test_switch_port)),
            media="copper", findings=findings, observed=_seen(row), note=jack.note)

    def run(self):
        lab = self.lab
        normal = [j for j in self.cu.jacks if not j.control]
        control = self.cu.control
        fiber = [lk.test_switch_port for lk in (self.ts.fiber.links if self.ts.fiber else [])]
        try:
            self.say("Turning off the test switch's bulkhead and fiber ports...")
            lab.set_ports([j.test_switch_port for j in normal] + fiber, on=False)
            watched = {j.switch_port for j in normal}
            baseline = lab.wait(lambda st: not (_up(st) & watched), self.timeout)
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
        want_speed, want_duplex = self.cu.speed_mbps, self.cu.duplex
        out = []
        if want_speed and row.get("speed_mbps") and row["speed_mbps"] != want_speed:
            if row["speed_mbps"] < want_speed:
                out.append(Finding(
                    Status.FAIL, "speed",
                    "%s came up at %s instead of %d Mb/s." % (label, row["speed_raw"],
                                                              want_speed),
                    "One of the four wire pairs in the run to %s is broken or miswired "
                    "(gigabit needs all four). Re-terminate or replace the cable." % jack.jack))
            else:
                out.append(Finding(Status.WARN, "speed", "%s came up at %s." % (
                    label, row["speed_raw"])))
        if want_duplex and row.get("duplex") and row["duplex"] != want_duplex:
            out.append(Finding(
                Status.FAIL, "duplex", "%s came up at %s duplex." % (label, row["duplex_raw"]),
                "Usually a damaged pair or a bad patch cable on %s. Replace the patch "
                "cable and re-test." % jack.jack))
        return out

    def test_jack(self, jack, baseline_up):
        lab, label = self.lab, drawing_label(jack.switch_port)
        ts_label = drawing_label(jack.test_switch_port)
        self.say("Copper test %s: turning on %s %s" % (jack.jack, self.ts_name, ts_label))
        if jack.switch_port in baseline_up:
            return self.result(jack, [Finding(
                Status.FAIL, "jack",
                "%s has link even with every test-switch port turned off." % label,
                "%s is plugged into something other than the test switch. Check the "
                "cable on the outside of the bulkhead." % jack.jack)],
                lab.status().get(jack.switch_port))

        lab.set_ports([jack.test_switch_port], on=True)
        st = lab.wait(lambda s: jack.switch_port in _up(s), self.timeout)
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
                "No %s port came up when %s %s was turned on."
                % (self.switch_name, self.ts_name, ts_label),
                "Check the patch cable from the test switch to %s, then the cable from "
                "%s to %s %s (open cable, wrong jack, or bad termination)."
                % (jack.jack, jack.jack, self.switch_name, label)))

        lab.set_ports([jack.test_switch_port], on=False)
        if came_up:
            st = lab.wait(lambda s: not (_up(s) & set(came_up)), self.timeout)
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
                "Not tested: %s carries the laptop's connection to %s, and no other "
                "jack passed to carry it while %s is turned off."
                % (jack.jack, self.switch_name, jack.jack),
                "Fix the other jacks and run the test again.")])

        self.say("Copper test %s: moving the connection to %s first" % (jack.jack, helper.jack))
        lab.set_ports([helper.test_switch_port], on=True)
        lab.wait(lambda s: helper.switch_port in _up(s), self.timeout)
        self.say("Waiting about %d seconds for the switches to accept the new path..."
                 % SETTLE_SECONDS)
        lab.sleep(SETTLE_SECONDS)
        findings = []
        try:
            self.say("Copper test %s: turning off %s %s" % (jack.jack, self.ts_name, ts_label))
            lab.set_ports([jack.test_switch_port], on=False)
            st = lab.wait(lambda s: jack.switch_port not in _up(s), self.timeout)
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
                "Lost the connection to %s when %s was turned off (%s)."
                % (self.switch_name, jack.jack, e),
                "%s didn't carry the connection. Run the test again; if it repeats, "
                "check %s." % (helper.jack, helper.jack))])

        lab.set_ports([jack.test_switch_port], on=True)
        st = lab.wait(lambda s: jack.switch_port in _up(s), self.timeout)
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


# --------------------------------------------------------------------------
# Fiber
# --------------------------------------------------------------------------

_MEMBER_FLAGS = {
    "I": ("isn't bundled: the test switch isn't running LACP on its end",
          "Set the test switch's fiber ports to 'channel-group 1 mode active'."),
    "s": ("is suspended: its settings don't match the other end",
          "Make the speed and access/trunk mode match on both ends of the fiber."),
    "D": ("is down", None),
    "H": ("is a hot-standby member, not carrying traffic",
          "Check the EtherChannel settings on both switches."),
}


class _FiberRun:
    def __init__(self, plan, lab, names, say, switch_ip, ts_hostname):
        self.ts = plan.test_switch
        self.fi = self.ts.fiber
        self.lab = lab
        self.say = say
        self.switch_ip = switch_ip
        self.ts_hostname = ts_hostname
        self.switch_name = names[self.fi.switch_id]
        self.base = plan.switch(self.fi.switch_id)

    def read(self):
        """Read Switch 1 for the fiber checks: a Snapshot plus EtherChannel members."""
        outputs = {cmd: self.lab.switch.run(cmd) for cmd in FIBER_COMMANDS}
        snap = build_snapshot("ssh", outputs)
        ec = parse_etherchannel_summary(self.lab.switch.run(ETHERCHANNEL_COMMAND))
        return snap, ec

    def settled(self, value):
        snap, ec = value
        for lk in self.fi.links:
            if snap.status.get(lk.switch_port, {}).get("status") != "connected":
                return False
            if (snap.has("cdp") or snap.has("lldp")) and not snap.neighbors(lk.switch_port):
                return False
            if self.fi.etherchannel and ec.get(lk.switch_port, {}).get("flag") != "P":
                return False
        return True

    def expectation(self, lk):
        base = self.base.port(lk.switch_port) if self.base else None
        expect = {"link": "up", "speed_mbps": self.fi.speed_mbps, "cdp_neighbor": True}
        if base and base.expect.get("light_levels_dbm"):
            expect["light_levels_dbm"] = base.expect["light_levels_dbm"]
        far = drawing_label(lk.test_switch_port)
        return PortExpectation(
            interface=lk.switch_port, diagram_label=drawing_label(lk.switch_port),
            media="fiber",
            connects_to={"device": self.ts.name, "port": far, "kind": "switch"},
            expect=expect, optic=base.optic if base else None,
            peer={"switch": self.ts.id, "switch_name": self.ts.name,
                  "interface": lk.test_switch_port,
                  "path": [lk.bulkhead] if lk.bulkhead else [], "note": None})

    def run(self):
        lab, links = self.lab, self.fi.links
        self.say("Fiber test: turning on %s's fiber ports..." % self.ts.name)
        lab.set_ports([lk.test_switch_port for lk in links], on=True)
        lab.wait(lambda st: all(lk.switch_port in _up(st) for lk in links), self.ts.link_timeout)
        self.say("Fiber test: waiting up to %d seconds for CDP and the EtherChannel..."
                 % FIBER_SETTLE_SECONDS)
        snap, ec = lab.until(self.read, self.settled, FIBER_SETTLE_SECONDS)

        plan = SwitchPlan(id=self.fi.switch_id, name=self.switch_name, model="",
                          unlisted_ports="ignore",
                          ports=[self.expectation(lk) for lk in links])
        checker = SwitchChecker(plan, snap, {self.ts.id: self.ts_hostname}
                                if self.ts_hostname else {})
        results = []
        for lk, port in zip(links, plan.ports):
            r = checker.check_port(port)
            r.switch_name = FIBER_NAME
            if lk.bulkhead:
                r.target = "%s %s (fiber bulkhead %s)" % (self.ts.name, port.connects_to["port"],
                                                          lk.bulkhead)
            if self.fi.etherchannel and snap.status.get(lk.switch_port, {}).get(
                    "status") == "connected":
                r.findings.append(self.channel_finding(lk, ec))
            results.append(r)

        if len(links) > 1:
            for lk, r in zip(links, results):
                r.findings.append(self.failover(lk))
        return results

    def channel_finding(self, lk, ec):
        label = drawing_label(lk.switch_port)
        m = ec.get(lk.switch_port)
        if m is None:
            return Finding(
                Status.FAIL, "etherchannel",
                "%s isn't in an EtherChannel on %s." % (label, self.switch_name),
                "%s needs 'channel-group 1 mode active' on both fiber ports (see the "
                "design doc). Two unbundled links to one switch get blocked or loop."
                % self.switch_name)
        ch = m["channel"].replace("Port-channel", "Po")
        if m["flag"] == "P":
            return Finding(Status.PASS, "etherchannel", "%s is bundled in %s." % (label, ch))
        what, action = _MEMBER_FLAGS.get(m["flag"], ("is in state %s" % m["flag"], None))
        return Finding(Status.FAIL, "etherchannel", "%s %s (%s)." % (label, what, ch),
                       action or "Check the fiber and the EtherChannel settings on both ends.")

    def failover(self, lk):
        """Turn this fiber port off and check traffic keeps flowing over the others."""
        lab = self.lab
        label = drawing_label(lk.switch_port)
        ts_label = drawing_label(lk.test_switch_port)
        others = [o for o in self.fi.links if o is not lk]
        self.say("Fiber test: turning off %s %s to check the other fiber carries traffic"
                 % (self.ts.name, ts_label))
        lab.set_ports([lk.test_switch_port], on=False)
        try:
            st = lab.wait(lambda s: lk.switch_port not in _up(s), self.ts.link_timeout)
            still_up = [o for o in others if o.switch_port in _up(st)]
            ec = parse_etherchannel_summary(lab.switch.run(ETHERCHANNEL_COMMAND))
            bundled = [o for o in still_up if not self.fi.etherchannel
                       or ec.get(o.switch_port, {}).get("flag") == "P"]
            pct = None
            if self.switch_ip:
                pct = parse_ping_success(lab.test_switch.run(
                    "ping %s repeat 20" % self.switch_ip))
        finally:
            lab.set_ports([lk.test_switch_port], on=True)
        self.say("Fiber test: turning %s %s back on" % (self.ts.name, ts_label))
        lab.until(self.read, self.settled, FIBER_SETTLE_SECONDS)

        rest = ", ".join(drawing_label(o.switch_port) for o in others)
        if len(bundled) == len(others) and (pct is None or pct >= 90):
            ping = " (%d%% of pings answered)" % pct if pct is not None else ""
            return Finding(Status.PASS, "failover",
                           "With %s turned off, traffic kept flowing over %s%s."
                           % (label, rest, ping))
        if pct is not None and pct < 90:
            why = "only %d%% of pings from %s to %s were answered" % (
                pct, self.ts.name, self.switch_name)
        else:
            why = "%s didn't stay up and bundled" % rest
        return Finding(Status.FAIL, "failover",
                       "With %s turned off, %s." % (label, why),
                       "The other fiber can't carry the traffic alone. Check %s's light "
                       "levels and EtherChannel results above." % rest)
