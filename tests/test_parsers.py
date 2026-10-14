import os

from nettest import parsers
from nettest.collectors import split_session_log
from nettest.interfaces import drawing_label, normalize_interface, normalize_mac


def test_normalize_interface():
    assert normalize_interface("Gi1/3") == "GigabitEthernet1/3"
    assert normalize_interface("Gig 1/3") == "GigabitEthernet1/3"
    assert normalize_interface("GigabitEthernet1/3") == "GigabitEthernet1/3"
    assert normalize_interface("Te1/1") == "TenGigabitEthernet1/1"
    assert normalize_interface("Ten 1/1") == "TenGigabitEthernet1/1"
    assert normalize_interface("eth0") == "Ethernet0"
    assert normalize_interface("not a port") == "not a port"


def test_drawing_label_and_mac():
    assert drawing_label("GigabitEthernet1/10") == "GI 1/10"
    assert drawing_label("TenGigabitEthernet1/2") == "TE 1/2"
    assert normalize_mac("3cec.ef10.2031") == "3c:ec:ef:10:20:31"
    assert normalize_mac("3C-EC-EF-10-20-31") == "3c:ec:ef:10:20:31"


STATUS = """
Port         Name               Status       Vlan       Duplex  Speed Type
Gi1/3        Server P1          connected    10         a-full a-1000 10/100/1000BaseTX
Gi1/4        A very long descr  notconnect   10           auto   auto 10/100/1000BaseTX
Gi1/8        UPS                connected    10         a-half  a-100 10/100/1000BaseTX
Gi1/9                           err-disabled 10           auto   auto 10/100/1000BaseTX
Gi1/10                          disabled     1            auto   auto 10/100/1000BaseTX
Te1/1                           connected    10           full    10G SFP-10GBase-SR
Te1/2                           notconnect   10           full    10G Not Present
"""


def test_parse_interfaces_status():
    st = parsers.parse_interfaces_status(STATUS)
    assert set(st) == {"GigabitEthernet1/%d" % i for i in (3, 4, 8, 9, 10)} | {
        "TenGigabitEthernet1/1", "TenGigabitEthernet1/2"}
    g3 = st["GigabitEthernet1/3"]
    assert (g3["status"], g3["speed_mbps"], g3["duplex"], g3["description"]) == \
        ("connected", 1000, "full", "Server P1")
    assert st["GigabitEthernet1/4"]["description"] == "A very long descr"
    assert st["GigabitEthernet1/8"]["duplex"] == "half"
    assert st["GigabitEthernet1/8"]["speed_mbps"] == 100
    assert st["GigabitEthernet1/9"]["status"] == "err-disabled"
    assert st["GigabitEthernet1/10"]["status"] == "disabled"
    assert st["GigabitEthernet1/4"]["speed_mbps"] is None
    assert st["TenGigabitEthernet1/1"]["speed_mbps"] == 10000
    assert st["TenGigabitEthernet1/2"]["type"] == "Not Present"


def test_parse_cdp_and_lldp():
    cdp = parsers.parse_cdp_neighbors_detail("""
-------------------------
Device ID: SW2.example.local
Entry address(es):
  IP address: 192.168.1.2
Platform: cisco ESS-3300-CON,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/4,  Port ID (outgoing port): GigabitEthernet1/5
Holdtime : 152 sec
""")
    n = cdp["GigabitEthernet1/4"][0]
    assert n["device_id"] == "SW2.example.local"
    assert n["remote_port"] == "GigabitEthernet1/5"
    assert n["ip"] == "192.168.1.2"
    assert n["platform"] == "cisco ESS-3300-CON"

    lldp = parsers.parse_lldp_neighbors_detail("""
------------------------------------------------
Local Intf: Gi1/9
Chassis id: 0011.2233.4455
Port id: Gi0/1
Port Description: CH1
System Name: radio1

Management Addresses:
    IP: 10.0.0.9
""")
    n = lldp["GigabitEthernet1/9"][0]
    assert n["device_id"] == "radio1"
    assert n["remote_port"] == "GigabitEthernet0/1"
    assert n["ip"] == "10.0.0.9"
    assert n["protocol"] == "lldp"


def test_parse_mac_table_skips_cpu():
    macs = parsers.parse_mac_address_table("""
Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
 All    0100.0ccc.cccc    STATIC      CPU
  10    3cec.ef10.2031    DYNAMIC     Gi1/3
  10    3cec.ef10.2099    DYNAMIC     Gi1/3
Total Mac Addresses for this criterion: 3
""")
    assert list(macs) == ["GigabitEthernet1/3"]
    assert [e["mac"] for e in macs["GigabitEthernet1/3"]] == [
        "3c:ec:ef:10:20:31", "3c:ec:ef:10:20:99"]


def test_parse_transceiver_detail_flags_and_na():
    text = open(os.path.join(os.path.dirname(__file__), "samples", "switch1_good.log")).read()
    dom = parsers.parse_transceiver_detail(text)
    te1 = dom["TenGigabitEthernet1/1"]
    assert te1["tx_dbm"] == {"value": -2.4, "flag": None, "high_alarm": 1.7,
                             "high_warn": -1.3, "low_warn": -7.3, "low_alarm": -11.3}
    assert te1["rx_dbm"]["value"] == -3.1
    assert te1["temperature_c"]["value"] == 33.4
    te2 = dom["TenGigabitEthernet1/2"]
    assert te2["rx_dbm"]["value"] == -40.0
    assert te2["rx_dbm"]["flag"] == "--"
    assert te2["rx_dbm"]["low_alarm"] == -13.9

    na = parsers.parse_transceiver_detail("""
           Optical              High Alarm  High Warn  Low Warn   Low Alarm
           Receive Power        Threshold   Threshold  Threshold  Threshold
Port       (dBm)                (dBm)       (dBm)      (dBm)      (dBm)
---------  -----------------    ----------  ---------  ---------  ---------
Te1/1      N/A                  N/A         N/A        N/A        N/A
""")
    assert na["TenGigabitEthernet1/1"]["rx_dbm"]["value"] is None


def test_split_session_log_handles_abbreviations():
    hostname, outputs = split_session_log(
        "SW2#term len 0\nSW2#sh int status\nPort  Name\nSW2#sh cdp nei det\n"
        "Total cdp entries displayed : 0\nSW2#show clock\n12:00\nSW2#")
    assert hostname == "SW2"
    assert outputs["show interfaces status"] == "Port  Name"
    assert outputs["show cdp neighbors detail"] == "Total cdp entries displayed : 0"
    assert "show clock" not in outputs


def test_hostname():
    assert parsers.parse_hostname("hostname SW1\n") == "SW1"
    assert parsers.parse_hostname("SW1#") == "SW1"
