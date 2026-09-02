"""ArgumentParser enforcing the CLI contract that usage errors exit with 1.

The standard argparse parser exits with status 2 for invalid arguments, while
this CLI exposes only its documented status codes. The subclass changes only
the exit code and preserves usage text and stderr behavior. Subparsers inherit
the class automatically, so their errors follow the same contract.
"""
from __future__ import annotations

import argparse
import sys


class ArgumentParser(argparse.ArgumentParser):
    """Behave like argparse.ArgumentParser but exit with 1 on usage errors."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")
