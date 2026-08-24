"""CLI entry point for tap-nextdoor."""

from tap_nextdoor.tap import TapNextdoor

if __name__ == "__main__":
    TapNextdoor.cli()
