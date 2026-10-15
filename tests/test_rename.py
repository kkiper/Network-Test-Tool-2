import json
import os
import shutil

import pytest

from nettest import find_tests, load_test
from nettest import rename

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "..", "configs", "standard-build")


@pytest.fixture
def site(tmp_path):
    dst = tmp_path / "site"
    shutil.copytree(SITE, dst)
    return str(dst)


def _read(site, fn):
    with open(os.path.join(site, fn), encoding="utf-8") as f:
        return f.read()


def test_lists_current_names(site):
    assert rename.switch_names(site) == [("switch1", "Switch 1", "switch1.json"),
                                         ("switch2", "Switch 2", "switch2.json")]


def test_rename_updates_names_everywhere(site):
    changed = rename.rename_switches(site, {"switch1": "Rack A core", "switch2": "Rack B"})
    assert sorted(os.path.basename(p) for p in changed) == [
        "combined.json", "switch1.json", "switch2.json"]
    labels = [label for label, _ in find_tests(site)]
    assert labels == ["Rack A core", "Rack B", "Rack A core + Rack B connected"]
    combined = load_test(os.path.join(site, "combined.json"))
    sw1 = combined.switches[0]
    assert sw1.name == "Rack A core"
    assert sw1.port("GigabitEthernet1/4").connects_to["device"] == "Rack B"
    note = json.loads(_read(site, "combined.json"))["interconnects"][0]["note"]
    assert note.startswith("Rack B GE 1/5") and note.endswith("Rack A core GI 1/4.")
    # Ids and the test switch are left alone.
    assert '"id": "switch1"' in _read(site, "switch1.json")
    assert '"name": "Test switch"' in _read(site, "combined.json")


def test_unchanged_text_is_byte_identical(site):
    before = _read(site, "switch1.json")
    rename.rename_switches(site, {"switch2": "Rack B"})
    assert _read(site, "switch1.json") == before


def test_swapping_names_works(site):
    rename.rename_switches(site, {"switch1": "Switch 2", "switch2": "Switch 1"})
    assert json.loads(_read(site, "combined.json"))["name"] == "Switch 2 + Switch 1 connected"


def test_whole_words_only(site):
    path = os.path.join(site, "switch1.json")
    data = json.loads(_read(site, "switch1.json"))
    data["ports"][0]["note"] = "Not Switch 10 or Switch 1a, but Switch 1."
    rename._write_json(path, data)
    rename.rename_switches(site, {"switch1": "Core"})
    assert json.loads(_read(site, "switch1.json"))["ports"][0]["note"] == \
        "Not Switch 10 or Switch 1a, but Core."


@pytest.mark.parametrize("names, msg", [
    ({"switch1": "   "}, "can't be blank"),
    ({"switch1": "x" * (rename.NAME_MAX + 1)}, "characters or fewer"),
    ({"switch1": "Switch 2"}, "can't both be called"),
    ({"switch9": "Rack"}, "no switch called"),
])
def test_bad_names_change_nothing(site, names, msg):
    before = {fn: _read(site, fn) for fn in os.listdir(site)}
    with pytest.raises(ValueError, match=msg):
        rename.rename_switches(site, names)
    assert {fn: _read(site, fn) for fn in os.listdir(site)} == before


def test_extra_spaces_are_tidied(site):
    rename.rename_switches(site, {"switch1": "  Rack   A\n"})
    assert rename.switch_names(site)[0][1] == "Rack A"


def test_command_line(site, capsys):
    assert rename.main([site, "switch1=Rack A", "switch2=Rack B"]) == 0
    assert "Updated 3 file(s)." in capsys.readouterr().out
    assert rename.main([site, "switch1=Rack B"]) == 2
    assert "Nothing was renamed" in capsys.readouterr().err


def test_command_line_lists_names_and_usage(site, capsys):
    assert rename.main([site]) == 0
    assert "switch1    Switch 1  (switch1.json)" in capsys.readouterr().out
    assert rename.main([]) == 2
    assert "Usage:" in capsys.readouterr().out
