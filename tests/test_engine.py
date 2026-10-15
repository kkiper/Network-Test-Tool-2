import json
import os
import shutil

import pytest

from nettest import (CollectionError, OfflineCollector, Status, find_tests, load_test,
                     run_test)
from nettest.__main__ import main as cli_main

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIGS = os.path.join(ROOT, "configs", "standard-build")
SAMPLES = os.path.join(HERE, "samples")


def sample(name):
    return os.path.join(SAMPLES, name)


def config(name):
    return os.path.join(CONFIGS, name)


def by_label(report, switch_id=None):
    return {(r.diagram_label if switch_id is None else r.diagram_label): r
            for s in report.switches if switch_id in (None, s.id) for r in s.results}


def run_single(cfg, sample_path):
    plan = load_test(cfg)
    return run_test(plan, {plan.switches[0].id: OfflineCollector(sample_path)})


def write_variant(tmp_path, src, replacements, name=None):
    text = open(src).read()
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / (name or os.path.basename(src))
    path.write_text(text)
    return str(path)


def copy_configs(tmp_path):
    dst = tmp_path / "site"
    shutil.copytree(CONFIGS, dst)
    return dst


def edit_json(path, fn):
    data = json.loads(open(path).read())
    fn(data)
    open(path, "w").write(json.dumps(data, indent=2))


# ---- config loading ------------------------------------------------------

def test_find_tests_lists_singles_then_combined():
    tests = find_tests(CONFIGS)
    assert [label for label, _ in tests] == ["Switch 1", "Switch 2",
                                            "Switch 1 + Switch 2 connected"]


def test_combined_overrides_bulkhead_ports():
    plan = load_test(config("combined.json"))
    assert plan.mode == "combined"
    p = plan.switch("switch1").port("Gi1/4")
    assert p.expect["link"] == "up" and p.expect["cdp_neighbor"] is True
    assert p.peer["interface"] == "GigabitEthernet1/5"
    assert p.target == "Switch 2 GI 1/5"
    single = load_test(config("switch1.json"))
    assert single.switch("switch1").port("Gi1/4").expect["link"] == "optional"


def test_bad_config_gives_readable_error(tmp_path):
    from nettest import ConfigError
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1, "switch": {"id": "x"}, "ports": []}')
    with pytest.raises(ConfigError, match="does not match the config format|missing a required setting"):
        load_test(str(bad))


# ---- single-switch tests -------------------------------------------------

def test_switch1_good_sample_passes():
    report = run_single(config("switch1.json"), sample("switch1_good.log"))
    assert report.status == Status.PASS, report.to_text()
    assert report.switches[0].hostname == "SW1-ESS3300"
    assert report.counts()["pass"] == 18


def test_switch1_faults_sample():
    report = run_single(config("switch1.json"), sample("switch1_faults.log"))
    ports = by_label(report)
    assert report.status == Status.FAIL

    assert ports["GI 1/3"].status == Status.PASS
    assert ports["GI 1/5"].status == Status.FAIL
    assert ports["GI 1/5"].summary == "GI 1/5 has no link."
    # The stray link on GI 1/10 is pointed out as a likely wrong port.
    assert "GI 1/10" in ports["GI 1/5"].findings[0].action
    assert ports["GI 1/7"].status == Status.PASS          # speed isn't checked by default
    assert ports["GI 1/9"].status == Status.FAIL
    assert "err-disabled" in ports["GI 1/9"].summary
    assert ports["GI 1/8"].status == Status.PASS
    assert ports["TE 1/1"].status == Status.FAIL
    assert "-14.6 dBm" in ports["TE 1/1"].summary
    assert ports["TE 1/1"].observed["rx_dbm"] == -14.6
    assert ports["TE 1/2"].status == Status.FAIL
    assert "no SFP" in ports["TE 1/2"].summary
    unused = ports["GI 1/10"]          # unused on the drawing, but something is plugged in
    assert unused.status == Status.FAIL
    assert "nothing should be plugged into it (device MAC 3c:ec:ef:10:20:32)" in unused.summary
    # The internal app-hosting port is always up and is never reported.
    assert [r.diagram_label for r in report.ports() if not r.listed] == []


def test_unused_port_empty_passes_and_missing_one_is_skipped(tmp_path):
    ports = by_label(run_single(config("switch1.json"), sample("switch1_good.log")))
    assert ports["GI 1/10"].summary == "GI 1/10 is empty, as expected."
    site = copy_configs(tmp_path)
    edit_json(site / "switch1.json", lambda d: d["ports"].append({
        "interface": "GigabitEthernet3/1", "media": "copper",
        "connects_to": {"device": "Unused", "kind": "unused"}, "expect": {"link": "down"}}))
    report = run_single(str(site / "switch1.json"), sample("switch1_good.log"))
    assert "GI 3/1" not in by_label(report) and report.status == Status.PASS


def test_switch2_folder_sample_passes_and_notes_lldp_off():
    report = run_single(config("switch2.json"), sample("switch2_good"))
    assert report.status == Status.PASS, report.to_text()
    assert report.switches[0].hostname == "SW2-ESS3300"
    assert "show lldp neighbors detail" in report.switches[0].unavailable


def test_speed_checked_when_config_sets_it(tmp_path):
    site = copy_configs(tmp_path)
    edit_json(site / "switch1.json",
              lambda d: d["defaults"]["copper"]["expect"].update(speed_mbps=1000))
    report = run_single(str(site / "switch1.json"), sample("switch1_faults.log"))
    g = by_label(report)["GI 1/7"]
    assert g.status == Status.WARN
    assert "100 Mb/s" in g.summary


def test_light_level_warning_band(tmp_path):
    # -10.5 dBm is between the SFP's low warning (-9.9) and low alarm (-13.9).
    path = write_variant(tmp_path, sample("switch1_good.log"),
                         [("Te1/1      -3.1 ", "Te1/1      -10.5 -")])
    te = by_label(run_single(config("switch1.json"), path))["TE 1/1"]
    assert te.status == Status.WARN
    assert "warning level" in te.summary


def test_light_levels_fall_back_to_config_range(tmp_path):
    site = copy_configs(tmp_path)

    def no_module(d):
        for p in d["ports"]:
            if p["media"] == "fiber":
                p["expect"]["light_levels_dbm"]["use_module_thresholds"] = False
    edit_json(site / "switch1.json", no_module)
    # -10.5 is a module warning but inside the datasheet range (-11.1..0.5).
    path = write_variant(tmp_path, sample("switch1_good.log"),
                         [("Te1/1      -3.1 ", "Te1/1      -10.5 -")])
    te = by_label(run_single(str(site / "switch1.json"), path))["TE 1/1"]
    assert te.status == Status.PASS
    # -11.5 is outside the datasheet range.
    path = write_variant(tmp_path, sample("switch1_good.log"),
                         [("Te1/1      -3.1 ", "Te1/1      -11.5 -")], name="b.log")
    te = by_label(run_single(str(site / "switch1.json"), path))["TE 1/1"]
    assert te.status == Status.FAIL
    assert "datasheet range" in te.summary


def test_no_light_when_link_up(tmp_path):
    path = write_variant(tmp_path, sample("switch1_good.log"),
                         [("Te1/1      -3.1 ", "Te1/1      -40.0 --")])
    te = by_label(run_single(config("switch1.json"), path))["TE 1/1"]
    assert te.status == Status.FAIL
    assert "no light" in te.summary


def test_swapped_cables_detected_by_mac(tmp_path):
    site = copy_configs(tmp_path)

    def add_macs(d):
        macs = {"GigabitEthernet1/3": "3c:ec:ef:10:20:31", "GigabitEthernet1/5": "3cec.ef10.2032"}
        for p in d["ports"]:
            if p["interface"] in macs:
                p["expect"]["mac_addresses"] = [macs[p["interface"]]]
    edit_json(site / "switch1.json", add_macs)
    path = write_variant(tmp_path, sample("switch1_good.log"), [
        ("  10    3cec.ef10.2031    DYNAMIC     Gi1/3", "  10    3cec.ef10.2031    DYNAMIC     Gi1/5"),
        ("  10    3cec.ef10.2032    DYNAMIC     Gi1/5", "  10    3cec.ef10.2032    DYNAMIC     Gi1/3"),
    ])
    ports = by_label(run_single(str(site / "switch1.json"), path))
    assert ports["GI 1/3"].status == Status.FAIL
    assert "plugged into GI 1/5 instead" in ports["GI 1/3"].summary
    assert "swapped" in ports["GI 1/3"].findings[-1].action


def test_unlisted_port_with_link(tmp_path):
    path = write_variant(tmp_path, sample("switch1_good.log"), [(
        "Gi2/9                           notconnect   1            auto   auto",
        "Gi2/9                           connected    1          a-full a-1000")])
    stray = by_label(run_single(config("switch1.json"), path))["GI 2/9"]
    assert not stray.listed and stray.status == Status.WARN
    site = copy_configs(tmp_path)
    edit_json(site / "switch1.json", lambda d: d.update(unlisted_ports="fail_if_up"))
    assert by_label(run_single(str(site / "switch1.json"), path))["GI 2/9"].status == Status.FAIL


def test_admin_down_port(tmp_path):
    path = write_variant(tmp_path, sample("switch1_good.log"), [(
        "Gi1/3        Server P1          connected    10         a-full a-1000",
        "Gi1/3        Server P1          disabled     10           auto   auto")])
    g = by_label(run_single(config("switch1.json"), path))["GI 1/3"]
    assert g.status == Status.FAIL
    assert "turned off in the switch configuration" in g.summary


# ---- combined test -------------------------------------------------------

def run_combined(sw1, sw2, cfg=None):
    plan = load_test(cfg or config("combined.json"))
    return run_test(plan, {"switch1": OfflineCollector(sw1), "switch2": OfflineCollector(sw2)})


def test_combined_good_sample_passes():
    report = run_combined(sample("combined_switch1.log"), sample("combined_switch2.log"))
    assert report.status == Status.PASS, report.to_text()
    gi14 = by_label(report, "switch1")["GI 1/4"]
    assert "sees SW2-ESS3300" in gi14.summary


def test_combined_wrong_far_end_port(tmp_path):
    sw1 = write_variant(tmp_path, sample("combined_switch1.log"), [(
        "Port ID (outgoing port): GigabitEthernet1/5",
        "Port ID (outgoing port): GigabitEthernet1/6")])
    report = run_combined(sw1, sample("combined_switch2.log"))
    gi14 = by_label(report, "switch1")["GI 1/4"]
    assert gi14.status == Status.FAIL
    assert gi14.summary == "GI 1/4 goes to port GI 1/6 on Switch 2, but should go to port GI 1/5."


def test_combined_wrong_device(tmp_path):
    sw1 = write_variant(tmp_path, sample("combined_switch1.log"), [(
        "Device ID: SW2-ESS3300.example.local", "Device ID: SOME-OTHER-SWITCH")])
    gi14 = by_label(run_combined(sw1, sample("combined_switch2.log")), "switch1")["GI 1/4"]
    assert gi14.status == Status.FAIL
    assert "SOME-OTHER-SWITCH" in gi14.summary


def test_combined_interconnect_down(tmp_path):
    sw2 = write_variant(tmp_path, sample("combined_switch2.log"), [(
        "Gi1/5        Bulkhead 2-J4      connected    20         a-full a-1000",
        "Gi1/5        Bulkhead 2-J4      notconnect   20           auto   auto")])
    ge15 = by_label(run_combined(sample("combined_switch1.log"), sw2), "switch2")["GI 1/5"]
    assert ge15.status == Status.FAIL
    assert "bulkheads 1-J5 and 2-J4" in ge15.findings[0].action


def test_combined_no_cdp_neighbor(tmp_path):
    sw2 = write_variant(tmp_path, sample("combined_switch2.log"), [(
        "Device ID: SW1-ESS3300", "Device ID: SW1-ESS3300-gone")])
    sw2 = write_variant(tmp_path, sw2, [(
        "Interface: GigabitEthernet1/5,", "Interface: GigabitEthernet1/7,")], name="b.log")
    ge15 = by_label(run_combined(sample("combined_switch1.log"), sw2), "switch2")["GI 1/5"]
    assert ge15.status == Status.FAIL
    assert "can't see Switch 1" in ge15.summary


def test_combined_placeholder_is_not_configured(tmp_path):
    site = copy_configs(tmp_path)
    edit_json(site / "combined.json",
              lambda d: d["interconnects"][0].update(placeholder=True))
    report = run_combined(sample("combined_switch1.log"), sample("combined_switch2.log"),
                          str(site / "combined.json"))
    assert by_label(report, "switch1")["GI 1/4"].status == Status.NOT_CONFIGURED
    assert report.status == Status.NOT_CONFIGURED


# ---- errors, reports, CLI ------------------------------------------------

class Unreachable:
    def describe(self):
        return "ssh 192.0.2.1"

    def collect(self, on_progress=None):
        raise CollectionError("Could not reach 192.0.2.1.")


def test_unreachable_switch_reported_not_raised():
    plan = load_test(config("combined.json"))
    report = run_test(plan, {"switch1": OfflineCollector(sample("combined_switch1.log")),
                             "switch2": Unreachable()})
    sw2 = report.switches[1]
    assert sw2.error == "Could not reach 192.0.2.1."
    assert report.status == Status.FAIL
    assert "Could not test this switch" in report.to_text()


def test_missing_collector_reported():
    report = run_test(load_test(config("switch1.json")), {})
    assert "No connection details" in report.switches[0].error


def test_progress_and_save(tmp_path):
    messages = []
    plan = load_test(config("switch1.json"))
    report = run_test(plan, {"switch1": OfflineCollector(sample("switch1_faults.log"))},
                      on_progress=messages.append)
    assert messages[-1] == "Done: FAIL"
    json_path, txt_path = report.save(str(tmp_path), site="Site 7")
    assert os.path.basename(json_path).startswith("site-7_switch-1_")
    data = json.load(open(json_path))
    assert data["status"] == "fail"
    assert data["counts"]["fail"] == 5
    assert "Fix:" in open(txt_path).read()


def test_cli_exit_codes(tmp_path, capsys):
    assert cli_main([config("switch1.json"), "--offline", sample("switch1_good.log")]) == 0
    assert cli_main([config("switch1.json"), "--offline", sample("switch1_faults.log"),
                     "--save", str(tmp_path)]) == 2
    assert len(os.listdir(tmp_path)) == 2
    assert cli_main([config("combined.json"),
                     "--offline", "switch1=" + sample("combined_switch1.log"),
                     "--offline", "switch2=" + sample("combined_switch2.log"),
                     "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["status"] == "pass"
