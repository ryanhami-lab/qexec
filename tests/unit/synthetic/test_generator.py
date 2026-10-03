"""Unit tests for the synthetic MBO generator (WP1-SYNTH).

These tests reconstruct an independent book from the emitted records (never importing the
generator's internal book) and verify the Databento MBO conventions of contract section 2.1,
the market-dynamics requirements (fill support with both queue-depletion and trade-through,
a positive imbalance->mid signal), halt behaviour, timestamp monotonicity, and runtime.
"""

from __future__ import annotations

import time
from collections import OrderedDict

import numpy as np

from qexec.core.records_io import iter_records, read_meta, verify_session
from qexec.core.types import Action, RecordFlag, Side, TradingStatus
from qexec.synthetic.generator import (
    STUDY_EPOCH_NS,
    SyntheticParams,
    generate_session,
    generate_study,
    write_synthetic_session,
)

# A fixed epoch for standalone sessions (2026-01-05T14:30:00Z).
_START = STUDY_EPOCH_NS


def _params(seed: int = 42, duration_s: int = 60) -> SyntheticParams:
    return SyntheticParams(seed=seed, session_id="SYN-0001", start_ns=_START, duration_s=duration_s)


# ---------------------------------------------------------------------------
# Independent book reconstruction (does not import the generator's book)
# ---------------------------------------------------------------------------


class IndependentBook:
    """Reconstructs the resting book from emitted mutating records only.

    Levels keep insertion (FIFO) order via ``OrderedDict`` so queue position is faithful.
    """

    def __init__(self, tick_size: int) -> None:
        self.ts = tick_size
        self.bids: dict[int, OrderedDict[int, int]] = {}
        self.asks: dict[int, OrderedDict[int, int]] = {}
        self.loc: dict[int, tuple[Side, int]] = {}

    def _side(self, side: Side) -> dict[int, OrderedDict[int, int]]:
        return self.bids if side is Side.BID else self.asks

    def apply(self, r) -> None:
        tick = r.price_fixed // self.ts
        if r.action is Action.ADD:
            self._side(r.side).setdefault(tick, OrderedDict())[r.order_id] = r.quantity
            self.loc[r.order_id] = (r.side, tick)
        elif r.action is Action.CANCEL:
            if r.order_id not in self.loc:
                return
            side, tk = self.loc[r.order_id]
            level = self._side(side)[tk]
            level[r.order_id] -= r.quantity
            if level[r.order_id] <= 0:
                del level[r.order_id]
                del self.loc[r.order_id]
                if not level:
                    del self._side(side)[tk]
        elif r.action is Action.MODIFY:
            if r.order_id not in self.loc:
                return
            side, tk = self.loc[r.order_id]
            level = self._side(side)[tk]
            old = level[r.order_id]
            if tick == tk and r.quantity <= old:
                level[r.order_id] = r.quantity  # priority-retaining size decrease
            else:
                del level[r.order_id]
                if not level:
                    del self._side(side)[tk]
                self._side(side).setdefault(tick, OrderedDict())[r.order_id] = r.quantity
                self.loc[r.order_id] = (side, tick)
        # TRADE / FILL never mutate the book.

    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None

    def level_total(self, side: Side, tick: int) -> int:
        level = self._side(side).get(tick)
        return sum(level.values()) if level else 0


def _events(records: list) -> list[list]:
    """Group records into events, each terminated by exactly one LAST-flagged record."""
    out: list[list] = []
    cur: list = []
    for r in records:
        cur.append(r)
        if r.flags & RecordFlag.LAST:
            out.append(cur)
            cur = []
    assert not cur, "trailing records not closed by LAST"
    return out


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_identical_records() -> None:
    _, _, r1, s1 = generate_session(_params(seed=7))
    _, _, r2, s2 = generate_session(_params(seed=7))
    assert r1 == r2
    assert s1 == s2


def test_different_seed_differs() -> None:
    _, _, r1, _ = generate_session(_params(seed=7))
    _, _, r2, _ = generate_session(_params(seed=8))
    assert r1 != r2


# ---------------------------------------------------------------------------
# Schema / flag conventions (contract 2.1)
# ---------------------------------------------------------------------------


def test_every_event_ends_in_exactly_one_last() -> None:
    _, _, records, _ = generate_session(_params())
    for ev in _events(records):
        assert ev[-1].flags & RecordFlag.LAST
        # Exactly one LAST per event (only the final record).
        assert sum(1 for r in ev if r.flags & RecordFlag.LAST) == 1


def test_snapshot_is_first_event() -> None:
    _, _, records, _ = generate_session(_params())
    events = _events(records)
    first = events[0]
    assert all(r.flags & RecordFlag.SNAPSHOT for r in first)
    assert all(r.action is Action.ADD for r in first)
    # No later record carries the SNAPSHOT flag.
    for ev in events[1:]:
        assert not any(r.flags & RecordFlag.SNAPSHOT for r in ev)


def test_dataset_and_instrument_constants() -> None:
    meta, inst, records, _ = generate_session(_params())
    assert meta.dataset_id == "SYNTH.MBO"
    assert inst.instrument_id == 1
    assert inst.symbol == "SYNZ6"
    assert inst.tick_size_fixed == 250_000_000
    assert inst.multiplier == 50
    for r in records:
        assert r.dataset_id == "SYNTH.MBO"
        assert r.publisher_id == 1
        assert r.instrument_id == 1


def test_source_ordinal_is_running_index() -> None:
    _, _, records, _ = generate_session(_params())
    assert [r.source_ordinal for r in records] == list(range(len(records)))


def test_order_ids_unique_for_adds() -> None:
    _, _, records, _ = generate_session(_params())
    add_ids = [r.order_id for r in records if r.action is Action.ADD]
    assert len(add_ids) == len(set(add_ids))
    assert all(oid != 0 for oid in add_ids)


def test_fill_followed_by_matching_cancel() -> None:
    """Each FILL is immediately followed by a CANCEL with the same order id and quantity."""
    _, _, records, _ = generate_session(_params())
    n_fills = 0
    for i, r in enumerate(records):
        if r.action is Action.FILL:
            n_fills += 1
            nxt = records[i + 1]
            assert nxt.action is Action.CANCEL
            assert nxt.order_id == r.order_id
            assert nxt.quantity == r.quantity
            assert nxt.price_fixed == r.price_fixed
            assert nxt.side == r.side
    assert n_fills > 0


def test_trade_conventions_and_qty_sums() -> None:
    """TRADE uses order_id 0, aggressor side; its qty equals the sum of its FILLs per level."""
    _, _, records, _ = generate_session(_params())
    n_trades = 0
    for ev in _events(records):
        # Walk the event: each TRADE is followed by FILL/CANCEL pairs for the same price level
        # until the next TRADE or the end of the event.
        i = 0
        while i < len(ev):
            r = ev[i]
            if r.action is Action.TRADE:
                n_trades += 1
                assert r.order_id == 0
                assert r.side in (Side.BID, Side.ASK)
                trade_price = r.price_fixed
                aggressor = r.side
                i += 1
                fill_sum = 0
                while i < len(ev) and ev[i].action in (Action.FILL, Action.CANCEL):
                    f = ev[i]
                    if f.action is Action.FILL:
                        # FILL is on the resting (opposite) side, at the trade price, never
                        # referencing a resting order id of 0.
                        assert f.side == aggressor.opposite()
                        assert f.price_fixed == trade_price
                        assert f.order_id != 0
                        fill_sum += f.quantity
                    i += 1
                assert fill_sum == r.quantity
            else:
                i += 1
    assert n_trades > 0


def test_trade_never_references_resting_order_id() -> None:
    _, _, records, _ = generate_session(_params())
    assert all(r.order_id == 0 for r in records if r.action is Action.TRADE)


def test_both_modify_branches_occur() -> None:
    """Priority-retaining (same price, smaller/equal qty) and priority-losing modifies occur."""
    _, _, records, _ = generate_session(_params())
    ts = 250_000_000
    book = IndependentBook(ts)
    retain = lose = 0
    for r in records:
        if r.action is Action.MODIFY and r.order_id in book.loc:
            _, old_tick = book.loc[r.order_id]
            old_qty = book._side(book.loc[r.order_id][0])[old_tick][r.order_id]
            new_tick = r.price_fixed // ts
            if new_tick == old_tick and r.quantity <= old_qty:
                retain += 1
            else:
                lose += 1
        book.apply(r)
    assert retain > 0
    assert lose > 0


# ---------------------------------------------------------------------------
# Internal-book invariants from emitted records
# ---------------------------------------------------------------------------


def test_no_crossed_book_and_nonnegative_sizes() -> None:
    _, _, records, status = generate_session(_params())
    ts = 250_000_000
    book = IndependentBook(ts)
    # Determine trading status at each event boundary from status events.
    status_by_time = sorted(status, key=lambda s: s.event_time_ns)
    for ev in _events(records):
        for r in ev:
            book.apply(r)
        # Nonnegative sizes everywhere.
        for side in (book.bids, book.asks):
            for level in side.values():
                for q in level.values():
                    assert q > 0
        # No crossed book at event boundaries while trading.
        et = ev[-1].event_time_ns
        trading = True
        for s in status_by_time:
            if s.event_time_ns <= et:
                trading = s.status is TradingStatus.TRADING
            else:
                break
        bb, ba = book.best_bid(), book.best_ask()
        if trading and bb is not None and ba is not None:
            assert bb < ba, f"crossed book at {et}: bid {bb} >= ask {ba}"


# ---------------------------------------------------------------------------
# Halts
# ---------------------------------------------------------------------------


def test_halts_occur_and_no_activity_while_halted() -> None:
    _, _, records, status = generate_session(_params(duration_s=120))
    halted = [s for s in status if s.status is TradingStatus.HALTED]
    resumed = [s for s in status if s.status is TradingStatus.TRADING]
    assert len(halted) >= 1
    # First status event is the explicit opening TRADING; every HALT is then paired with a
    # resume.
    first = min(status, key=lambda s: s.event_time_ns)
    assert first.status is TradingStatus.TRADING
    assert len(halted) == len(resumed) - 1
    # Build halted intervals [halt_time, resume_time).
    status_sorted = sorted(status, key=lambda s: s.event_time_ns)
    intervals: list[tuple[int, int]] = []
    open_halt: int | None = None
    for s in status_sorted:
        if s.status is TradingStatus.HALTED:
            open_halt = s.event_time_ns
        elif s.status is TradingStatus.TRADING and open_halt is not None:
            intervals.append((open_halt, s.event_time_ns))
            open_halt = None
    assert intervals
    for lo, hi in intervals:
        assert hi > lo
        for r in records:
            # No order activity strictly inside a halt interval.
            if lo < r.event_time_ns < hi:
                raise AssertionError(f"record at {r.event_time_ns} inside halt [{lo},{hi})")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_event_time_strictly_nondecreasing_and_capture_after_event() -> None:
    _, _, records, _ = generate_session(_params())
    prev_event = None
    prev_capture = None
    for r in records:
        assert r.capture_time_ns > r.event_time_ns  # positive jitter
        if prev_event is not None:
            assert r.event_time_ns >= prev_event
        if prev_capture is not None:
            assert r.capture_time_ns >= prev_capture
        prev_event = r.event_time_ns
        prev_capture = r.capture_time_ns


def test_event_time_constant_within_event_and_nondecreasing_across() -> None:
    _, _, records, _ = generate_session(_params())
    events = _events(records)
    prev = None
    for ev in events:
        times = {r.event_time_ns for r in ev}
        assert len(times) == 1  # constant within an event
        t = ev[0].event_time_ns
        if prev is not None:
            assert t >= prev  # nondecreasing across events (contract 2.1)
        prev = t


# ---------------------------------------------------------------------------
# Fill-support property (independent FIFO checker)
# ---------------------------------------------------------------------------


def _fifo_fill_study(records: list, tick_size: int, sample_times: list[int]):
    """Insert a virtual 1-lot at the back of the best bid at each sampled time and measure
    whether it fills within one second, using only the emitted records.

    A fill occurs by:

    * **queue depletion** - cumulative execution (FILL+CANCEL pairs) at the virtual's price
      level reaches the quantity resting ahead of it; or
    * **trade-through** - an aggressive sell (TRADE, aggressor side ASK) prices strictly
      through the virtual's limit (at a tick below it).

    Returns (n_total, n_filled, n_depletion, n_trade_through).
    """
    events = _events(records)
    book = IndependentBook(tick_size)
    sample_times = sorted(sample_times)
    si = 0
    virtuals: list[dict] = []
    n_filled = n_depl = n_tt = 0

    for ev in events:
        et = ev[0].event_time_ns
        # Place any virtuals whose sample time has arrived, at the back of the current bid.
        while si < len(sample_times) and sample_times[si] <= et:
            bb = book.best_bid()
            if bb is not None:
                virtuals.append(
                    {
                        "tick": bb,
                        "ahead": book.level_total(Side.BID, bb),
                        "deadline": sample_times[si] + 1_000_000_000,
                        "done": False,
                    }
                )
            si += 1

        # Identify execution-cancels (a CANCEL immediately preceded by a matching FILL).
        exec_cancel = set()
        for i in range(1, len(ev)):
            if (
                ev[i].action is Action.CANCEL
                and ev[i - 1].action is Action.FILL
                and ev[i].order_id == ev[i - 1].order_id
                and ev[i].quantity == ev[i - 1].quantity
            ):
                exec_cancel.add(i)

        for idx, r in enumerate(ev):
            for v in virtuals:
                if v["done"] or r.event_time_ns > v["deadline"]:
                    continue
                if r.action is Action.TRADE and r.side is Side.ASK:
                    if r.price_fixed // tick_size < v["tick"]:
                        v["done"] = True
                        v["mode"] = "tt"
                elif (
                    r.action is Action.CANCEL
                    and r.side is Side.BID
                    and idx in exec_cancel
                    and r.price_fixed // tick_size == v["tick"]
                ):
                    v["ahead"] -= r.quantity
                    if v["ahead"] <= 0:
                        v["done"] = True
                        v["mode"] = "depl"
        for r in ev:
            book.apply(r)

    for v in virtuals:
        if v.get("done"):
            n_filled += 1
            if v.get("mode") == "depl":
                n_depl += 1
            elif v.get("mode") == "tt":
                n_tt += 1
    return len(virtuals), n_filled, n_depl, n_tt


def test_fill_support_rate_and_both_mechanisms() -> None:
    # A 10-minute session: fill rate within a broad band and both mechanisms present.
    _, _, records, _ = generate_session(_params(seed=42, duration_s=600))
    ts = 250_000_000
    rng = np.random.default_rng(12345)
    start = records[0].event_time_ns
    end = records[-1].event_time_ns - 1_000_000_000
    samples = [int(x) for x in rng.uniform(start, end, 400)]
    total, filled, depl, tt = _fifo_fill_study(records, ts, samples)
    assert total >= 300
    rate = filled / total
    # Broad acceptance band (the calibrated target is ~15-50%).
    assert 0.05 <= rate <= 0.80, f"fill rate {rate:.3f} outside [0.05, 0.80]"
    assert depl >= 1, "no queue-depletion fill occurred"
    assert tt >= 1, "no trade-through fill occurred"


# ---------------------------------------------------------------------------
# Stationarity (the book must not accumulate without bound)
# ---------------------------------------------------------------------------


def _resting_count_by_second(records: list, tick_size: int) -> dict[int, int]:
    """Reconstruct the number of live resting orders at each event, bucketed by whole second.

    Uses only the emitted mutating records via the independent book, so it never consults the
    generator's internal state. The value stored for a second is the live-order count at the
    last event in that second.
    """
    book = IndependentBook(tick_size)
    start = records[0].event_time_ns
    by_second: dict[int, int] = {}
    for ev in _events(records):
        for r in ev:
            book.apply(r)
        n_live = sum(len(level) for level in (*book.bids.values(), *book.asks.values()))
        sec = int((ev[-1].event_time_ns - start) / 1_000_000_000)
        by_second[sec] = n_live
    return by_second


def test_book_is_stationary_over_a_long_session() -> None:
    """The resting book must be stationary: resting-order count and top-of-book queue stable.

    Regression guard for the non-stationary-book defect (resting orders grew ~linearly from a
    few thousand to tens of thousands over a 600 s session). With the per-order cancellation
    hazard the expected resting depth converges, so the mean resting-order count in the last
    100 s is within a 0.7-1.4 ratio of the 100-200 s window, and the median best-level queue
    stays in a sane band. Deterministic seed so the bounds are reproducible.
    """
    _, _, records, _ = generate_session(_params(seed=42, duration_s=600))
    ts = 250_000_000
    by_second = _resting_count_by_second(records, ts)

    early = [n for s, n in by_second.items() if 100 <= s < 200]
    late = [n for s, n in by_second.items() if 500 <= s < 600]
    assert early and late, "need both the 100-200 s and 500-600 s windows populated"
    mean_early = float(np.mean(early))
    mean_late = float(np.mean(late))
    ratio = mean_late / mean_early
    # Bounded, non-growing: a linearly accumulating book would give a ratio far above 1.4.
    assert 0.7 <= ratio <= 1.4, (
        f"resting-order count not stationary: mean(500-600s)={mean_late:.0f} "
        f"mean(100-200s)={mean_early:.0f} ratio={ratio:.2f} outside [0.7, 1.4]"
    )
    # The resting-order count stays bounded (an accumulating book exceeded ~25k here before).
    assert max(by_second.values()) < 2000, (
        f"resting-order count {max(by_second.values())} too large (book accumulating)"
    )

    # Median best-level (best-bid) queue in the back half of the session within ~[5, 80].
    book = IndependentBook(ts)
    start = records[0].event_time_ns
    best_bid_q: list[int] = []
    for ev in _events(records):
        for r in ev:
            book.apply(r)
        sec = int((ev[-1].event_time_ns - start) / 1_000_000_000)
        bb = book.best_bid()
        if bb is not None and sec >= 300:
            best_bid_q.append(book.level_total(Side.BID, bb))
    assert best_bid_q, "no best-bid queue samples in the back half"
    median_bq = float(np.median(best_bid_q))
    assert 5.0 <= median_bq <= 80.0, f"median best-level queue {median_bq:.0f} outside [5, 80]"

    # Event rate still in the intended 50-300/s band.
    dur_s = (records[-1].event_time_ns - records[0].event_time_ns) / 1e9
    n_events = sum(1 for r in records if r.flags & RecordFlag.LAST)
    rate = n_events / dur_s
    assert 50.0 <= rate <= 300.0, f"event rate {rate:.0f}/s outside [50, 300]"

    # Fill support and both mechanisms still present at the stationary calibration (15-50%
    # target; broad lower guard so the deterministic seed is robust).
    rng = np.random.default_rng(12345)
    end = records[-1].event_time_ns - 1_000_000_000
    samples = [int(x) for x in rng.uniform(start, end, 400)]
    total, filled, depl, tt = _fifo_fill_study(records, ts, samples)
    fill_rate = filled / total
    assert 0.10 <= fill_rate <= 0.50, f"fill rate {fill_rate:.3f} outside [0.10, 0.50]"
    assert depl >= 1, "no queue-depletion fill at the stationary calibration"
    assert tt >= 1, "no trade-through fill at the stationary calibration"


# ---------------------------------------------------------------------------
# Imbalance signal
# ---------------------------------------------------------------------------


def test_imbalance_predicts_next_mid_change_positive() -> None:
    _, _, records, _ = generate_session(_params(seed=42, duration_s=600))
    ts = 250_000_000
    book = IndependentBook(ts)
    imb: list[float] = []
    mid: list[float] = []
    for ev in _events(records):
        for r in ev:
            book.apply(r)
        bb, ba = book.best_bid(), book.best_ask()
        if bb is not None and ba is not None and bb < ba:
            bq = book.level_total(Side.BID, bb)
            aq = book.level_total(Side.ASK, ba)
            tot = bq + aq
            imb.append((bq - aq) / tot if tot else 0.0)
            mid.append((bb + ba) / 2.0)
    imb_a = np.array(imb)
    mid_a = np.array(mid)
    # Correlate current imbalance with the mid change over the next K events (aggregates the
    # signal above the per-event noise where most consecutive mid changes are zero).
    k = 20
    future = np.array([mid_a[i + k] - mid_a[i] for i in range(len(mid_a) - k)])
    x = imb_a[: len(future)]
    assert x.std() > 0 and future.std() > 0
    corr = float(np.corrcoef(x, future)[0, 1])
    assert corr > 0.05, f"imbalance->mid correlation {corr:.3f} not positive"


# ---------------------------------------------------------------------------
# write / study API
# ---------------------------------------------------------------------------


def test_write_and_read_back(tmp_path) -> None:
    directory = write_synthetic_session(_params(duration_s=30), tmp_path)
    verify_session(directory)
    meta = read_meta(directory)
    assert meta.session_id == "SYN-0001"
    back = list(iter_records(directory))
    _, _, records, _ = generate_session(_params(duration_s=30))
    assert back == records


def test_generate_study_layout(tmp_path) -> None:
    paths = generate_study(tmp_path, n_sessions=3, base_seed=100, duration_s=20)
    assert [p.name for p in paths] == ["SYN-0001", "SYN-0002", "SYN-0003"]
    metas = [read_meta(p) for p in paths]
    assert [m.session_id for m in metas] == ["SYN-0001", "SYN-0002", "SYN-0003"]
    # Consecutive synthetic days from the fixed epoch.
    one_day = 86_400 * 1_000_000_000
    assert metas[0].start_ns == STUDY_EPOCH_NS
    assert metas[1].start_ns == STUDY_EPOCH_NS + one_day
    assert metas[2].start_ns == STUDY_EPOCH_NS + 2 * one_day


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def test_ten_minute_session_runtime() -> None:
    t0 = time.perf_counter()
    _, _, records, _ = generate_session(_params(seed=1, duration_s=600))
    elapsed = time.perf_counter() - t0
    assert elapsed < 30.0, f"10-minute session took {elapsed:.1f}s (>= 30s)"
    # Event rate in the intended 50-300 events/second band.
    dur_s = (records[-1].event_time_ns - records[0].event_time_ns) / 1e9
    n_events = sum(1 for r in records if r.flags & RecordFlag.LAST)
    rate = n_events / dur_s
    assert 50.0 <= rate <= 300.0, f"event rate {rate:.0f}/s outside [50, 300]"
