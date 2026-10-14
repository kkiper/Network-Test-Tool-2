# Network Test Tool

A tool for field techs to confirm that Cisco ESS 3300 switches are cabled the
way the wiring diagrams say. It will log in to each switch over SSH, read link
status, speed, CDP/LLDP neighbors, MAC addresses and SFP light levels, and
compare them to a JSON config. Each port gets a pass, warn or fail result.

Testing happens in three modes: Switch 1 alone, Switch 2 alone, then both
switches connected together.

## Configs

```
configs/standard-build/
  switch1.json    Switch 1 wiring
  switch2.json    Switch 2 wiring
  combined.json   both switches connected, plus the link between them
schema/
  switch-config.schema.json
  combined-config.schema.json
```

Copy `configs/standard-build/` to a new folder if a site is wired differently.

### Switch config (`switch1.json`, `switch2.json`)

| Field | Meaning |
|---|---|
| `schema_version` | Always `1` for now. |
| `switch` | `id` (used by `combined.json`), display `name`, `model`. |
| `defaults.copper.expect` | What every copper port should show unless the port overrides it (link up, 1000 Mb/s, full duplex). |
| `unlisted_ports` | What to do with ports not in the list: `ignore`, `warn_if_up` or `fail_if_up`. |
| `ports[]` | One entry per cabled port. |

Each port:

| Field | Meaning |
|---|---|
| `interface` | Full IOS name, e.g. `GigabitEthernet1/3`, `TenGigabitEthernet1/1`. |
| `diagram_label` | The label exactly as drawn (`GI 1/3`, `GE 1/9`, `TE1/1`) so techs can match it to the drawing. |
| `media` | `copper` or `fiber`. |
| `connects_to` | `device`, `port` (or `null` if the drawing shows none), and `kind`: `device`, `bulkhead` (passive patch point) or `switch`. |
| `optic` | Fiber only: SFP part number, type, wavelength, multimode/singlemode. |
| `expect` | Overrides for this port: `link` (`up`, `optional`, `down`), `speed_mbps`, `duplex`, `cdp_neighbor`, `mac_addresses`, `light_levels_dbm`. |
| `note` | Plain-English note shown to the tech. |

Bulkhead ports use `link: optional` because a passive bulkhead only shows link
when something is plugged into its far side.

Fiber light levels: the checker uses the thresholds the SFP reports about
itself (`use_module_thresholds: true`) and falls back to the `tx`/`rx` ranges
in dBm, which come from the optic's datasheet (FS.com SFP-10GSR-85I: transmit
-6.0 to -1.0 dBm, receive -11.1 to +0.5 dBm).

### Combined config (`combined.json`)

Lists the two switch configs to run together and the `interconnects` between
them. Each interconnect names both ends (`a`, `b`: switch id and interface),
the `media`, any bulkheads along the `path`, and what to `expect` (link up and
each switch seeing the other as a CDP neighbor). In the combined test the
interconnect's `expect` replaces each end port's standalone `expect`, so a
bulkhead port that was optional on its own must have link when the switches
are joined. An entry with `"placeholder": true` is not filled in yet and is
reported as "not configured" rather than failed.
