"""Parsers for Cisco IOS / IOS-XE show-command output.

Each parser takes the raw text of one command and returns plain Python data
keyed by full interface name. They are deliberately forgiving: unknown lines
are skipped, so a banner, a prompt or a trailing ``Total ...`` line in pasted
output does not break anything.

Written against the Cisco ESS 3300 (IOS-XE 17.x), but the formats are shared
across Catalyst IOS/IOS-XE switches.
"""

import re

from .interfaces import normalize_interface, normalize_mac

_IFACE_RE = re.compile(r"^(?:Gi|Te|Fa|Tw|Twe|Et|Po|Ap)[A-Za-z]*\s?\d+(?:/\d+)+", re.I)


# --------------------------------------------------------------------------
# show interfaces status
# --------------------------------------------------------------------------

def parse_speed(text):
    """``a-1000`` -> 1000, ``10G`` -> 10000, ``auto`` -> None."""
    if not text:
        return None
    t = text.lower().strip()
    if t.startswith("a-"):
        t = t[2:]
    m = re.match(r"^(\d+(?:\.\d+)?)([gm]?)$", t)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2) == "g":
        value *= 1000
    return int(value)


def parse_duplex(text):
    """``a-full`` -> ``full``, ``half`` -> ``half``, ``auto`` -> None."""
    if not text:
        return None
    t = text.lower().strip()
    if t.startswith("a-"):
        t = t[2:]
    return t if t in ("full", "half") else None


def parse_interfaces_status(text):
    """Parse ``show interfaces status``.

    Returns ``{interface: {"description", "status", "vlan", "duplex",
    "speed_mbps", "duplex_raw", "speed_raw", "type"}}``. The Name (description)
    column can contain spaces and the Type column can too (``Not Present``), so
    rows are split around the Status word rather than on whitespace.
    """
    result = {}
    seen_header = False
    for line in (text or "").splitlines():
        if re.match(r"^\s*Port\s+Name\s+Status\s+Vlan\s+Duplex\s+Speed\s+Type", line):
            seen_header = True
            continue
        if not seen_header or not _IFACE_RE.match(line.strip()):
            continue
        m = _STATUS_ROW.match(line.strip())
        if not m:
            continue
        port, name, status, vlan, duplex, speed, type_ = m.groups()
        result[normalize_interface(port)] = {
            "description": (name or "").strip(),
            "status": status,
            "vlan": vlan,
            "duplex_raw": duplex,
            "speed_raw": speed,
            "duplex": parse_duplex(duplex),
            "speed_mbps": parse_speed(speed),
            "type": (type_ or "").strip(),
        }
    return result


_STATUS_WORDS = ("connected", "notconnect", "disabled", "err-disabled", "inactive",
                 "monitoring", "suspended", "sfpAbsent", "xcvrAbsen", "noOperMem",
                 "notconnec", "faulty", "down", "up")

_STATUS_ROW = re.compile(
    r"^(\S+)\s+(?:(.*?)\s+)?(%s)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$"
    % "|".join(re.escape(w) for w in _STATUS_WORDS))


# --------------------------------------------------------------------------
# show cdp neighbors detail
# --------------------------------------------------------------------------

def parse_cdp_neighbors_detail(text):
    """Parse ``show cdp neighbors detail``.

    Returns ``{local_interface: [{"device_id", "remote_port", "platform",
    "ip", "protocol": "cdp"}]}``.
    """
    result = {}
    for block in re.split(r"^-{5,}\s*$", text or "", flags=re.M):
        dev = re.search(r"^Device ID:\s*(.+?)\s*$", block, re.M)
        intf = re.search(r"^Interface:\s*([^,]+),\s*Port ID \(outgoing port\):\s*(.+?)\s*$",
                         block, re.M)
        if not dev or not intf:
            continue
        platform = re.search(r"^Platform:\s*([^,]+)", block, re.M)
        ip = re.search(r"IP(?:v4)? [Aa]ddress:\s*(\S+)", block)
        local = normalize_interface(intf.group(1).strip())
        result.setdefault(local, []).append({
            "device_id": dev.group(1),
            "remote_port": normalize_interface(intf.group(2)),
            "platform": platform.group(1).strip() if platform else None,
            "ip": ip.group(1) if ip else None,
            "protocol": "cdp",
        })
    return result


# --------------------------------------------------------------------------
# show lldp neighbors detail
# --------------------------------------------------------------------------

def parse_lldp_neighbors_detail(text):
    """Parse ``show lldp neighbors detail``.

    Returns the same shape as :func:`parse_cdp_neighbors_detail`, with
    ``protocol: "lldp"`` and the chassis id / port description added.
    """
    result = {}
    for block in re.split(r"^-{5,}\s*$", text or "", flags=re.M):
        local = re.search(r"^Local Intf:\s*(\S+)", block, re.M)
        if not local:
            continue

        def field(label):
            m = re.search(r"^%s:\s*(.*?)\s*$" % re.escape(label), block, re.M)
            return m.group(1) if m and m.group(1) else None

        ip = re.search(r"^\s*IP:\s*(\S+)", block, re.M)
        port_id = field("Port id")
        result.setdefault(normalize_interface(local.group(1)), []).append({
            "device_id": field("System Name") or field("Chassis id"),
            "remote_port": normalize_interface(port_id) if port_id else None,
            "port_description": field("Port Description"),
            "chassis_id": field("Chassis id"),
            "platform": None,
            "ip": ip.group(1) if ip else None,
            "protocol": "lldp",
        })
    return result


# --------------------------------------------------------------------------
# show mac address-table
# --------------------------------------------------------------------------

_MAC_ROW = re.compile(
    r"^\s*(\d+|All)\s+([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+(\S+)\s+(\S+)\s*$")


def parse_mac_address_table(text):
    """Parse ``show mac address-table``.

    Returns ``{interface: [{"mac", "vlan", "type"}]}``. Entries on ``CPU`` and
    other non-port destinations are dropped.
    """
    result = {}
    for line in (text or "").splitlines():
        m = _MAC_ROW.match(line)
        if not m:
            continue
        vlan, mac, type_, port = m.groups()
        if not _IFACE_RE.match(port):
            continue
        result.setdefault(normalize_interface(port), []).append({
            "mac": normalize_mac(mac), "vlan": vlan, "type": type_.lower(),
        })
    return result


# --------------------------------------------------------------------------
# show interfaces transceiver detail
# --------------------------------------------------------------------------

_SECTIONS = (
    ("Transmit Power", "tx_dbm"),
    ("Receive Power", "rx_dbm"),
    ("Temperature", "temperature_c"),
    ("Voltage", "voltage_v"),
    ("Current", "current_ma"),
)

_FLAG_RE = r"(?:\+\+|--|\+|-)"


def _num(token):
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def parse_transceiver_detail(text):
    """Parse ``show interfaces transceiver detail`` (SFP DOM readings).

    Returns ``{interface: {"tx_dbm": {...}, "rx_dbm": {...}, ...}}`` where each
    reading is ``{"value", "flag", "high_alarm", "high_warn", "low_warn",
    "low_alarm"}``. ``value`` is ``None`` when the switch prints ``N/A``.
    ``flag`` is the switch's own ``++ + - --`` marker, or ``None``.
    """
    result = {}
    section = None
    for line in (text or "").splitlines():
        if "Threshold" in line:
            for title, key in _SECTIONS:
                if title in line:
                    section = key
                    break
        if section is None:
            continue
        tokens = line.split()
        if not tokens or not _IFACE_RE.match(tokens[0]):
            continue
        port = normalize_interface(tokens[0])
        rest = tokens[1:]
        if not rest:
            continue
        value = _num(rest[0])
        rest = rest[1:]
        flag = None
        if rest and re.fullmatch(_FLAG_RE, rest[0]) and len(rest) >= 5:
            flag, rest = rest[0], rest[1:]
        thresholds = [_num(t) for t in rest[:4]] + [None] * (4 - len(rest[:4]))
        result.setdefault(port, {})[section] = {
            "value": value,
            "flag": flag,
            "high_alarm": thresholds[0],
            "high_warn": thresholds[1],
            "low_warn": thresholds[2],
            "low_alarm": thresholds[3],
        }
    return result


# --------------------------------------------------------------------------
# hostname
# --------------------------------------------------------------------------

def parse_hostname(text):
    """Find the hostname in ``show running-config | include ^hostname`` or a prompt."""
    m = re.search(r"^hostname\s+(\S+)", text or "", re.M)
    if m:
        return m.group(1)
    m = re.search(r"^([A-Za-z0-9._-]+)[#>]", text or "", re.M)
    return m.group(1) if m else None
