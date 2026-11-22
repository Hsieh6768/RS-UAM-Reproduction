import argparse
import json
from pathlib import Path
import sys

from src.common.config import load_config
from src.data.p0 import archive_inputs, prepare_data, prepare_network, validate_inputs


def main(argv=None):
    from src.cli.experiment import add_simulate, simulate
    parser = argparse.ArgumentParser(description="RS-UAM input audit and rolling experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-inputs", "archive-inputs", "prepare-network", "prepare-data"):
        subparser = commands.add_parser(name)
        subparser.add_argument("--config", default="configs/p0.json", type=Path)
        if name == "validate-inputs":
            subparser.add_argument("--dry-run", action="store_true", help="Input validation is always read-only")
    add_simulate(commands)
    audit = commands.add_parser("audit-scc100", help="Read-only full SCC100 data gate after manual preparation")
    audit.add_argument("--config", type=Path, default=Path("configs/debug_10min_scc100.yaml"))
    args = parser.parse_args(argv)
    if Path(sys.prefix).name != "ridesharing" or sys.version_info[:3] != (3, 9, 25):
        parser.error(f"Requires ridesharing / Python 3.9.25 (current: {sys.executable})")
    try:
        if args.command == "simulate":
            return simulate(args)
        if args.command == "audit-scc100":
            from contextlib import redirect_stdout
            from io import StringIO
            from src.data.scc100_audit import audit_scc100
            result = audit_scc100(args.config)
            preview = StringIO()
            with redirect_stdout(preview):
                simulate(parser.parse_args(["simulate", "--config", str(args.config), "--dry-run"]))
            result["e10_dry_run"] = json.loads(preview.getvalue())
            print(json.dumps(result, indent=2))
            return int(result["status"] != "DATA_GATE PASS")
        if args.command == "validate-inputs":
            print(json.dumps(validate_inputs(load_config(args.config)), indent=2, ensure_ascii=True))
        elif args.command == "archive-inputs":
            print(json.dumps(archive_inputs(load_config(args.config)), indent=2, ensure_ascii=True))
        elif args.command == "prepare-network":
            print(prepare_network(args.config))
        else:
            print(prepare_data(args.config))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        label = "DATA_GATE FAIL" if args.command == "audit-scc100" else args.command
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
