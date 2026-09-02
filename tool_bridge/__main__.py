"""Canonical ``python3 -m tool_bridge`` entry point; no installation required."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
