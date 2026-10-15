"""Bulkhead jack test against a simulated Switch 1 + test switch."""

import itertools
import json
import os

import pytest

from nettest import ConfigError, OfflineCollector, Status, load_test, run_test
from nettest import gui_logic as logic
from nettest.interfaces import normalize_interface, short_interface
from nettest.jacktest import run_jack_test

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), "configs", "standard-build", "bulkhead-jacks.json")

GOOD_WIRING = {   # test-switch port -> Switch 1 port, as cabled in the standard build
    "Gi1/1": "Gi1/6", "Gi1/2": "Gi2/1", "Gi1/3": "Gi2/2", "Gi1/4": "Gi2/3",
    "Gi1/5": "Gi2/4", "Gi1/6": "Gi2/5", "Gi1/7": "Gi2/6", "Gi1/8": "Gi2/7",
    "Gi1/9": "Gi2/8", "Gi1/12": "Gi1/4",
}
CONTROL = "GigabitEthernet1/12"


class FakeLab:
    """A test switch cabled to Switch 1. Every test-switch port starts turned on."""

    def __init__(self, wiring=None, speeds=None, always_up=(), dead_links=()):
        self.wiring = {normalize_interface(k): normalize_interface(v)
                       for k, v in (wiring or GOOD_WIRING).items()}
        self.speeds = {normalize_interface(k): v for k, v in (speeds or {}).items()}
        self.always_up = {normalize_interface(p) for p in always_up}
        # Links that show up but don't carry traffic (e.g. blocked or broken).
        self.dead = {normalize_interface(p) for p in dead_links}
        self.on = set(self.wiring) | {"TenGigabitEthernet1/1", "TenGigabitEthernet1/2"}
        self.config_log = []

    def up(self):
        ports = set(self.always_up)
        for ts_port in self.on:
            if ts_port in self.wiring:
                ports.add(self.wiring[ts_port])
        return ports

    def reachable(self):
        """The laptop sits on the test switch, so it reaches Switch 1 over any live link."""
        return any(p in self.on and p not in self.dead for p in self.wiring)

    def status_text(self):
        rows = ["Port         Name   Status       Vlan  Duplex  Speed Type"]
        for p in sorted(set(self.wiring.values()) | self.always_up):
            if p in self.up():
                speed = self.speeds.get(p, "a-1000")
                rows.append("%-12s        connected    1     a-full  %s 10/100/1000BaseTX"
                            % (short_interface(p), speed))
            else:
                rows.append("%-12s        notconnect   1       auto   auto 10/100/1000BaseTX"
                            % short_interface(p))
        return "\n".join(rows)


class FakeSession:
    def __init__(self, lab, role):
        self.lab, self.role, self.closed = lab, role, False

    def run(self, command):
        assert self.role == "switch" and command == "show interfaces status"
        if not self.lab.reachable():
            raise OSError("Socket is closed")
        return self.lab.status_text()

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


class FakeCollector:
    def __init__(self, lab, role):
        self.lab, self.role = lab, role

    def describe(self):
        return "fake %s" % self.role

    def open(self, on_progress=None):
        return FakeSession(self.lab, self.role)


def run(lab):
    plan = load_test(CONFIG)
    clock = itertools.count()
    report = run_jack_test(plan, {"switch1": FakeCollector(lab, "switch"),
                                  "test_switch": FakeCollector(lab, "test_switch")},
                           sleep=lambda s: None, clock=lambda: next(clock))
    return report, {r.target.split()[1]: r for r in report.ports()}


def test_good_wiring_passes_every_jack_and_leaves_only_control_on():
    lab = FakeLab()
    report, jacks = run(lab)
    assert report.status == Status.PASS, report.to_text()
    assert list(jacks) == ["1-J6", "1-J7", "1-J8", "1-J9", "1-J10", "1-J11", "1-J12",
                           "1-J14", "1-J15", "1-J5"]
    assert jacks["1-J7"].diagram_label == "GI 2/1"
    assert jacks["1-J7"].target == "Bulkhead 1-J7 (Test switch GI 1/2)"
    assert "Went down and came back up" in jacks["1-J5"].summary
    assert lab.on == {CONTROL}       # fiber and every jack port off, control back on


def test_swapped_cables_name_both_jacks():
    wiring = dict(GOOD_WIRING, **{"Gi1/2": "Gi2/2", "Gi1/3": "Gi2/1"})
    report, jacks = run(FakeLab(wiring))
    assert report.status == Status.FAIL
    for jack in ("1-J7", "1-J8"):
        assert jacks[jack].status == Status.FAIL
    assert "brought up GI 2/2 (bulkhead 1-J8) instead of GI 2/1" in jacks["1-J7"].summary
    assert any("1-J7 and 1-J8 are probably swapped" in (f.action or "")
               for f in jacks["1-J7"].findings)
    assert jacks["1-J6"].status == Status.PASS


def test_open_cable_fails_with_no_link():
    wiring = dict(GOOD_WIRING)
    del wiring["Gi1/4"]
    _, jacks = run(FakeLab(wiring))
    assert jacks["1-J9"].status == Status.FAIL
    assert jacks["1-J9"].summary.startswith("No Switch 1 port came up")


def test_100mb_link_means_a_broken_pair():
    _, jacks = run(FakeLab(speeds={"Gi2/4": "a-100"}))
    assert jacks["1-J10"].status == Status.FAIL
    assert "came up at a-100 instead of 1000" in jacks["1-J10"].summary
    assert "four wire pairs" in jacks["1-J10"].findings[-1].action


def test_jack_plugged_into_something_else():
    _, jacks = run(FakeLab(always_up=["Gi2/5"]))
    assert jacks["1-J11"].status == Status.FAIL
    assert "link even with every test-switch port turned off" in jacks["1-J11"].summary


def test_other_ports_up_on_switch1_dont_confuse_the_test():
    report, _ = run(FakeLab(always_up=["Gi1/3", "Gi1/5", "Gi1/8"]))   # server, UPS
    assert report.status == Status.PASS


def test_control_jack_not_tested_when_no_other_jack_passed():
    wiring = {"Gi1/12": "Gi1/4"}   # nothing but the control link is cabled
    report, jacks = run(FakeLab(wiring))
    assert jacks["1-J5"].status == Status.WARN
    assert jacks["1-J5"].summary.startswith("Not tested")


def test_lost_connection_during_control_test_turns_control_back_on():
    lab = FakeLab(dead_links=["Gi1/1"])   # 1-J6 links up but carries nothing
    report, jacks = run(lab)
    assert jacks["1-J6"].status == Status.PASS
    assert jacks["1-J5"].status == Status.FAIL
    assert "Lost the connection to Switch 1" in jacks["1-J5"].summary
    assert CONTROL in lab.on


def test_offline_collectors_are_refused():
    plan = load_test(CONFIG)
    report = run_test(plan, {"switch1": OfflineCollector("x.log"),
                             "test_switch": OfflineCollector("y.log")})
    assert all("needs live switches" in s.error for s in report.switches)
    with pytest.raises(logic.FormError, match="needs live switches"):
        logic.build_collectors(plan, True, {}, "", "")


def test_live_form_asks_for_both_ips():
    plan = load_test(CONFIG)
    with pytest.raises(logic.FormError, match="Enter the IP address for Test switch"):
        logic.build_collectors(plan, False, {"switch1": "192.168.0.20"}, "admin", "pw")


def test_confirmation_names_the_control_cable():
    text = logic.jack_test_warning(load_test(CONFIG))
    assert "test switch GigabitEthernet1/12 is cabled to 1-J5" in text


def test_config_rejects_two_control_jacks(tmp_path):
    data = json.load(open(CONFIG))
    data["jacks"][0]["control"] = True
    path = tmp_path / "jacks.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="more than one jack as the control"):
        load_test(str(path))


def test_config_rejects_a_port_used_twice(tmp_path):
    data = json.load(open(CONFIG))
    data["jacks"][1]["test_switch_port"] = data["jacks"][0]["test_switch_port"]
    path = tmp_path / "jacks.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="GigabitEthernet1/1 for more than one jack"):
        load_test(str(path))
