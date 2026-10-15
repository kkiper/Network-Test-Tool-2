"""Compare what a switch reports against what the wiring config expects.

Every port gets a :class:`PortResult` with an overall status and a list of
:class:`Finding` objects. Each finding has a plain-English ``message`` (what
is wrong) and ``action`` (what the tech should do about it), written for
someone who has never seen IOS output.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum

from .interfaces import drawing_label, normalize_mac


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    NOT_CONFIGURED = "not_configured"

    @property
    def rank(self):
        return {"pass": 0, "not_configured": 1, "warn": 2, "fail": 3}[self.value]


def worst(statuses, default=Status.PASS):
    statuses = list(statuses)
    return max(statuses, key=lambda s: s.rank) if statuses else default


@dataclass
class Finding:
    status: Status
    check: str            # "link", "speed", "duplex", "neighbor", "mac", "optic", "tx", "rx", "unlisted"
    message: str
    action: str = None

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class PortResult:
    switch_id: str
    switch_name: str
    interface: str
    diagram_label: str
    target: str
    media: str
    findings: list = field(default_factory=list)
    observed: dict = field(default_factory=dict)
    note: str = None
    listed: bool = True   # False for ports that aren't in the config

    @property
    def status(self):
        return worst(f.status for f in self.findings)

    @property
    def ip(self):
        """IP address(es) seen on the port, for display ("" if none)."""
        return ", ".join((self.observed or {}).get("ips") or [])

    def neighbors(self):
        """``[(device_id, port_id)]`` of CDP/LLDP neighbors, without repeats."""
        seen = []
        for n in (self.observed or {}).get("neighbors") or []:
            pair = (n.get("device_id") or "",
                    drawing_label(n["remote_port"]) if n.get("remote_port") else "")
            if pair not in seen:   # CDP and LLDP often report the same neighbor
                seen.append(pair)
        return seen

    @property
    def neighbor_device(self):
        """Device ID(s) of CDP/LLDP neighbors on the port, for display ("" if none)."""
        return ", ".join(d for d, _ in self.neighbors())

    @property
    def neighbor_port(self):
        """The neighbor's own port ID(s), e.g. ``GI 1/5`` ("" if none)."""
        return ", ".join(p for _, p in self.neighbors())

    @property
    def summary(self):
        """The single most important line, for a one-row-per-port list."""
        if not self.findings:
            return "OK"
        top = worst(f.status for f in self.findings)
        if top == Status.PASS:
            return " ".join(f.message for f in self.findings)
        return next(f.message for f in self.findings if f.status == top)

    def to_dict(self):
        return {
            "switch_id": self.switch_id,
            "switch_name": self.switch_name,
            "interface": self.interface,
            "diagram_label": self.diagram_label,
            "target": self.target,
            "ip": self.ip,
            "neighbor_device": self.neighbor_device,
            "neighbor_port": self.neighbor_port,
            "media": self.media,
            "listed": self.listed,
            "status": self.status.value,
            "summary": self.summary,
            "note": self.note,
            "findings": [f.to_dict() for f in self.findings],
            "observed": self.observed,
        }


# --------------------------------------------------------------------------

_UP = {"connected"}
_ABSENT = {"sfpabsent", "xcvrabsen"}

# Ports inside the switch that are always up and never cabled, e.g. the
# ESS 3300's app-hosting port (Ap1/1). Never reported as unlisted.
INTERNAL_PORT_PREFIXES = ("AppGigabitEthernet",)

NO_LIGHT_DBM = -30.0   # at or below this, treat Rx as "no light at all"


def _link_state(status_row):
    if status_row is None:
        return "missing"
    s = status_row["status"].lower()
    if s in _UP:
        return "up"
    if s == "disabled":
        return "admin-down"
    if s == "err-disabled":
        return "err-disabled"
    if s in _ABSENT:
        return "no-sfp"
    if s in ("notconnect", "notconnec", "down"):
        return "down"
    return s


def _short_host(name):
    return (name or "").split(".")[0].lower()


class SwitchChecker:
    """Checks one switch's :class:`~nettest.collectors.Snapshot` against its plan."""

    def __init__(self, switch_plan, snapshot, peer_hostnames=None):
        self.plan = switch_plan
        self.snap = snapshot
        # switch id -> hostname read from that switch, for combined mode.
        self.peer_hostnames = peer_hostnames or {}

    # ---- entry point ---------------------------------------------------

    def run(self):
        if not self.snap.has("status"):
            reason = self.snap.unavailable.get("show interfaces status", "no output")
            raise ValueError("Couldn't read port status from %s (%s)" % (self.plan.name, reason))
        results = [r for r in (self.check_port(p) for p in self.plan.ports) if r is not None]
        results += self.check_unlisted()
        self._add_cross_port_hints(results)
        return results

    # ---- one port ------------------------------------------------------

    def check_port(self, port):
        label = port.diagram_label
        row = self.snap.status.get(port.interface)
        result = PortResult(
            switch_id=self.plan.id, switch_name=self.plan.name,
            interface=port.interface, diagram_label=label, target=port.target,
            media=port.media, note=port.note,
            observed=self._observed(port.interface),
        )
        add = result.findings.append

        if port.placeholder:
            add(Finding(Status.NOT_CONFIGURED, "config",
                        "%s: the link to %s isn't filled in the config yet, so it wasn't tested."
                        % (label, port.peer["switch_name"] if port.peer else "the other switch"),
                        "Ask whoever maintains the configs to finish this entry in combined.json."))
            return result

        unused = port.connects_to.get("kind") == "unused"
        if row is None and unused:
            return None  # e.g. an expansion-module port on a switch without the module
        if row is None:
            add(Finding(Status.FAIL, "link",
                        "%s (%s) doesn't exist on %s." % (label, port.interface, self.plan.name),
                        "Check that you connected to the right switch, and that the config "
                        "file uses the right port name."))
            return result

        expect = port.expect
        state = _link_state(row)
        want = expect.get("link", "up")

        self._check_link(result, port, state, want)

        if state == "up":
            self._check_speed_duplex(result, port, row)
            self._check_neighbor(result, port)
            self._check_macs(result, port)
            self._check_ip(result, port)
        if port.media == "fiber" and not unused:
            self._check_optic(result, port, row, state)

        if not result.findings:
            add(Finding(Status.PASS, "link", "%s is OK." % label))
        return result

    # ---- individual checks --------------------------------------------

    def _check_link(self, result, port, state, want):
        add = result.findings.append
        label, target = port.diagram_label, port.target
        bulkhead = port.connects_to.get("kind") == "bulkhead"
        via = port.connects_to.get("via")
        path = (port.peer or {}).get("path") or []

        if state == "up":
            if want == "down":
                add(Finding(Status.FAIL, "unused",
                            "%s has a link, but nothing should be plugged into it%s."
                            % (label, self._describe_far_end(port.interface)),
                            "Check whether a cable meant for another port was plugged into "
                            "%s by mistake, and unplug it." % label))
            else:
                row = self.snap.status[port.interface]
                add(Finding(Status.PASS, "link", "%s has link (%s, %s)."
                            % (label, _speed_text(row["speed_mbps"]), row["duplex"] or "auto")))
            return

        if want == "down" and state in ("admin-down", "no-sfp", "down"):
            add(Finding(Status.PASS, "link", "%s is empty, as expected." % label))
            return
        if state == "admin-down":
            add(Finding(Status.FAIL if want == "up" else Status.WARN, "link",
                        "%s is turned off in the switch configuration (shutdown)." % label,
                        "The switch config needs 'no shutdown' on %s. Ask the network "
                        "engineer; this isn't a cabling problem." % port.interface))
            return
        if state == "err-disabled":
            add(Finding(Status.FAIL, "link",
                        "%s was shut down by the switch because of an error (err-disabled)."
                        % label,
                        "This usually means a loop or a security violation. Check the cable "
                        "goes to %s and not back into this switch, then ask the network "
                        "engineer to reset the port." % target))
            return
        if state == "no-sfp":
            # The optic check reports this in detail.
            if want == "up":
                add(Finding(Status.FAIL, "link", "%s has no link because no SFP module "
                            "is installed." % label,
                            "Insert the SFP module into %s." % label))
            return
        if state not in ("down",):
            add(Finding(Status.FAIL if want == "up" else Status.WARN, "link",
                        "%s is in an unusual state: %s." % (label, state),
                        "Ask the network engineer to look at %s." % port.interface))
            return

        # Plain "no link".
        if want == "up":
            if port.peer:
                hops = (" through bulkheads %s" % " and ".join(path)) if path else ""
                action = ("Check the cable from %s on %s to %s on %s%s. Make sure every "
                          "patch cable along the way is clicked in."
                          % (label, self.plan.name, _label(port.peer["interface"]),
                             port.peer["switch_name"], hops))
            elif bulkhead:
                action = ("Check the cable from %s to bulkhead %s, and that something is "
                          "plugged into the outside of the bulkhead." % (label, target))
            else:
                action = ("Check the cable from %s to %s, and that %s is powered on."
                          % (label, target, port.connects_to.get("device", "the device")))
            add(Finding(Status.FAIL, "link", "%s has no link." % label, action))
        elif want == "optional":
            why = ("nothing may be plugged into the outside of bulkhead %s yet"
                   % (port.connects_to.get("port") or target)) if bulkhead or via \
                else "this port is allowed to be empty"
            add(Finding(Status.PASS, "link",
                        "%s has no link. That's OK here because %s." % (label, why)))
        else:  # want == "down"
            add(Finding(Status.PASS, "link", "%s is empty, as expected." % label))

    def _check_speed_duplex(self, result, port, row):
        add = result.findings.append
        label = port.diagram_label
        want_speed = port.expect.get("speed_mbps")
        got_speed = row["speed_mbps"]
        if want_speed and got_speed and got_speed != want_speed:
            if port.media == "copper" and got_speed < want_speed:
                action = ("A link that comes up slower than it should usually means a "
                          "damaged cable or a bad termination (one of the 8 wires isn't "
                          "making contact). Try a known-good patch cable to %s; if that "
                          "fixes it, re-terminate or replace the run." % port.target)
            else:
                action = ("Check that %s is the right device and its port is set to "
                          "auto-negotiate." % port.target)
            add(Finding(Status.WARN, "speed",
                        "%s is running at %s, but should be %s."
                        % (label, _speed_text(got_speed), _speed_text(want_speed)), action))
        want_duplex = port.expect.get("duplex")
        got_duplex = row["duplex"]
        if want_duplex and got_duplex and got_duplex != want_duplex:
            add(Finding(Status.WARN, "duplex",
                        "%s is running %s duplex, but should be %s duplex."
                        % (label, got_duplex, want_duplex),
                        "Half duplex usually means the device on the other end has its "
                        "speed/duplex hard-set. Set %s to auto-negotiate, or try another "
                        "cable." % port.target))

    def _check_neighbor(self, result, port):
        want = port.expect.get("cdp_neighbor")
        if not want:
            return
        add = result.findings.append
        label = port.diagram_label
        if not self.snap.has("cdp") and not self.snap.has("lldp"):
            add(Finding(Status.WARN, "neighbor",
                        "Couldn't check what's on the other end of %s: CDP and LLDP are "
                        "both off on %s." % (label, self.plan.name),
                        "Ask the network engineer to enable CDP ('cdp run') so this can "
                        "be checked."))
            return
        neighbors = self.snap.neighbors(port.interface)
        peer = port.peer or {}
        expected_name = want if isinstance(want, str) else None
        if not expected_name and peer:
            expected_name = self.peer_hostnames.get(peer["switch"])
        who = peer.get("switch_name") or expected_name or "another switch"

        if not neighbors:
            add(Finding(Status.FAIL, "neighbor",
                        "%s has link, but %s can't see %s on it." % (label, self.plan.name, who),
                        "Something else is plugged in, or the link goes to the wrong place. "
                        "Trace the cable from %s and check it ends at %s%s."
                        % (label, who,
                           " port %s" % _label(peer["interface"]) if peer.get("interface") else "")))
            return

        def name_ok(n):
            return not expected_name or _short_host(n["device_id"]) == _short_host(expected_name)

        def port_ok(n):
            return not peer.get("interface") or n.get("remote_port") is None \
                or n["remote_port"] == peer["interface"]

        if any(name_ok(n) and port_ok(n) for n in neighbors):
            n = next(n for n in neighbors if name_ok(n) and port_ok(n))
            add(Finding(Status.PASS, "neighbor", "%s sees %s on port %s."
                        % (label, n["device_id"], _label(n.get("remote_port")) or "?")))
            return

        n = next((n for n in neighbors if name_ok(n)), None)
        if n is not None:
            add(Finding(Status.FAIL, "neighbor",
                        "%s goes to port %s on %s, but should go to port %s."
                        % (label, _label(n["remote_port"]), who, _label(peer["interface"])),
                        "Move the cable on %s from %s to %s."
                        % (who, _label(n["remote_port"]), _label(peer["interface"]))))
            return
        n = neighbors[0]
        add(Finding(Status.FAIL, "neighbor",
                    "%s is connected to %s (port %s), not %s."
                    % (label, n["device_id"], _label(n.get("remote_port")) or "?", who),
                    "Trace the cable from %s; it ends at the wrong device." % label))

    def _check_macs(self, result, port):
        wanted = [normalize_mac(m) for m in port.expect.get("mac_addresses") or []]
        if not wanted:
            return
        add = result.findings.append
        label = port.diagram_label
        if not self.snap.has("macs"):
            add(Finding(Status.WARN, "mac", "Couldn't read the MAC address table, so the "
                        "device on %s wasn't identified." % label))
            return
        seen = {e["mac"] for e in self.snap.macs.get(port.interface, [])}
        for mac in wanted:
            if mac in seen:
                add(Finding(Status.PASS, "mac", "%s sees the expected device (%s)." % (label, mac)))
                continue
            elsewhere = [i for i, entries in self.snap.macs.items()
                         if any(e["mac"] == mac for e in entries)]
            if elsewhere:
                other = self.plan.port(elsewhere[0])
                other_label = other.diagram_label if other else drawing_label(elsewhere[0])
                add(Finding(Status.FAIL, "mac",
                            "The device that belongs on %s (%s) is plugged into %s instead."
                            % (label, mac, other_label),
                            "The cables for %s and %s are probably swapped. Swap them "
                            "back." % (label, other_label)))
            else:
                add(Finding(Status.FAIL, "mac",
                            "%s has link, but the expected device (%s) isn't on it." % (label, mac),
                            "A different device is plugged in, or %s hasn't sent any traffic "
                            "yet. Check the cable goes to %s." % (port.target, port.target)))

    def _check_ip(self, result, port):
        """Compare the device's IP with the one in the config, if one is listed.

        Never fails a port: a matching IP passes, a different one warns, and a
        port with no ``ip_address`` (IP not known yet) isn't checked.
        """
        want_ip = port.expect.get("ip_address")
        if not want_ip or port.connects_to.get("kind") == "switch":
            return
        ips = result.observed.get("ips") or []
        if not ips:
            return  # the switch hasn't seen an IP for this device; nothing to compare
        label = port.diagram_label
        if want_ip in ips:
            result.findings.append(Finding(
                Status.PASS, "ip", "%s's device has the expected IP (%s)." % (label, want_ip)))
        else:
            result.findings.append(Finding(
                Status.WARN, "ip", "%s's device has IP %s, but the drawing says %s."
                % (label, ", ".join(ips), want_ip),
                "Check that %s is set to %s, and that the right device is plugged "
                "into %s." % (port.target, want_ip, label)))

    def _check_optic(self, result, port, row, state):
        add = result.findings.append
        label = port.diagram_label
        optic = port.optic or {}
        part = optic.get("part_number") or "the SFP"

        if state == "no-sfp" or (row and "not present" in row.get("type", "").lower()):
            if not any(f.check == "link" and f.status == Status.FAIL for f in result.findings):
                add(Finding(Status.FAIL, "optic", "%s has no SFP module installed." % label,
                            "Insert an SFP module (%s) into %s." % (part, label)))
            return

        # Type check from 'show interfaces status' (e.g. "SFP-10GBase-SR").
        want_type = _alnum(optic.get("type"))
        got_type = row.get("type", "") if row else ""
        if want_type and got_type and want_type not in _alnum(got_type):
            add(Finding(Status.WARN, "optic",
                        "%s has a %s module, but the drawing calls for %s (%s)."
                        % (label, got_type, optic.get("type"), part),
                        "Check the SFP part number on the module's label."))

        levels = port.expect.get("light_levels_dbm")
        if not levels:
            return
        if not self.snap.has("transceivers"):
            add(Finding(Status.WARN, "optic",
                        "Couldn't read light levels for %s." % label,
                        "Light levels weren't collected from the switch; re-run the test."))
            return
        dom = self.snap.transceivers.get(port.interface)
        if not dom or ("tx_dbm" not in dom and "rx_dbm" not in dom):
            add(Finding(Status.WARN, "optic",
                        "%s's SFP doesn't report light levels." % label,
                        "The module may not support monitoring (DOM). Light levels can't "
                        "be checked on this port."))
            return
        use_module = levels.get("use_module_thresholds", True)
        self._check_level(result, port, "tx", dom.get("tx_dbm"), levels.get("tx"), use_module)
        if state == "up":
            self._check_level(result, port, "rx", dom.get("rx_dbm"), levels.get("rx"), use_module)
        else:
            rx = (dom.get("rx_dbm") or {}).get("value")
            if rx is not None:
                result.observed["rx_dbm"] = rx

    def _check_level(self, result, port, which, reading, cfg_range, use_module):
        add = result.findings.append
        label = port.diagram_label
        name = "transmit" if which == "tx" else "receive"
        if reading is None or reading.get("value") is None:
            add(Finding(Status.WARN, which, "%s didn't report a %s light level." % (label, name)))
            return
        value = reading["value"]
        result.observed["%s_dbm" % which] = value

        status, low, high, source = Status.PASS, None, None, None
        th = reading
        if use_module and th.get("low_alarm") is not None and th.get("high_alarm") is not None:
            source = "the SFP's own alarm level"
            if value <= th["low_alarm"] or reading.get("flag") == "--":
                status, low = Status.FAIL, th["low_alarm"]
            elif value >= th["high_alarm"] or reading.get("flag") == "++":
                status, high = Status.FAIL, th["high_alarm"]
            elif th.get("low_warn") is not None and value <= th["low_warn"]:
                status, low, source = Status.WARN, th["low_warn"], "the SFP's own warning level"
            elif th.get("high_warn") is not None and value >= th["high_warn"]:
                status, high, source = Status.WARN, th["high_warn"], "the SFP's own warning level"
        elif cfg_range:
            source = "the datasheet range in the config"
            if value < cfg_range["min"]:
                status, low = Status.FAIL, cfg_range["min"]
            elif value > cfg_range["max"]:
                status, high = Status.FAIL, cfg_range["max"]

        if status == Status.PASS:
            add(Finding(Status.PASS, which, "%s %s light is %.1f dBm, which is normal."
                        % (label, name, value)))
            return

        if which == "rx" and value <= NO_LIGHT_DBM:
            add(Finding(Status.FAIL, which,
                        "%s is receiving no light (%.1f dBm)." % (label, value),
                        "The fiber isn't connected at the far end, or the two strands are "
                        "swapped. Check the fiber from %s to %s and try swapping the "
                        "strands at one end." % (label, port.target)))
            return

        bound = "below %s of %.1f dBm" % (source, low) if low is not None \
            else "above %s of %.1f dBm" % (source, high)
        msg = "%s %s light is %.1f dBm, %s." % (label, name, value, bound)
        if which == "rx" and low is not None:
            action = ("Too little light is getting through. Clean both ends of the fiber "
                      "and the bulkhead with a fiber cleaner, make sure each connector "
                      "clicks in, and check the fiber isn't kinked. Then re-test.")
        elif which == "rx":
            action = ("Too much light. This is unusual on multimode fiber; check the SFP at "
                      "the far end is the right type.")
        else:
            action = "The SFP in %s may be failing. Replace it with a spare %s." % (
                label, (port.optic or {}).get("part_number", "module"))
        add(Finding(status, which, msg, action))

    # ---- ports not in the config --------------------------------------

    def check_unlisted(self):
        policy = self.plan.unlisted_ports
        if policy == "ignore":
            return []
        listed = {p.interface for p in self.plan.ports}
        results = []
        for iface, row in self.snap.status.items():
            if iface in listed or _link_state(row) != "up" \
                    or iface.startswith(INTERNAL_PORT_PREFIXES):
                continue
            label = drawing_label(iface)
            status = Status.FAIL if policy == "fail_if_up" else Status.WARN
            r = PortResult(switch_id=self.plan.id, switch_name=self.plan.name,
                           interface=iface, diagram_label=label,
                           target="(nothing on the drawing)", media="",
                           observed=self._observed(iface), listed=False)
            seen = self._describe_far_end(iface)
            r.findings.append(Finding(
                status, "unlisted",
                "%s has link, but nothing should be plugged into it%s." % (label, seen),
                "Check whether a cable meant for another port was plugged into %s by "
                "mistake." % label))
            results.append(r)
        return results

    # ---- hints that need every port's result --------------------------

    def _add_cross_port_hints(self, results):
        stray = [r for r in results
                 if any(f.check in ("unlisted", "unused") and f.status != Status.PASS
                        for f in r.findings)]
        if not stray:
            return
        names = ", ".join(r.diagram_label for r in stray)
        for r in results:
            if r in stray:
                continue
            nolink = [f for f in r.findings if f.check == "link" and f.status == Status.FAIL
                      and "no link" in f.message]
            for f in nolink:
                f.action = (f.action or "") + (
                    " Also: %s %s link but should be empty; the cable for %s may be "
                    "in the wrong port." % (names, "has" if len(stray) == 1 else "have",
                                            r.diagram_label))

    # ---- helpers -------------------------------------------------------

    def _observed(self, iface):
        row = self.snap.status.get(iface) or {}
        return {
            "status": row.get("status"),
            "speed_mbps": row.get("speed_mbps"),
            "duplex": row.get("duplex"),
            "type": row.get("type"),
            "description": row.get("description"),
            "neighbors": [{k: n.get(k) for k in ("device_id", "remote_port", "protocol")}
                          for n in self.snap.neighbors(iface)],
            "macs": [e["mac"] for e in self.snap.macs.get(iface, [])],
            "ips": self.snap.ips(iface),
        }

    def _describe_far_end(self, iface):
        n = self.snap.neighbors(iface)
        if n:
            return " (it sees %s)" % n[0]["device_id"]
        macs = self.snap.macs.get(iface, [])
        if macs:
            return " (device MAC %s)" % macs[0]["mac"]
        return ""


def _speed_text(mbps):
    if not mbps:
        return "unknown speed"
    if mbps >= 1000 and mbps % 1000 == 0:
        return "%d Gb/s" % (mbps // 1000)
    return "%d Mb/s" % mbps


def _label(iface):
    return drawing_label(iface) if iface else iface


def _alnum(text):
    return "".join(c for c in (text or "").lower() if c.isalnum())
