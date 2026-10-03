"""``qexec synth`` -- generate synthetic sessions (SYNTHETIC data only)."""

from __future__ import annotations

import argparse
from pathlib import Path

from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError
from qexec.synthetic.generator import (
    STUDY_EPOCH_NS,
    SyntheticParams,
    write_synthetic_session,
)

_ONE_DAY_NS = 86_400 * 1_000_000_000


def run_synth(args: argparse.Namespace) -> int:
    """Generate ``--sessions`` synthetic sessions under ``--out``.

    Session ids are ``SYN-0001..`` on consecutive synthetic days from the study epoch; seeds are
    ``--seed + index`` (so a given ``--seed`` reproduces an identical set).
    """
    out_dir = Path(args.out)
    n_sessions: int = args.sessions
    duration_s: int = args.duration
    base_seed: int = args.seed

    if n_sessions < 1:
        raise CommandUsageError("--sessions must be >= 1")
    if duration_s <= 0:
        raise CommandUsageError("--duration must be a positive number of seconds")

    print(SYNTHETIC_BANNER)
    print(
        f"Generating {n_sessions} SYNTHETIC session(s), {duration_s}s each, "
        f"base seed {base_seed}, into {out_dir}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for i in range(n_sessions):
        session_id = f"SYN-{i + 1:04d}"
        target = out_dir / session_id
        if target.exists():
            raise CommandError(
                f"refusing to overwrite existing session {target} "
                "(sessions are immutable; choose a fresh --out)"
            )
        params = SyntheticParams(
            seed=base_seed + i,
            session_id=session_id,
            start_ns=STUDY_EPOCH_NS + i * _ONE_DAY_NS,
            duration_s=duration_s,
        )
        path = write_synthetic_session(params, out_dir)
        written.append(path)
        print(f"  wrote {path}")

    print(f"Done: {len(written)} session(s) written. Data is SYNTHETIC.")
    return 0
