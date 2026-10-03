"""A study compares the same admitted tasks across its declared scenarios."""

from pathlib import Path

import _engine_helpers as H  # noqa: N812

from qexec.core.config import ExperimentConfig
from qexec.core.types import TradingStatus
from qexec.policies.baselines import B1Policy
from qexec.sim.engine import SessionEngine


def test_study_scenarios_share_delayed_status_exclusion(tmp_path: Path) -> None:
    start = 1_000_000_000_000
    sid = "common-status"
    arrival = H.first_arrival_ns(sid, start, H.S, H.S)
    b = H.SessionBuilder(sid, start)
    b.snapshot(start, [(H.BID, 5, 1)], [(H.ASK, 5, 2)])
    b.add_status(TradingStatus.HALTED, arrival - 2 * H.MS, arrival - 2 * H.MS)
    b.add_status(TradingStatus.TRADING, arrival - 500 * H.US, arrival - 500 * H.US)
    b.add_event(arrival + 3 * H.S, [H.RecordSpec(H.Action.NONE, H.Side.NONE, 0, 0, 0)])
    path = b.write(tmp_path)
    for active in ("L1", "L6"):
        config = ExperimentConfig("common", latency_id=active, planned_scenario_ids=("L1", "L6"))
        outputs = SessionEngine(path, config, [B1Policy()]).run()
        assert outputs.tasks.get_column("eligible").to_list() == [False, False]
        assert outputs.task_results.height == 0


def test_declared_scenarios_survive_config_roundtrip() -> None:
    cfg = ExperimentConfig("common", planned_scenario_ids=("L1", "L6"))
    assert ExperimentConfig.from_dict(cfg.to_dict()) == cfg
