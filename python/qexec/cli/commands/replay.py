"""``qexec replay validate`` -- verify a session and replay it through ``ReferenceBook``.

Verifies the session's SHA-256 checksums, replays every batch through the authoritative
:class:`~qexec.reference.book.ReferenceBook` (committing each batch so boundary invariants run),
and prints an anomaly / invariant summary. Exits nonzero if any anomaly or invariant violation
is detected or if checksum verification fails. All data is SYNTHETIC.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from qexec.adapters.batches import iter_session_batches
from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError
from qexec.core.records_io import read_instrument, read_status, verify_session
from qexec.core.types import Action, BatchKind, QualityFlag, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import execution_groups, valid_resting_allocation


def run_replay_validate(args: argparse.Namespace) -> int:
    """Verify checksums and replay the session; nonzero exit on anomalies/violations."""
    session_dir = Path(args.session)
    if not session_dir.is_dir():
        raise CommandUsageError(f"--session must be an existing directory, got {session_dir}")

    print(SYNTHETIC_BANNER)
    print(f"Validating SYNTHETIC session {session_dir}")

    # Checksum verification (reads the immutable session files).
    try:
        verify_session(session_dir)
    except (ValueError, FileNotFoundError) as exc:
        raise CommandError(f"checksum verification failed: {exc}") from exc
    print("  checksums: OK")

    instrument = read_instrument(session_dir)
    book = ReferenceBook(instrument)
    book.set_status(TradingStatus.TRADING)

    n_batches = 0
    n_records = 0
    n_init = 0
    quality_flag_counts: dict[str, int] = {}
    invariant_violations: list[str] = []
    status = sorted(read_status(session_dir), key=lambda s: int(s.event_time_ns))
    status_index = 0

    try:
        for batch in iter_session_batches(session_dir):
            n_batches += 1
            n_records += len(batch.records)
            if batch.kind is BatchKind.INITIALIZATION:
                n_init += 1
            for flag in batch.quality_flags:
                name = flag.name if isinstance(flag, QualityFlag) else str(flag)
                quality_flag_counts[name] = quality_flag_counts.get(name, 0) + 1
            while (
                status_index < len(status)
                and status[status_index].event_time_ns <= batch.exchange_proxy_time
            ):
                book.set_status(status[status_index].status)
                status_index += 1
            invalid_evidence = execution_groups(batch).invalid
            book.begin_batch(batch)
            for record in batch.records:
                if record.action is Action.FILL:
                    invalid_evidence |= (
                        book.status is not TradingStatus.TRADING
                        or not valid_resting_allocation(record, book)
                    )
                book.apply_record(record)
            book.commit_batch(batch)
            if invalid_evidence:
                evidence_flag = "INVALID_EXECUTION_EVIDENCE"
                quality_flag_counts[evidence_flag] = quality_flag_counts.get(evidence_flag, 0) + 1
            violations = book.validate_invariants()
            if violations:
                invariant_violations.extend(f"batch {batch.batch_id}: {msg}" for msg in violations)
    except Exception as exc:
        raise CommandError(f"replay failed at batch {n_batches}: {exc}") from exc

    anomalies = list(book.anomalies)

    print(f"  batches replayed: {n_batches} ({n_init} initialization)")
    print(f"  records applied:  {n_records}")
    bid, ask = book.best_bid_ask()
    bid_px = bid.price_fixed if bid is not None else None
    ask_px = ask.price_fixed if ask is not None else None
    print(f"  final best bid/ask (fixed): {bid_px} / {ask_px}")
    if quality_flag_counts:
        flags_summary = ", ".join(f"{k}={v}" for k, v in sorted(quality_flag_counts.items()))
        print(f"  batch quality flags: {flags_summary}")
    else:
        print("  batch quality flags: none")
    print(f"  book anomalies: {len(anomalies)}")
    for anomaly in anomalies[:20]:
        print(f"    - {anomaly.kind} @ batch {anomaly.batch_id}: {anomaly.detail}")
    if len(anomalies) > 20:
        print(f"    ... and {len(anomalies) - 20} more")
    print(f"  invariant violations: {len(invariant_violations)}")
    for violation in invariant_violations[:20]:
        print(f"    - {violation}")

    if anomalies or invariant_violations:
        print("RESULT: ANOMALIES DETECTED (session not clean). Data is SYNTHETIC.")
        return 1
    if quality_flag_counts:
        # R15: a session carrying ANY batch quality flag (e.g. TRUNCATED / corrupt) is not clean,
        # even when the ReferenceBook replayed without an anomaly or invariant violation.
        total_flags = sum(quality_flag_counts.values())
        print(
            f"RESULT: QUALITY FLAGS DETECTED ({total_flags} across batches; session not clean). "
            "Data is SYNTHETIC."
        )
        return 1
    print("RESULT: OK (no anomalies, no invariant violations). Data is SYNTHETIC.")
    return 0
