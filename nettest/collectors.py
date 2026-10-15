"""Get show-command output from a live switch (SSH) or from saved text.

A collector returns a :class:`Snapshot`: the raw text of each command plus
the parsed data the checker uses. The checker never knows whether the text
came from SSH or a file, which is what makes offline mode and the tests work.
"""

import os
import re
from dataclasses import dataclass, field

from . import parsers

# Command -> (snapshot attribute, parser). Order is the order they run in.
COMMANDS = {
    "show interfaces status": ("status", parsers.parse_interfaces_status),
    "show cdp neighbors detail": ("cdp", parsers.parse_cdp_neighbors_detail),
    "show lldp neighbors detail": ("lldp", parsers.parse_lldp_neighbors_detail),
    "show mac address-table": ("macs", parsers.parse_mac_address_table),
    "show interfaces transceiver detail": ("transceivers", parsers.parse_transceiver_detail),
    # Only used to show each device's IP address; no check depends on them.
    "show ip arp": ("arp", parsers.parse_ip_arp),
    "show device-tracking database": ("device_tracking", parsers.parse_device_tracking),
}
OPTIONAL_COMMANDS = {"show ip arp", "show device-tracking database"}
HOSTNAME_COMMAND = "show running-config | include ^hostname"

# Output that means "the command ran but the feature is off / not supported".
_UNAVAILABLE = re.compile(r"^% (?:CDP|LLDP) is not enabled|^% Invalid input|"
                          r"^% Incomplete command|^% Ambiguous command", re.M)


class CollectionError(Exception):
    """Could not reach or log in to the switch."""


@dataclass
class Snapshot:
    """Everything read from one switch."""

    source: str                      # "ssh 10.0.0.1" or "file ..."
    hostname: str = None
    raw: dict = field(default_factory=dict)        # command -> text (None if missing)
    unavailable: dict = field(default_factory=dict)  # command -> reason
    status: dict = field(default_factory=dict)
    cdp: dict = field(default_factory=dict)
    lldp: dict = field(default_factory=dict)
    macs: dict = field(default_factory=dict)
    transceivers: dict = field(default_factory=dict)
    arp: dict = field(default_factory=dict)              # mac -> [ip]
    device_tracking: dict = field(default_factory=dict)  # interface -> [ip]

    def has(self, attr):
        """True if the command behind ``attr`` produced usable output."""
        for cmd, (a, _) in COMMANDS.items():
            if a == attr:
                return self.raw.get(cmd) is not None and cmd not in self.unavailable
        return False

    def neighbors(self, interface):
        return self.cdp.get(interface, []) + self.lldp.get(interface, [])

    def ips(self, interface):
        """IP addresses of whatever is plugged into ``interface``, best guess.

        From device tracking, then the ARP table for the port's MACs, then
        any management address a CDP/LLDP neighbor advertises.
        """
        found = []

        def add(ip):
            if ip and ip not in found:
                found.append(ip)
        for ip in self.device_tracking.get(interface, []):
            add(ip)
        for entry in self.macs.get(interface, []):
            for ip in self.arp.get(entry["mac"], []):
                add(ip)
        for n in self.neighbors(interface):
            add(n.get("ip"))
        return found


def build_snapshot(source, outputs, hostname=None):
    """Parse a ``{command: text}`` dict into a :class:`Snapshot`."""
    snap = Snapshot(source=source, hostname=hostname)
    for cmd, (attr, parse) in COMMANDS.items():
        text = outputs.get(cmd)
        snap.raw[cmd] = text
        if text is None:
            snap.unavailable[cmd] = "not collected"
            continue
        m = _UNAVAILABLE.search(text)
        if m:
            snap.unavailable[cmd] = m.group(0).lstrip("% ").strip()
            continue
        setattr(snap, attr, parse(text))
    if snap.hostname is None:
        snap.hostname = parsers.parse_hostname(outputs.get(HOSTNAME_COMMAND, ""))
    return snap


# --------------------------------------------------------------------------
# Live switch over SSH
# --------------------------------------------------------------------------

class SshCollector:
    """Read a switch over SSH using netmiko.

    ``device_type`` defaults to ``cisco_xe`` (the ESS 3300 runs IOS-XE).
    ``secret`` is the enable password, only needed if the login lands in user
    EXEC mode (``>`` prompt).
    """

    def __init__(self, host, username, password, secret=None, port=22,
                 device_type="cisco_xe", timeout=20):
        self.host = host
        self.username = username
        self.password = password
        self.secret = secret
        self.port = port
        self.device_type = device_type
        self.timeout = timeout

    def describe(self):
        return "ssh %s" % self.host

    def collect(self, on_progress=None):
        try:
            from netmiko import ConnectHandler
            from netmiko.exceptions import (NetmikoAuthenticationException,
                                            NetmikoTimeoutException)
        except ImportError:
            raise CollectionError("The netmiko package is not installed, so the tool "
                                  "can't talk to switches. Run: pip install netmiko")

        say = on_progress or (lambda msg: None)
        say("Connecting to %s..." % self.host)
        try:
            conn = ConnectHandler(device_type=self.device_type, host=self.host,
                                  username=self.username, password=self.password,
                                  secret=self.secret or "", port=self.port,
                                  conn_timeout=self.timeout, auth_timeout=self.timeout)
        except NetmikoAuthenticationException:
            raise CollectionError("The switch at %s rejected the username or password."
                                  % self.host)
        except NetmikoTimeoutException:
            raise CollectionError("Could not reach %s. Check the laptop's network cable, "
                                  "its IP address, and that the switch IP is right."
                                  % self.host)
        except Exception as e:  # paramiko/socket errors come in many shapes
            raise CollectionError("Could not connect to %s: %s" % (self.host, e))

        try:
            if self.secret and not conn.check_enable_mode():
                conn.enable()
            hostname = conn.find_prompt().rstrip("#>").strip() or None
            outputs = {}
            for cmd in list(COMMANDS) + [HOSTNAME_COMMAND]:
                say("Running '%s' on %s" % (cmd, self.host))
                outputs[cmd] = conn.send_command(cmd, read_timeout=60)
        finally:
            conn.disconnect()
        return build_snapshot(self.describe(), outputs, hostname=hostname)


# --------------------------------------------------------------------------
# Saved output (offline mode)
# --------------------------------------------------------------------------

_PROMPT_LINE = re.compile(r"^([A-Za-z0-9._-]+)(?:\([^)]*\))?[#>]\s*(.*?)\s*$")


def _matches_command(typed, full):
    """True if ``typed`` is an IOS abbreviation of ``full`` (``sh int status``)."""
    typed_words = typed.lower().split()
    full_words = full.lower().split()
    if len(typed_words) != len(full_words):
        return False
    return all(f.startswith(t) for t, f in zip(typed_words, full_words))


def _command_for(typed):
    for cmd in list(COMMANDS) + [HOSTNAME_COMMAND]:
        if _matches_command(typed, cmd):
            return cmd
    return None


def split_session_log(text):
    """Split a saved terminal session (PuTTY log, copy-paste) into commands.

    Looks for prompt lines like ``Switch1#show interfaces status`` and returns
    ``(hostname, {command: output})``. Abbreviated commands are recognised.
    """
    outputs, hostname = {}, None
    current, buf = None, []
    for line in text.splitlines():
        m = _PROMPT_LINE.match(line)
        if m and (not m.group(2) or _command_for(m.group(2)) or m.group(2).split()[0].lower()
                  in ("sh", "show", "term", "terminal", "en", "enable", "exit")):
            if current:
                outputs[current] = "\n".join(buf)
            hostname = hostname or m.group(1)
            current = _command_for(m.group(2)) if m.group(2) else None
            buf = []
            continue
        if current:
            buf.append(line)
    if current:
        outputs[current] = "\n".join(buf)
    return hostname, outputs


def _file_name_for(cmd):
    return re.sub(r"[^a-z0-9]+", "_", cmd.lower()).strip("_") + ".txt"


class OfflineCollector:
    """Read saved show-command output instead of a live switch.

    ``path`` is either:

    * a session log file containing prompts and commands (``Switch1#show
      interfaces status`` followed by its output, and so on), or
    * a folder with one file per command, named after the command:
      ``show_interfaces_status.txt``, ``show_cdp_neighbors_detail.txt``,
      ``show_lldp_neighbors_detail.txt``, ``show_mac_address_table.txt``,
      ``show_interfaces_transceiver_detail.txt`` and optionally
      ``hostname.txt``.
    """

    def __init__(self, path):
        self.path = path

    def describe(self):
        return "file %s" % self.path

    def collect(self, on_progress=None):
        say = on_progress or (lambda msg: None)
        say("Reading saved output from %s" % self.path)
        if os.path.isdir(self.path):
            outputs, hostname = {}, None
            for cmd in COMMANDS:
                fn = os.path.join(self.path, _file_name_for(cmd))
                if os.path.exists(fn):
                    with open(fn, encoding="utf-8", errors="replace") as f:
                        outputs[cmd] = f.read()
            hn = os.path.join(self.path, "hostname.txt")
            if os.path.exists(hn):
                with open(hn, encoding="utf-8") as f:
                    text = f.read()
                hostname = parsers.parse_hostname(text) or text.strip() or None
            if not outputs:
                raise CollectionError("No saved command output found in %s" % self.path)
        elif os.path.isfile(self.path):
            with open(self.path, encoding="utf-8", errors="replace") as f:
                hostname, outputs = split_session_log(f.read())
            if not outputs:
                raise CollectionError("%s doesn't contain any of the show commands this "
                                      "tool needs" % self.path)
        else:
            raise CollectionError("Saved output not found: %s" % self.path)
        return build_snapshot(self.describe(), outputs, hostname=hostname)
