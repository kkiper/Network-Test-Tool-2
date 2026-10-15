"""Rename the switches in a site's configs (e.g. "Switch 1" -> "Rack A core").

The name is only what the tool shows: the test choices, the results table,
messages and saved reports. It doesn't change anything on the switches
themselves (their hostnames stay as they are).

Run it from the command line with the site folder and one ID=NAME per switch::

    python -m nettest.rename configs/standard-build switch1="Rack A core" switch2="Rack B"

With just the site folder, it lists the switches and their current names.

The new name is written to ``switch.name`` in that switch's config. Anywhere
else in the site's configs the old name appears in a name, note or device
field (e.g. "Switch 1 + Switch 2 connected") is updated to match.
"""

import json
import os
import re
import sys

from .config import ConfigError, _read_json, load_test

NAME_MAX = 40
# Only these fields hold display text. Ids, interfaces and jack labels are
# never touched.
TEXT_KEYS = ("name", "note", "device")

USAGE = """Usage: python -m nettest.rename SITE_FOLDER [ID=NAME ...]

  python -m nettest.rename configs/standard-build
      list the switches and their current names
  python -m nettest.rename configs/standard-build switch1="Rack A core" switch2="Rack B"
      rename them (put names with spaces in quotes)"""


def switch_names(site_dir):
    """``[(id, name, filename)]`` for each switch config in ``site_dir``."""
    out = []
    for fn in sorted(os.listdir(site_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            data = _read_json(os.path.join(site_dir, fn))
        except ConfigError:
            continue
        sw = data.get("switch") if isinstance(data, dict) else None
        if isinstance(sw, dict) and "ports" in data and "id" in sw:
            out.append((sw["id"], sw.get("name", sw["id"]), fn))
    return out


def check_names(current, new_names):
    """Tidy and check the new names; returns ``{id: name}`` or raises ValueError.

    ``current`` is :func:`switch_names` output, ``new_names`` is ``{id: name}``.
    """
    known = {sid: name for sid, name, _ in current}
    final = dict(known)
    for sid, name in new_names.items():
        if sid not in known:
            raise ValueError("There's no switch called %r in this site. Switches: %s"
                             % (sid, ", ".join(known)))
        name = " ".join(str(name).split())   # no newlines or doubled spaces
        if not name:
            raise ValueError("The name for %s can't be blank." % known[sid])
        if len(name) > NAME_MAX:
            raise ValueError("Keep the name for %s to %d characters or fewer."
                             % (known[sid], NAME_MAX))
        final[sid] = name
    seen = {}
    for sid, name in final.items():
        if name.lower() in seen:
            raise ValueError("Two switches can't both be called %r." % name)
        seen[name.lower()] = sid
    return {sid: final[sid] for sid in new_names}


def _replace_text(node, pattern, mapping):
    """Swap old names for new ones in the display-text fields of a JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in TEXT_KEYS and isinstance(v, str):
                node[k] = pattern.sub(lambda m: mapping[m.group(0)], v)
            else:
                _replace_text(v, pattern, mapping)
    elif isinstance(node, list):
        for v in node:
            _replace_text(v, pattern, mapping)


def rename_switches(site_dir, new_names):
    """Rename switches in every config in ``site_dir``.

    ``new_names`` is ``{switch id: new name}``. Returns the paths of the files
    that changed. Nothing is written if a name is bad or a config would no
    longer load.
    """
    current = switch_names(site_dir)
    new_names = check_names(current, new_names)
    old = {sid: name for sid, name, _ in current}
    mapping = {old[sid]: new for sid, new in new_names.items() if old[sid] != new}
    if not mapping:
        return []
    # One pass with every old name at once, so swapping two names works, and
    # whole words only, so "Switch 1" doesn't match inside "Switch 10".
    pattern = re.compile(r"(?<!\w)(%s)(?!\w)" % "|".join(
        re.escape(n) for n in sorted(mapping, key=len, reverse=True)))

    updates = {}
    for fn in sorted(os.listdir(site_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(site_dir, fn)
        try:
            data = _read_json(path)
        except ConfigError:
            continue
        before = json.dumps(data, sort_keys=True)
        sw = data.get("switch") if isinstance(data, dict) else None
        _replace_text(data, pattern, mapping)
        if isinstance(sw, dict) and sw.get("id") in new_names:
            sw["name"] = new_names[sw["id"]]
        if json.dumps(data, sort_keys=True) != before:
            updates[path] = data

    _write_checked(site_dir, updates)
    return sorted(updates)


def _write_checked(site_dir, updates):
    """Write the updated configs, putting them all back if any won't load."""
    originals = {}
    for path, data in updates.items():
        with open(path, encoding="utf-8") as f:
            originals[path] = f.read()
    try:
        for path, data in updates.items():
            _write_json(path, data)
        for fn in os.listdir(site_dir):
            path = os.path.join(site_dir, fn)
            if fn.endswith(".json") and (path in updates or _is_test(path)):
                load_test(path)
    except Exception:
        for path, text in originals.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        raise


def _is_test(path):
    try:
        data = _read_json(path)
    except ConfigError:
        return False
    return isinstance(data, dict) and (data.get("test") == "combined"
                                       or ("switch" in data and "ports" in data))


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    site, pairs = argv[0], argv[1:]
    if not os.path.isdir(site):
        print("Not a folder: %s" % site, file=sys.stderr)
        return 2
    if not pairs:
        for sid, name, fn in switch_names(site):
            print("%-10s %s  (%s)" % (sid, name, fn))
        return 0
    new_names = {}
    for p in pairs:
        if "=" not in p:
            print("Write each rename as ID=NAME, e.g. switch1=\"Rack A core\"", file=sys.stderr)
            return 2
        sid, name = p.split("=", 1)
        new_names[sid.strip()] = name
    try:
        changed = rename_switches(site, new_names)
    except (ValueError, ConfigError) as e:
        print("Nothing was renamed: %s" % e, file=sys.stderr)
        return 2
    for sid, name, fn in switch_names(site):
        print("%-10s %s  (%s)" % (sid, name, fn))
    print("Updated %d file(s)." % len(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
