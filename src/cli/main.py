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
    args = parser.parse_args(argv)
    if Path(sys.prefix).name != "ridesharing" or sys.version_info[:3] != (3, 9, 25):
        parser.error(f"Requires ridesharing / Python 3.9.25 (current: {sys.executable})")
    try:
        if args.command == "simulate":
            return simulate(args)
        if args.command == "validate-inputs":
            print(json.dumps(validate_inputs(load_config(args.config)), indent=2, ensure_ascii=True))
        elif args.command == "archive-inputs":
            print(json.dumps(archive_inputs(load_config(args.config)), indent=2, ensure_ascii=True))
        elif args.command == "prepare-network":
            print(prepare_network(args.config))
        else:
            print(prepare_data(args.config))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
