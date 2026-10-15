"""Load wiring configs and turn them into per-port expectations.

A *test* is what the tech runs: Switch 1 alone, Switch 2 alone, or both
switches connected. Single-switch tests come straight from ``switch1.json`` /
``switch2.json``; the combined test comes from ``combined.json``, which pulls
in both switch configs and overlays the interconnect expectations on the ports
at each end.
"""

import copy
import json
import os
from dataclasses import dataclass, field

from .interfaces import drawing_label, normalize_interface

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema")


class ConfigError(Exception):
    """A config file is missing, unreadable or doesn't match the schema."""


@dataclass
class PortExpectation:
    """Everything the checker needs to know about one cabled port."""

    interface: str
    diagram_label: str
    media: str
    connects_to: dict
    expect: dict
    optic: dict = None
    note: str = None
    # Set in combined mode for the two ends of a switch-to-switch link.
    peer: dict = None
    # Set for an interconnect still marked "placeholder" in combined.json.
    placeholder: bool = False

    @property
    def target(self):
        """Plain-English name of what this port should be cabled to."""
        dev = self.connects_to.get("device") or "the device on the drawing"
        port = self.connects_to.get("port")
        return "%s %s" % (dev, port) if port else dev


@dataclass
class SwitchPlan:
    id: str
    name: str
    model: str
    unlisted_ports: str
    ports: list = field(default_factory=list)

    def port(self, interface):
        interface = normalize_interface(interface)
        for p in self.ports:
            if p.interface == interface:
                return p
        return None


@dataclass
class TestPlan:
    """A loaded test: one switch (single mode) or two (combined mode)."""

    mode: str          # "single", "combined" or "jack_test"
    name: str
    path: str
    switches: list     # [SwitchPlan]
    jack_test: object = None   # JackTestPlan, for mode "jack_test"

    def switch(self, switch_id):
        for s in self.switches:
            if s.id == switch_id:
                return s
        raise KeyError(switch_id)


@dataclass
class Jack:
    """One bulkhead jack in a jack test: which switch port and test-switch port it joins."""

    jack: str
    switch_port: str
    test_switch_port: str
    control: bool = False
    note: str = None


@dataclass
class JackTestPlan:
    """A bulkhead jack test: the test switch turns ports on and off one at a time."""

    switch_id: str
    test_switch_id: str
    jacks: list                 # [Jack], control jack last
    also_shut: list = field(default_factory=list)
    speed_mbps: int = 1000
    duplex: str = "full"
    link_timeout: float = 15
    note: str = None

    @property
    def control(self):
        return next((j for j in self.jacks if j.control), None)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ConfigError("Config file not found: %s" % path)
    except json.JSONDecodeError as e:
        raise ConfigError("%s is not valid JSON (line %d): %s" % (path, e.lineno, e.msg))


def _validate(data, schema_name, path):
    """Validate against the JSON schema when ``jsonschema`` is installed."""
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:  # validation is a nice-to-have, not a requirement
        return
    schemas = {}
    for name in ("switch-config.schema.json", "combined-config.schema.json",
                 "jack-test.schema.json"):
        with open(os.path.join(SCHEMA_DIR, name), encoding="utf-8") as f:
            schemas[name] = json.load(f)
    registry = Registry().with_resources(
        (name, Resource.from_contents(s)) for name, s in schemas.items())
    validator = jsonschema.Draft202012Validator(schemas[schema_name], registry=registry)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "(top level)"
        raise ConfigError("%s does not match the config format at %s: %s"
                          % (os.path.basename(path), where, e.message))


def _merge_expect(*layers):
    merged = {}
    for layer in layers:
        if layer:
            merged.update(copy.deepcopy(layer))
    return merged


def _load_switch(path):
    data = _read_json(path)
    _validate(data, "switch-config.schema.json", path)
    try:
        return _build_switch(data)
    except (KeyError, TypeError) as e:
        # Only reachable when jsonschema isn't installed to catch it earlier.
        raise ConfigError("%s is missing a required setting: %s"
                          % (os.path.basename(path), e))


def _build_switch(data):
    defaults = data.get("defaults", {})
    sw = data["switch"]
    plan = SwitchPlan(id=sw["id"], name=sw["name"], model=sw.get("model", ""),
                      unlisted_ports=data.get("unlisted_ports", "ignore"))
    for p in data["ports"]:
        media = p["media"]
        expect = _merge_expect(defaults.get(media, {}).get("expect"), p.get("expect"))
        expect.setdefault("link", "up")
        plan.ports.append(PortExpectation(
            interface=normalize_interface(p["interface"]),
            diagram_label=p.get("diagram_label") or p["interface"],
            media=media,
            connects_to=p["connects_to"],
            expect=expect,
            optic=p.get("optic"),
            note=p.get("note"),
        ))
    return plan


def load_test(path):
    """Load a switch config or a combined config and return a :class:`TestPlan`."""
    path = os.path.abspath(path)
    data = _read_json(path)
    if data.get("test") == "combined":
        return _load_combined(path, data)
    if data.get("test") == "jack_test":
        return _load_jack_test(path, data)
    plan = _load_switch(path)
    return TestPlan(mode="single", name=plan.name, path=path, switches=[plan])


def _load_combined(path, data):
    _validate(data, "combined-config.schema.json", path)
    base = os.path.dirname(path)
    switches = {}
    for entry in data["switches"]:
        sw = _load_switch(os.path.join(base, entry["config"]))
        if sw.id != entry["id"]:
            raise ConfigError("%s says switch id %r but %s calls it %r"
                              % (os.path.basename(path), entry["id"], entry["config"], sw.id))
        switches[sw.id] = sw

    names = {sid: sw.name for sid, sw in switches.items()}
    for link in data.get("interconnects", []):
        ends = [link["a"], link["b"]]
        for here, there in (ends, ends[::-1]):
            sw = switches.get(here["switch"])
            if sw is None:
                raise ConfigError("Interconnect refers to unknown switch %r" % here["switch"])
            if not here.get("interface"):
                continue
            port = sw.port(here["interface"])
            peer = {
                "switch": there["switch"],
                "switch_name": names.get(there["switch"], there["switch"]),
                "interface": normalize_interface(there["interface"]) if there.get("interface") else None,
                "path": link.get("path", []),
                "note": link.get("note"),
            }
            if port is None:
                # Interconnect on a port the switch config doesn't list: add it.
                port = PortExpectation(
                    interface=normalize_interface(here["interface"]),
                    diagram_label=here["interface"],
                    media=link.get("media") or "copper",
                    connects_to={"device": peer["switch_name"],
                                 "port": drawing_label(peer["interface"]) if peer["interface"] else None,
                                 "kind": "switch"},
                    expect={},
                )
                sw.ports.append(port)
            port.peer = peer
            if link.get("placeholder"):
                port.placeholder = True
                continue
            # The interconnect's expect replaces the standalone expect, but
            # keep standalone values it doesn't mention (e.g. light levels).
            port.expect = _merge_expect(port.expect, link.get("expect"))
            port.expect.setdefault("link", "up")
            port.connects_to = {"device": peer["switch_name"],
                                "port": drawing_label(peer["interface"]) if peer["interface"] else None,
                                "kind": "switch",
                                "via": port.connects_to}

    return TestPlan(mode="combined", name=data.get("name", "Combined"), path=path,
                    switches=[switches[e["id"]] for e in data["switches"]])


def _load_jack_test(path, data):
    _validate(data, "jack-test.schema.json", path)
    try:
        sw, ts = data["switch"], data["test_switch"]
        expect = data.get("expect", {})
        jacks = [Jack(jack=j["jack"], switch_port=normalize_interface(j["switch_port"]),
                      test_switch_port=normalize_interface(j["test_switch_port"]),
                      control=bool(j.get("control")), note=j.get("note"))
                 for j in data["jacks"]]
    except (KeyError, TypeError) as e:
        raise ConfigError("%s is missing a required setting: %s" % (os.path.basename(path), e))
    if sum(j.control for j in jacks) > 1:
        raise ConfigError("%s marks more than one jack as the control link."
                          % os.path.basename(path))
    if sw["id"] == ts["id"]:
        raise ConfigError("%s gives the switch and the test switch the same id."
                          % os.path.basename(path))
    for attr in ("switch_port", "test_switch_port"):
        seen = [getattr(j, attr) for j in jacks]
        dupes = sorted({p for p in seen if seen.count(p) > 1})
        if dupes:
            raise ConfigError("%s uses %s for more than one jack."
                              % (os.path.basename(path), dupes[0]))
    jacks.sort(key=lambda j: j.control)   # control link is tested last
    jt = JackTestPlan(
        switch_id=sw["id"], test_switch_id=ts["id"], jacks=jacks,
        also_shut=[normalize_interface(p) for p in ts.get("also_shut", [])],
        speed_mbps=expect.get("speed_mbps", 1000), duplex=expect.get("duplex", "full"),
        link_timeout=data.get("link_timeout_seconds", 15), note=data.get("note"))
    switches = [SwitchPlan(id=sw["id"], name=sw["name"], model="", unlisted_ports="ignore"),
                SwitchPlan(id=ts["id"], name=ts["name"], model="", unlisted_ports="ignore")]
    return TestPlan(mode="jack_test", name=data.get("name", "Bulkhead jacks"), path=path,
                    switches=switches, jack_test=jt)


def find_tests(site_dir):
    """List the tests available in a site folder, for a GUI picker.

    Returns ``[(label, path)]`` with single-switch tests first, then combined,
    then bulkhead jack tests.
    """
    singles, combined, jacks = [], [], []
    for fn in sorted(os.listdir(site_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(site_dir, fn)
        try:
            data = _read_json(path)
        except ConfigError:
            continue
        if data.get("test") == "combined":
            combined.append((data.get("name", "Combined"), path))
        elif data.get("test") == "jack_test":
            jacks.append((data.get("name", "Bulkhead jacks"), path))
        elif "switch" in data and "ports" in data:
            singles.append((data["switch"].get("name", fn), path))
    return singles + combined + jacks
