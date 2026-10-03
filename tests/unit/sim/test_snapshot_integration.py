"""Replacement snapshot state must agree in evaluator, client, and engine paths."""

from pathlib import Path

import _engine_helpers as H  # noqa: N812

from qexec.core.config import ExperimentConfig
from qexec.labels.price import MidSeries
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.sim.engine import SessionEngine


def test_recovered_snapshot_replaces_old_quotes_on_every_plane(tmp_path: Path) -> None:
    sid = "replace-everywhere"
    start = 1_000_000_000_000
    arrival = H.first_arrival_ns(sid, start, H.S, H.S)
    b = H.SessionBuilder(sid, start)
    b.snapshot(start, [(H.BID, 10, 1)], [(H.ASK, 10, 2)])
    b.add_event(arrival - 400 * H.MS, [H.RecordSpec(H.Action.CANCEL, H.Side.BID, H.BID, 1, 999)])
    # Complete bare snapshot: old bid=100 and ask=100.25 must disappear.
    bid, ask = H.BID - 2 * H.TICK, H.ASK + H.TICK
    recovery = arrival - 200 * H.MS
    b.snapshot(recovery, [(bid, 5, 11)], [(ask, 5, 12)])
    b.add_event(arrival + 3 * H.S, [H.RecordSpec(H.Action.NONE, H.Side.NONE, 0, 0, 0)])
    path = b.write(tmp_path)
    series = MidSeries.from_session(path)
    assert series.mid_at(recovery) == 200_000_000_000  # (99.5 + 100.5) * 1e9.
    assert series.source_window_valid(arrival, arrival + H.S)
    outputs = SessionEngine(path, ExperimentConfig("replace"), [B0Policy(), B1Policy()]).run()
    assert outputs.tasks.get_column("arrival_reference_mid2").to_list() == [bid + ask] * 2
    assert set(outputs.executions.get_column("price_fixed")) == {bid, ask}
    assert outputs.task_results.get_column("is_ticks_net").to_list() == [2.0] * 4
