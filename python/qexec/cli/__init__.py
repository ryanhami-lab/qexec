"""QExec command-line interface (WP7-RELEASE).

A single ``qexec`` entry point (see ``[project.scripts]`` in ``pyproject.toml``) exposes the
release-layer subcommands over the synthetic research replay engine:

* ``synth``            -- generate synthetic sessions
* ``replay validate``  -- verify a session's checksums and replay it through ``ReferenceBook``
* ``tasks build``      -- build the policy-independent task manifest for a session
* ``experiment run``   -- run the full research study (:func:`qexec.experiments.run_study`)
* ``demo``             -- end-to-end synthetic study + Markdown report (used by the CI smoke stage)
* ``trace task``       -- print the causal event trace of one (task, policy) world
* ``analysis report``  -- render the Markdown research report for a finished study

All data is SYNTHETIC and every command says so where applicable. No command makes any network
call. Exit codes: ``0`` success, ``2`` usage error, ``1`` runtime failure (see
:data:`qexec.cli.main.EXIT_OK` / ``EXIT_USAGE`` / ``EXIT_FAILURE``).
"""

from __future__ import annotations

from qexec.cli.main import main

__all__ = ["main"]
