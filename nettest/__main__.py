"""Command-line front end, and the launcher for the window.

Run with no arguments to open the window. Command-line examples::

    # Live, one switch (prompts for the password)
    python -m nettest configs/standard-build/switch1.json --host 192.168.1.1 --user admin

    # Live, both switches connected
    python -m nettest configs/standard-build/combined.json \\
        --host switch1=192.168.1.1 --host switch2=192.168.1.2 --user admin

    # Both switches connected, plus the copper and fiber tests through the
    # external test switch (they turn test-switch ports off and on)
    python -m nettest configs/standard-build/combined.json --copper --fiber \\
        --host switch1=192.168.0.20 --host switch2=192.168.0.10 \\
        --host test_switch=192.168.0.60 --user admin

    # Offline, from saved output (a session log file or a folder of files)
    python -m nettest configs/standard-build/switch1.json --offline switch1-log.txt
    python -m nettest configs/standard-build/combined.json \\
        --offline switch1=sw1.txt --offline switch2=sw2.txt

Exit code: 0 all passed, 1 warnings, 2 failures, 3 couldn't run.
"""

import argparse
import getpass
import json
import sys

from . import ConfigError, OfflineCollector, SshCollector, Status, load_test, run_test


def _assign(values, plan, what):
    """Turn ``["1.2.3.4"]`` or ``["switch1=1.2.3.4", ...]`` into ``{id: value}``."""
    ids = [s.id for s in plan.switches] + ([plan.test_switch.id] if plan.test_switch else [])
    out = {}
    for v in values or []:
        if "=" in v and v.split("=", 1)[0] in ids:
            sid, val = v.split("=", 1)
            out[sid] = val
        elif len(ids) == 1:
            out[ids[0]] = v
        else:
            raise SystemExit("This test has more than one switch (%s). Give each %s as "
                             "ID=VALUE." % (", ".join(ids), what))
    return out


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:  # double-clicked or run with no arguments: open the window
        from .gui import main as gui_main
        gui_main()
        return 0
    ap = argparse.ArgumentParser(prog="nettest", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="switch1.json, switch2.json or combined.json")
    ap.add_argument("--host", action="append", help="switch IP (ID=IP in combined mode)")
    ap.add_argument("--offline", action="append",
                    help="saved output file or folder (ID=PATH in combined mode)")
    ap.add_argument("--user", help="SSH username")
    ap.add_argument("--password", help="SSH password (prompted if omitted)")
    ap.add_argument("--enable", help="enable password, if needed")
    ap.add_argument("--copper", action="store_true",
                    help="combined test: also run the copper test through the test switch")
    ap.add_argument("--fiber", action="store_true",
                    help="combined test: also run the fiber test through the test switch")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--save", metavar="FOLDER", help="save .txt and .json reports here")
    ap.add_argument("--site", help="site name used in saved report file names")
    args = ap.parse_args(argv)

    try:
        plan = load_test(args.config)
    except ConfigError as e:
        print("Config problem: %s" % e, file=sys.stderr)
        return 3

    extras = tuple(e for e in ("copper", "fiber") if getattr(args, e))
    missing = [e for e in extras if e not in plan.extras]
    if missing:
        print("Config problem: %s has no %s test set up." % (args.config, missing[0]),
              file=sys.stderr)
        return 3
    collectors = {}
    for sid, path in _assign(args.offline, plan, "saved-output path").items():
        collectors[sid] = OfflineCollector(path)
    hosts = _assign(args.host, plan, "host")
    if hosts:
        user = args.user or input("Username: ")
        password = args.password or getpass.getpass("Password: ")
        for sid, host in hosts.items():
            collectors[sid] = SshCollector(host, user, password, secret=args.enable)
    if not collectors:
        ap.error("give --host for a live switch or --offline for saved output")

    progress = None if args.json else (lambda m: print("..", m, file=sys.stderr))
    report = run_test(plan, collectors, on_progress=progress, extras=extras)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())
    if args.save:
        for p in report.save(args.save, site=args.site):
            print("Saved %s" % p, file=sys.stderr)

    if any(s.error for s in report.switches):
        return 3
    return {Status.PASS: 0, Status.NOT_CONFIGURED: 0, Status.WARN: 1,
            Status.FAIL: 2}[report.status]


if __name__ == "__main__":
    sys.exit(main())
