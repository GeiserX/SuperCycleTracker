"""The `sct` command. The subcommands (run, check, predict, import-apple-health, say) land with
the bot; until then the entry point only reports the version."""

from __future__ import annotations

import argparse

from supercycletracker import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sct", description="SuperCycleTracker")
    parser.add_argument("--version", action="version", version=f"sct {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
