import os

import pytest

from nettest import OfflineCollector, SshCollector, Status, load_test, run_test
from nettest import gui_logic as logic

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIGS = os.path.join(os.path.dirname(HERE), "configs", "standard-build")
SAMPLES = os.path.join(HERE, "samples")


def faults_report():
    plan = load_test(os.path.join(CONFIGS, "switch1.json"))
    return run_test(plan, {"switch1": OfflineCollector(os.path.join(SAMPLES, "switch1_faults.log"))})


def test_list_sites_finds_standard_build():
    assert "standard-build" in logic.list_sites(os.path.join(os.path.dirname(HERE), "configs"))


def test_sorted_rows_puts_problems_first_in_config_order():
    rows = logic.sorted_rows(faults_report())
    assert [r.diagram_label for r in rows[:5]] == ["GI 1/5", "GI 1/9", "GI 1/10", "TE 1/1", "TE 1/2"]
    assert [r.status for r in rows[:5]] == [Status.FAIL] * 5
    assert all(r.status == Status.PASS for r in rows[5:])


def test_details_text_includes_fix_and_what_was_read():
    rows = {r.diagram_label: r for r in faults_report().ports()}
    text = logic.details_text(rows["GI 1/5"])
    assert "FAIL  GI 1/5 has no link." in text
    assert "How to fix: Check the cable from GI 1/5 to Server Port 2" in text
    assert "switch says 'notconnect'" in text
    assert "light Tx -2.4 / Rx -14.6 dBm" in logic.details_text(rows["TE 1/1"])


def test_counts_text():
    assert logic.counts_text({"fail": 4, "warn": 1, "pass": 13}) == \
        "4 failed, 1 warning, 13 passed"
    assert logic.counts_text({"fail": 0, "warn": 2, "pass": 1, "not_configured": 1}) == \
        "0 failed, 2 warnings, 1 passed, 1 not set up"


def test_build_collectors_live_and_offline():
    single = load_test(os.path.join(CONFIGS, "switch1.json"))
    c = logic.build_collectors(single, False, {"switch1": " 192.168.1.1 "}, "admin", "pw")
    assert isinstance(c["switch1"], SshCollector) and c["switch1"].host == "192.168.1.1"
    sample = os.path.join(SAMPLES, "switch1_good.log")
    c = logic.build_collectors(single, True, {"switch1": sample}, "", "")
    assert isinstance(c["switch1"], OfflineCollector)


@pytest.mark.parametrize("offline,addresses,user,pw,message", [
    (False, {}, "admin", "pw", "Enter the IP address for Switch 1"),
    (False, {"switch1": "10.0.0.1"}, "", "pw", "Enter the switch username"),
    (False, {"switch1": "10.0.0.1"}, "admin", "", "Enter the switch password"),
    (True, {}, "", "", "Choose the saved output file for Switch 1"),
    (True, {"switch1": "/no/such/file.log"}, "", "", "Can't find the saved output"),
])
def test_build_collectors_explains_what_is_missing(offline, addresses, user, pw, message):
    plan = load_test(os.path.join(CONFIGS, "switch1.json"))
    with pytest.raises(logic.FormError, match=message):
        logic.build_collectors(plan, offline, addresses, user, pw)


def test_combined_needs_both_switches():
    plan = load_test(os.path.join(CONFIGS, "combined.json"))
    with pytest.raises(logic.FormError, match="Switch 2"):
        logic.build_collectors(plan, False, {"switch1": "10.0.0.1"}, "admin", "pw")


def test_settings_never_store_passwords(tmp_path):
    path = str(tmp_path / "s.json")
    logic.save_settings({"site": "standard-build", "password": "x", "enable_password": "y"},
                        path)
    assert logic.load_settings(path) == {"site": "standard-build"}
    assert logic.load_settings(str(tmp_path / "missing.json")) == {}


def test_check_serials():
    from nettest import load_test
    plan = load_test(os.path.join(logic.configs_dir(), "standard-build", "combined.json"))
    assert logic.check_serials(plan, {"switch1": " fcw2233a1b ", "switch2": "FCW-1234"}) == \
        {"switch1": "FCW2233A1B", "switch2": "FCW-1234"}
    with pytest.raises(logic.FormError, match="serial number for Switch 2"):
        logic.check_serials(plan, {"switch1": "FCW2233A1B"})
    with pytest.raises(logic.FormError, match="longer than 10"):
        logic.check_serials(plan, {"switch1": "FCW2233A1BX", "switch2": "A"})
    with pytest.raises(logic.FormError, match="letters, numbers"):
        logic.check_serials(plan, {"switch1": "FCW 2233", "switch2": "A"})
    assert logic.serial_fits("ABCDEFGHIJ") and not logic.serial_fits("ABCDEFGHIJK")
