"""Copper and fiber tests against a simulated Switch 1 + external test switch."""

import itertools
import json
import os
import shutil

import pytest

from nettest import ConfigError, OfflineCollector, Status, load_test, run_test
from nettest import gui_logic as logic
from nettest.engine import TestReport as Report
from nettest.interfaces import normalize_interface, short_interface
from nettest.testswitch import run_test_switch_tests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIGS = os.path.join(os.path.dirname(HERE), "configs", "standard-build")
COMBINED = os.path.join(CONFIGS, "combined.json")
SAMPLES = os.path.join(HERE, "samples")

COPPER = {   # test-switch port -> Switch 1 port, as cabled in the standard build
    "Gi1/1": "Gi1/6", "Gi1/2": "Gi2/1", "Gi1/3": "Gi2/2", "Gi1/4": "Gi2/3",
    "Gi1/5": "Gi2/4", "Gi1/6": "Gi2/5", "Gi1/7": "Gi2/6", "Gi1/8": "Gi2/7",
    "Gi1/9": "Gi2/8",
}
FIBER = {"Te1/1": "Te1/1", "Te1/2": "Te1/2"}
CONTROL = "GigabitEthernet1/1"
TE = ("TenGigabitEthernet1/1", "TenGigabitEthernet1/2")


def _n(d):
    return {normalize_interface(k): normalize_interface(v) for k, v in d.items()}


def _transceiver_template():
    text = open(os.path.join(SAMPLES, "switch1_good.log")).read()
    start = text.index("SW1-ESS3300#show interfaces transceiver detail\n")
    start = text.index("\n", start) + 1
    end = text.index("SW1-ESS3300#", start)
    return text[start:end]


TRANSCEIVER = _transceiver_template()


class FakeLab:
    """A test switch cabled to Switch 1. Every test-switch port starts turned on."""

    def __init__(self, copper=None, fiber=None, speeds=None, always_up=(), dead_links=(),
                 lacp=True, rx_dbm=-3.1):
        self.copper = _n(COPPER if copper is None else copper)
        self.fiber = _n(FIBER if fiber is None else fiber)    # test-switch -> Switch 1
        self.speeds = {normalize_interface(k): v for k, v in (speeds or {}).items()}
        self.always_up = {normalize_interface(p) for p in always_up}
        # Links that come up but carry no traffic (e.g. a broken strand).
        self.dead = {normalize_interface(p) for p in dead_links}
        self.lacp = lacp
        self.rx_dbm = rx_dbm
        self.on = set(self.copper) | set(self.fiber) | set(TE)
        self.config_log = []

    def wiring(self):
        return {**self.copper, **self.fiber}

    def up(self):
        ports = set(self.always_up)
        for ts_port, sw_port in self.wiring().items():
            if ts_port in self.on:
                ports.add(sw_port)
        return ports

    def far_end(self, sw_port):
        return next((t for t, s in self.wiring().items() if s == sw_port), None)

    def reachable(self):
        """The laptop sits on the test switch, so it reaches Switch 1 over any live link."""
        return any(p in self.on and p not in self.dead for p in self.wiring())

    def status_text(self):
        rows = ["Port         Name   Status       Vlan  Duplex  Speed Type"]
        ports = set(self.copper.values()) | set(TE) | self.always_up
        for p in sorted(ports):
            fiber = p.startswith("Ten")
            kind = "SFP-10GBase-SR" if fiber else "10/100/1000BaseTX"
            if p in self.up():
                speed = self.speeds.get(p, "10G" if fiber else "a-1000")
                rows.append("%-12s        connected    1     a-full  %s %s"
                            % (short_interface(p), speed, kind))
            else:
                rows.append("%-12s        notconnect   1       auto   auto %s"
                            % (short_interface(p), kind))
        return "\n".join(rows)

    def cdp_text(self):
        blocks = []
        for p in sorted(self.up() & set(TE)):
            blocks.append("-------------------------\nDevice ID: TESTSW.lab\n"
                          "Platform: cisco C9300,  Capabilities: Switch\n"
                          "Interface: %s,  Port ID (outgoing port): %s\n"
                          % (p, self.far_end(p)))
        return "\n".join(blocks) or "Total cdp entries displayed : 0"

    def transceiver_text(self):
        text = TRANSCEIVER
        for p, short in zip(TE, ("Te1/1", "Te1/2")):
            rx = "%-20s" % ("%.1f" % self.rx_dbm if p in self.up() else "-40.0       --")
            old = [ln for ln in text.splitlines() if ln.startswith(short)][-1]
            text = text.replace(old, "%-11s%s 2.0         -1.0       -9.9       -13.9"
                                % (short, rx))
        return text

    def etherchannel_text(self):
        members = []
        for p in TE:
            flag = ("P" if self.lacp else "I") if p in self.up() else "D"
            members.append("%s(%s)" % (short_interface(p), flag))
        bundled = any(m.endswith("(P)") for m in members)
        return ("Group  Port-channel  Protocol    Ports\n"
                "------+-------------+-----------+------------------------\n"
                "1      Po1(%s)         LACP        %s\n" % ("SU" if bundled else "SD",
                                                           "    ".join(members)))


class FakeSession:
    def __init__(self, lab, role):
        self.lab, self.role, self.closed = lab, role, False

    def run(self, command):
        lab = self.lab
        if self.role == "test_switch":
            assert command.startswith("ping ")
            ok = any(t in lab.on and t not in lab.dead for t in lab.fiber)
            return "Success rate is %d percent" % (100 if ok else 0)
        if not lab.reachable():
            raise OSError("Socket is closed")
        return {
            "show interfaces status": lab.status_text,
            "show cdp neighbors detail": lab.cdp_text,
            "show lldp neighbors detail": lambda: "Total entries displayed: 0",
            "show interfaces transceiver detail": lab.transceiver_text,
            "show etherchannel summary": lab.etherchannel_text,
        }[command]()

    def hostname(self):
        return "TESTSW" if self.role == "test_switch" else "SW1-ESS3300"

    def configure(self, lines):
        assert self.role == "test_switch"
        self.lab.config_log.append(list(lines))
        for iface, action in zip(lines[::2], lines[1::2]):
            port = normalize_interface(iface.split(None, 1)[1])
            if action == "shutdown":
                self.lab.on.discard(port)
            else:
                self.lab.on.add(port)

    def close(self):
        self.closed = True


class FakeCollector(OfflineCollector):
    """Reads saved output for the normal checks; opens the simulated lab for the extras."""

    def __init__(self, lab, role, saved=None):
        super().__init__(saved)
        self.lab, self.role, self.host = lab, role, "192.168.0.20"

    def open(self, on_progress=None):
        return FakeSession(self.lab, self.role)


def run(lab, extras=("copper", "fiber"), config=COMBINED):
    plan = load_test(config)
    report = Report(test_name=plan.name, mode=plan.mode, config_path=plan.path,
                        started_at="2026-10-15T12:00:00")
    clock = itertools.count()
    run_test_switch_tests(plan, {"switch1": FakeCollector(lab, "switch"),
                                 "test_switch": FakeCollector(lab, "test_switch")},
                          extras, report, sleep=lambda s: None, clock=lambda: next(clock))
    sections = {s.name: s for s in report.switches}
    rows = {}
    for s in report.switches:
        for r in s.results:
            key = r.target.split()[1] if s.name == "Copper test" else r.diagram_label
            rows[key] = r
    return report, sections, rows


# ---- copper ----------------------------------------------------------------

def test_good_wiring_passes_both_tests_and_turns_everything_back_on():
    lab = FakeLab()
    report, sections, rows = run(lab)
    assert report.status == Status.PASS, report.to_text()
    assert list(sections) == ["Copper test", "Fiber test"]
    assert [r.target.split()[1] for r in sections["Copper test"].results] == [
        "1-J7", "1-J8", "1-J9", "1-J10", "1-J11", "1-J12", "1-J14", "1-J15", "1-J6"]
    assert rows["1-J7"].diagram_label == "GI 2/1"
    assert rows["1-J7"].target == "Bulkhead 1-J7 (Test switch GI 1/2)"
    assert rows["1-J7"].switch_name == "Copper test"
    assert "Went down and came back up" in rows["1-J6"].summary
    assert lab.on == set(lab.copper) | set(TE)   # every port back on at the end


def test_copper_test_turns_the_fiber_off_and_works_with_a_dead_fiber():
    lab = FakeLab(dead_links=TE, rx_dbm=-40.0)
    report, sections, rows = run(lab, extras=("copper",))
    assert list(sections) == ["Copper test"]
    assert sections["Copper test"].status == Status.PASS, report.to_text()
    first = lab.config_log[0]
    assert "interface TenGigabitEthernet1/1" in first and first[-1] == "shutdown"


def test_swapped_cables_name_both_jacks():
    copper = dict(COPPER, **{"Gi1/2": "Gi2/2", "Gi1/3": "Gi2/1"})
    _, _, rows = run(FakeLab(copper), extras=("copper",))
    for jack in ("1-J7", "1-J8"):
        assert rows[jack].status == Status.FAIL
    assert "brought up GI 2/2 (bulkhead 1-J8) instead of GI 2/1" in rows["1-J7"].summary
    assert any("1-J7 and 1-J8 are probably swapped" in (f.action or "")
               for f in rows["1-J7"].findings)
    assert rows["1-J9"].status == Status.PASS


def test_open_cable_fails_with_no_link():
    copper = dict(COPPER)
    del copper["Gi1/4"]
    _, _, rows = run(FakeLab(copper), extras=("copper",))
    assert rows["1-J9"].status == Status.FAIL
    assert rows["1-J9"].summary.startswith("No Switch 1 port came up")


def test_100mb_link_means_a_broken_pair():
    _, _, rows = run(FakeLab(speeds={"Gi2/4": "a-100"}), extras=("copper",))
    assert rows["1-J10"].status == Status.FAIL
    assert "came up at a-100 instead of 1000" in rows["1-J10"].summary
    assert "four wire pairs" in rows["1-J10"].findings[-1].action


def test_jack_plugged_into_something_else():
    _, _, rows = run(FakeLab(always_up=["Gi2/5"]), extras=("copper",))
    assert rows["1-J11"].status == Status.FAIL
    assert "link even with every test-switch port turned off" in rows["1-J11"].summary


def test_control_jack_not_tested_when_no_other_jack_passed():
    _, _, rows = run(FakeLab({"Gi1/1": "Gi1/6"}), extras=("copper",))
    assert rows["1-J6"].status == Status.WARN
    assert rows["1-J6"].summary.startswith("Not tested")


def test_lost_connection_during_control_test_turns_control_back_on():
    lab = FakeLab(dead_links=["Gi1/2"] + list(TE))   # 1-J7 links up but carries nothing
    _, _, rows = run(lab, extras=("copper",))
    assert rows["1-J7"].status == Status.PASS
    assert rows["1-J6"].status == Status.FAIL
    assert "Lost the connection to Switch 1" in rows["1-J6"].summary
    assert CONTROL in lab.on


# ---- fiber -----------------------------------------------------------------

def test_fiber_checks_link_light_neighbor_bundle_and_failover():
    lab = FakeLab()
    _, sections, rows = run(lab, extras=("fiber",))
    assert sections["Fiber test"].status == Status.PASS, sections["Fiber test"].results
    te1 = rows["TE 1/1"]
    msgs = [f.message for f in te1.findings]
    assert te1.target == "Test switch TE 1/1 (fiber bulkhead P1/S1)"
    assert any("sees TESTSW.lab on port TE 1/1" in m for m in msgs)
    assert "TE 1/1 is bundled in Po1." in msgs
    assert any(m.startswith("With TE 1/1 turned off, traffic kept flowing over TE 1/2")
               for m in msgs)
    assert lab.on == set(lab.copper) | set(TE)


def test_crossed_fiber_pairs_are_reported():
    _, _, rows = run(FakeLab(fiber={"Te1/1": "Te1/2", "Te1/2": "Te1/1"}), extras=("fiber",))
    assert rows["TE 1/1"].status == Status.FAIL
    assert "TE 1/1 goes to port TE 1/2 on Test switch, but should go to port TE 1/1" in \
        rows["TE 1/1"].summary


def test_fiber_without_lacp_on_the_test_switch():
    _, _, rows = run(FakeLab(lacp=False), extras=("fiber",))
    assert rows["TE 1/1"].status == Status.FAIL
    assert "isn't bundled: the test switch isn't running LACP" in rows["TE 1/1"].summary


def test_failed_fiber_link_fails_link_and_failover():
    fiber = {"Te1/1": "Te1/1"}            # pair P2/S2 is broken
    _, _, rows = run(FakeLab(fiber=fiber), extras=("fiber",))
    assert rows["TE 1/2"].status == Status.FAIL
    assert rows["TE 1/2"].summary.startswith("TE 1/2 has no link")
    assert any("With TE 1/1 turned off" in f.message and f.status == Status.FAIL
               for f in rows["TE 1/1"].findings)


# ---- running from run_test, config, and the form ---------------------------

def test_run_test_adds_sections_after_the_normal_checks(monkeypatch):
    lab = FakeLab()
    plan = load_test(COMBINED)
    saved = {"switch1": os.path.join(SAMPLES, "combined_switch1.log"),
             "switch2": os.path.join(SAMPLES, "combined_switch2.log")}
    collectors = {"switch1": FakeCollector(lab, "switch", saved["switch1"]),
                  "switch2": OfflineCollector(saved["switch2"]),
                  "test_switch": FakeCollector(lab, "test_switch")}
    clock = itertools.count()
    monkeypatch.setattr("nettest.testswitch.time.sleep", lambda s: None)
    monkeypatch.setattr("nettest.testswitch.time.monotonic", lambda: next(clock))
    report = run_test(plan, collectors, extras=("copper", "fiber"))
    assert [s.name for s in report.switches] == ["Switch 1", "Switch 2", "Copper test",
                                                 "Fiber test"]
    assert "Copper test (TESTSW)\n" in report.to_text()


def test_only_three_test_modes_are_listed():
    from nettest import find_tests
    assert [label for label, _ in find_tests(CONFIGS)] == [
        "Switch 1", "Switch 2", "Switch 1 + Switch 2 connected"]
    assert load_test(COMBINED).extras == ("copper", "fiber")
    assert load_test(os.path.join(CONFIGS, "switch1.json")).extras == ()


def test_offline_mode_refuses_the_extras():
    plan = load_test(COMBINED)
    with pytest.raises(logic.FormError, match="need live switches"):
        logic.build_collectors(plan, True, {}, "", "", extras=("copper",))


def test_extras_ask_for_the_test_switch_ip():
    plan = load_test(COMBINED)
    ips = {"switch1": "192.168.0.20", "switch2": "192.168.0.10"}
    with pytest.raises(logic.FormError, match="Enter the IP address for Test switch"):
        logic.build_collectors(plan, False, ips, "admin", "pw", extras=("fiber",))
    c = logic.build_collectors(plan, False, dict(ips, test_switch="192.168.0.60"),
                               "admin", "pw", extras=("fiber",))
    assert c["test_switch"].host == "192.168.0.60"
    assert "test_switch" not in logic.build_collectors(plan, False, ips, "admin", "pw")


def test_confirmation_names_the_control_cable():
    text = logic.test_switch_warning(load_test(COMBINED), ("copper", "fiber"))
    assert "Test switch GigabitEthernet1/1 is cabled to 1-J6" in text
    assert "copper and fiber tests" in text


@pytest.mark.parametrize("edit,message", [
    (lambda ts: ts["copper_test"]["jacks"][1].update(control=True),
     "more than one jack as the control"),
    (lambda ts: ts["copper_test"]["jacks"][1].update(
        test_switch_port=ts["copper_test"]["jacks"][0]["test_switch_port"]),
     "GigabitEthernet1/1 for more than one link"),
    (lambda ts: ts["fiber_test"].update(switch="switch9"), "unknown switch 'switch9'"),
])
def test_test_switch_config_errors(tmp_path, edit, message):
    site = tmp_path / "site"
    shutil.copytree(CONFIGS, site)
    path = site / "combined.json"
    data = json.loads(path.read_text())
    edit(data["test_switch"])
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match=message):
        load_test(str(path))
