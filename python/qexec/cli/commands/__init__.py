"""Subcommand implementations for the ``qexec`` CLI (WP7-RELEASE).

Each command is a small function taking the parsed :class:`argparse.Namespace` and returning an
integer process exit code (``0`` ok, ``1`` failure; usage errors are raised as
:class:`qexec.cli.commands.CommandUsageError` and mapped to exit code ``2`` by the dispatcher).
Commands never make network calls and always print that the data is SYNTHETIC where applicable.
"""

from __future__ import annotations


class CommandUsageError(Exception):
    """A recoverable usage error (bad argument value); dispatcher maps it to exit code 2."""


class CommandError(Exception):
    """A runtime failure during command execution; dispatcher maps it to exit code 1."""


SYNTHETIC_BANNER = "NOTE: all QExec data is SYNTHETIC (software validation, not a market finding)."

__all__ = ["SYNTHETIC_BANNER", "CommandError", "CommandUsageError"]
