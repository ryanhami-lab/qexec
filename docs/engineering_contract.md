# QExec Engineering Contract (synthetic-data build)

This document binds every implementation agent. It translates the three planning documents
(`01-product-spec.md`, `02-architecture-plan.md`, `03-implementation-plan.md`) into code-level
interfaces. The planning documents remain authoritative for research semantics; this document is
authoritative for module boundaries, signatures, and file formats. If they conflict, stop and
report the conflict instead of guessing.

## 0. Cost and data constraints (absolute)

- **No money may be spent.** No Databento account, API key, metadata/cost call, paid package,
  cloud service, or GitHub Actions run. No network calls from code or tests.
- All data is **synthetic**, produced locally by `qexec.synthetic`. Results from synthetic data
  are software validation, never market findings. Every report must say so.
- G0 (real-data feasibility), R01 real-data parts, and the final real-data holdout are
  **BLOCKED pending paid data and a vendor adapter**. Canonical session interfaces are implemented;
  vendor decoding and a real-session study entry point are additional work.

## 1. Working rules for agents

1. Work only inside your assigned git worktree and branch. Never touch `main` or another
   worktree. Never run destructive git commands (`reset --hard`, `push`, `clean -f`, `branch -D`).
2. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1` before every commit.
   The pre-commit hook runs it in fast mode; never bypass it with `--no-verify`.
3. Commit only files you own (section 3). Stage specific paths, not `git add .`.
4. Do not edit `python/qexec/core/*`. If a core contract is wrong or missing, report it in your
   final message; the manager changes core.
5. Never mark a test skipped/xfail to make the pipeline pass. Never weaken an assertion to make
   it pass. Never fabricate output. Report failures honestly.
6. Hand-derive golden fixtures in tests (explicit expected numbers in comments), not by
   recording the implementation's own output.
7. Code must pass `ruff format`, `ruff check`, `mypy --strict`. No `# type: ignore` without a
   comment explaining why. No `Any` in public signatures except where unavoidable (I/O dicts).
8. Determinism: no wall-clock, no unseeded randomness, no iteration over unordered sets where
   order affects output.

## 2. Core conventions (implemented in `python/qexec/core`, read-only for agents)

- `types.py`: `TimeNs` (int ns), `PriceFixed` (int, 1e-9 units), `Mid2` (bid+ask, exact),
  `Side`, `TaskSide` (value is sign `s`), `Action`, `RecordFlag`, `TradingStatus`,
  `InstrumentDefinition`, `StatusEvent`, `CanonicalRecord`, `OrderKey`, `EventBatch`,
  `BookLevel`, `BookSnapshot`.
- `config.py`: `LatencyScenario`, `LATENCY_SCENARIOS` (L0-L6), `guard_ns`, `checkpoint_valid`,
  `primary_price_tau_ns`, `arrival_spacing_ns`, menus, `ExperimentConfig`.
- `tasks.py`: `PolicyId`, `TaskStatus`, `CheckpointChoice`, `DecisionReason`, `FillMechanism`,
  `Liquidity`, `Task`, `Execution`, `TaskResult`.
- `records_io.py`: session directory format (write/verify/read/iter).

### 2.1 Source semantics (Databento CME MBO convention, emulated by the synthetic feed)

- An **event** is a run of records ending with a record whose flags include `LAST`.
- `ADD` inserts a new resting order at the back of its price level.
- `CANCEL` removes `quantity` from an existing order (partial allowed; to zero removes it).
  Partial cancel retains priority.
- `MODIFY` sets a new absolute price and quantity. **Price change or size increase loses
  priority** (moves to back of the new level); pure size decrease retains priority.
  `MODIFY` of an unknown order is an anomaly (diagnostic), not an implicit add, unless an
  adapter option `modify_unknown_as_add=True` is explicitly enabled.
- `CLEAR` removes all resting orders (reset). Subsequent `ADD`s with `SNAPSHOT` flag rebuild.
- An aggressive execution against resting liquidity is emitted, within one event, as:
  for each price level touched, one `TRADE` (side = aggressor side, price, total qty at that
  level, order_id 0), then per resting order filled one `FILL` (side = resting side, order_id,
  price, qty) followed immediately by the `CANCEL` that reduces that order by the same qty.
  `TRADE`/`FILL` never mutate the book; only the `CANCEL` does. Count execution once.
- Initialization: the session starts with an event of `SNAPSHOT`-flagged `ADD`s (in priority
  order) closed by `LAST`. Snapshot adds are not economic order flow.
- Event time `event_time_ns` (exchange proxy) and capture time `capture_time_ns` (>= event
  time). Batch `exchange_proxy_time` = max event time; `capture_complete_time` = max capture.

### 2.2 Simulation ordering (architecture 6.3)

Within one timestamp: (1) historical batch commit, (2) exchange command processing,
(3) client delivery, (4) decisions/timers. A command arriving at the exchange at time `t` is
processed after every historical batch with `exchange_proxy_time <= t`. Causal microsteps:
a zero-delay descendant is ordered after its cause (use a monotone sequence counter).

## 3. Module ownership and interfaces

Packages live under `python/qexec/`. Tests under `tests/unit/<pkg>/`, `tests/golden/`,
`tests/integration/`. Each work package owns only its listed paths.

### WP1-BOOK — `adapters/`, `reference/` (tests: `tests/unit/adapters`, `tests/unit/reference`, `tests/golden/book`)

```python
# qexec/adapters/batches.py
class BatchAssembler:
    def __init__(self, instrument_id: int) -> None: ...
    def feed(self, record: CanonicalRecord) -> EventBatch | None: ...  # returns a batch on LAST
    def finish(self) -> EventBatch | None: ...  # truncated trailing batch, flagged TRUNCATED
def iter_batches(records: Iterable[CanonicalRecord], instrument_id: int) -> Iterator[EventBatch]
def iter_session_batches(session_dir: Path) -> Iterator[EventBatch]  # verifies checksums
# Batch kind INITIALIZATION iff every record is SNAPSHOT-flagged (or CLEAR followed by snapshot adds).
# Quality flags also cover invalid per-record timestamps, bad receive timestamps and invalid records.

# qexec/adapters/diagnostics.py
@dataclass(frozen=True) class Anomaly: kind: str; batch_id: int; source_ordinal: int; detail: str
@dataclass(frozen=True) class DuplicateReport ...
def detect_duplicate_records(records) -> list[Anomaly]   # same (instrument, ordinal) or identical content+offset
def detect_overlapping_sessions(session_dirs) -> list[Anomaly]

# qexec/reference/book.py
@dataclass(frozen=True) class RestingOrder: key: OrderKey; side: Side; price_fixed: int; quantity: int; priority_seq: int
class ReferenceBook:
    def __init__(self, instrument: InstrumentDefinition, *, modify_unknown_as_add: bool = False) -> None
    def apply_record(self, record: CanonicalRecord) -> None   # mutating actions only; others no-op
    def begin_batch(self, batch: EventBatch) -> None         # complete snapshot replaces resting state
    def apply_batch(self, batch: EventBatch) -> None          # apply all records then commit
    def commit_batch(self, batch: EventBatch) -> BookSnapshot # validates invariants at boundary
    def snapshot(self, depth: int = 10) -> BookSnapshot
    def best_bid_ask(self) -> tuple[BookLevel | None, BookLevel | None]
    def get_order(self, key: OrderKey) -> RestingOrder | None
    def orders_at(self, side: Side, price_fixed: int) -> tuple[RestingOrder, ...]  # priority order
    def visible_quantity_ahead(self, key: OrderKey) -> int
    def validate_invariants(self) -> list[str]   # empty list == OK
    def set_status(self, status: TradingStatus) -> None
    @property def status(self) -> TradingStatus
    @property def anomalies(self) -> list[Anomaly]
    def copy(self) -> ReferenceBook   # deep, independent copy (used for checkpoint/restore)
```
Required tests: T01-T05, T34 (priority retention/loss), T35 (duplicates), T36 (ambiguity tagging),
plus a golden multi-event trace with hand-computed snapshots.

### WP1-SYNTH — `synthetic/` (tests: `tests/unit/synthetic`)

```python
# qexec/synthetic/generator.py
@dataclass(frozen=True) class SyntheticParams:
    seed: int; session_id: str; start_ns: int; duration_s: int
    tick_size_fixed: int = 250_000_000; multiplier: int = 50; initial_mid_ticks: int = 20_000
    levels: int = 10; mean_level_qty: float = 20.0 ... (rates for limit add, cancel, market orders,
    latent signal persistence, halts) -- document every parameter
def generate_session(params: SyntheticParams) -> tuple[SessionMeta, InstrumentDefinition, list[CanonicalRecord], list[StatusEvent]]
def write_synthetic_session(params: SyntheticParams, out_dir: Path) -> Path
def generate_study(out_root: Path, n_sessions: int, base_seed: int, duration_s: int) -> list[Path]
```
Requirements: exact emulation of section 2.1 (including TRADE/FILL/CANCEL triplets, multi-level
sweeps, snapshot init, LAST flags, capture >= event time with realistic jitter, strictly
nondecreasing event times, unique order IDs). Market dynamics must give: a latent short-horizon
drift correlated with order-book imbalance (so price models have learnable signal); queue
depletion fills and trade-throughs both occurring for orders joined at the touch within 1 s;
occasional short trading halts (status events) so halt handling is exercised; occasional
`MODIFY` (both priority-retaining and priority-losing). The generator keeps its own independent
internal book (do NOT import `qexec.reference`), and its unit tests verify its own invariants.
Default `duration_s` small enough that a 2-session quick demo runs in under ~60 s.

### WP-METRICS — `analysis/metrics.py`, `analysis/stats.py` (tests: `tests/unit/analysis`)

```python
def implementation_shortfall_ticks(side: int, fills: Sequence[tuple[int, int]], m0_mid2: int,
    tick_size_fixed: int, multiplier: int, fees_fixed: int, quantity: int = 1) -> tuple[float, float]  # (gross, net)
def markout_ticks(side: int, fill_price_fixed: int, future_mid2: int, tick_size_fixed: int) -> float
def deadline_value_ticks(side: int, fills_by_deadline: Sequence[tuple[int,int]], fees_fixed: int,
    z0_mid2: int, mT_mid2: int, tick_size_fixed: int, multiplier: int) -> float   # C_T, product section 8
def benchmark_offset_ticks(side: int, z0_mid2: int, m0_mid2: int, tick_size_fixed: int) -> float
# fees_fixed are 1e-9 currency units. fills are (quantity, price_fixed).
# stats.py
def common_completion_set(results: pl.DataFrame, baseline: str, candidate: str) -> pl.DataFrame
def session_paired_effect(results, baseline, candidate, metric="is_ticks_net") -> PairedEffect
    # per-session mean of (baseline - candidate) on common-completion tasks; sessions with none are
    # UNDEFINED (counted, excluded), equal-weight across sessions. Positive favors candidate.
def paired_miss_difference(results, baseline, candidate) -> PairedEffect  # all technically evaluable; positive = candidate worse
def session_block_bootstrap(results, baseline, candidate, statistic, n_boot, seed) -> (lo, hi)
    # resample whole sessions jointly for both policies (pairing preserved)
def zero_event_session_upper_bound(n_sessions: int, alpha: float = 0.05) -> float  # 1 - alpha**(1/D)
```
Required tests: T19 (signs, fees, half ticks — include product 7.1 synthetic example: 1.20
ticks buy and sell), T28 (offset identity `IS(m0)=IS(z0)+s(z0-m0)/delta` for completed tasks),
T32, T49 (adding a third policy's rows does not change the primary pair estimate), T50
(all-zero misses: no degenerate interval presented as a zero-risk claim; bound reported).

### WP2-OVERLAY — `sim/overlay.py`, `sim/evidence.py` (tests: `tests/unit/sim/test_overlay*.py`, `tests/golden/overlay`)

**Message types are in `qexec.core.messages`** (`CommandKind`, `ReportKind`, `ExchangeCommand`,
`ExchangeReport`, `TERMINAL_REPORTS`) — use them; the inline sketch below is illustrative only.

The exchange-side hypothetical order for one task (architecture section 8). Pure exchange logic,
no latency. Driven by the engine:

```python
@dataclass(frozen=True) class ExchangeCommand:
    command_id: str; task_id: str; kind: Literal["PASSIVE_LIMIT","AGGRESSIVE","CANCEL"]
    side: TaskSide; limit_price_fixed: int | None; quantity: int; arrival_time_ns: int
@dataclass(frozen=True) class ExchangeReport:
    report_id: str; command_id: str; task_id: str
    kind: Literal["ACCEPTED","REJECTED","FILL","CANCELLED","CANCEL_REJECTED_TERMINAL","AGGRESSIVE_UNFILLED"]
    exchange_time_ns: int; cumulative_executed: int; execution: Execution | None; reason: str
class ExchangeOverlay:
    def __init__(self, task_id: str, instrument: InstrumentDefinition, fee_per_contract_fixed: int) -> None
    def on_record(self, record: CanonicalRecord, book_before: ReferenceBook) -> list[ExchangeReport]
        # Called by the engine for EVERY live record BEFORE book.apply_record(record).
    def on_batch_committed(self, batch: EventBatch, book_after: ReferenceBook) -> list[ExchangeReport]
    def on_command(self, cmd: ExchangeCommand, book: ReferenceBook) -> list[ExchangeReport]
        # book is the committed historical state at arrival (after all batches with proxy <= arrival)
    @property def executed_quantity(self) -> int     # <= 1 always (assert)
    @property def is_working(self) -> bool
    def true_queue_ahead(self) -> int | None   # audit only; never exposed to policies
    def copy(self) -> ExchangeOverlay
```
Rules: passive limit marketable at arrival -> fill at opposite best (AGGRESSIVE liquidity,
mechanism AGGRESSIVE) if status TRADING and opposite best qty >= 1, else rest at its limit
behind all resting orders at that price (record the ahead set of OrderKeys). Invalid status or
non-tick-aligned limit -> REJECTED. Queue-ahead quantity is always the *current historical book
quantity* of ahead-set orders still at that price with retained priority (so book reductions
are counted exactly once by the book itself). Ahead-set orders that lose priority (MODIFY price
change / size increase) leave the ahead set. FILL of an order at our price that is NOT in the
ahead set (behind us) means the aggressor reached our priority -> virtual fill 1 at our limit,
QUEUE_DEPLETION. Opposing aggressive TRADE at a price strictly through our limit (buy: trade
price < limit with aggressor side ASK/SELL; sell: > limit, aggressor BID) -> virtual fill at our
own limit, TRADE_THROUGH. A behind-order FILL while ahead-set quantity is still positive is
inconsistent with the admitted FIFO model: the virtual order receives the fill only under the
frozen ambiguity rule `AMBIGUITY_FILL=False` (default: no fill) and an `AMBIGUOUS` evidence
entry is logged either way. Exact exhaustion of quantity ahead without further eligible volume is
no fill (T08). Touch/quote move/cancellation is never a fill (T09, T10). One execution group
(one TRADE and its FILLs) grants at most one unit. After the single fill the order is done.
CANCEL command on a working order -> CANCELLED (terminal); on a terminal order ->
CANCEL_REJECTED_TERMINAL with cumulative status. AGGRESSIVE: SYNTHETIC_DIRECT_TOP — fill 1 at
opposite best if TRADING and qty >= 1 else AGGRESSIVE_UNFILLED; never rests, never retries.
CLEAR/snapshot during a working order -> report reason TECHNICAL_RESET and mark the overlay
`technically_invalid` (T37). Fee per execution = `fee_per_contract_fixed`.
Required tests: T06-T10, T12, T17, T36, T37, T55, T58 + the architecture's mandatory fixtures.

### WP-FEATURES — `features/`, `labels/price.py` (tests: `tests/unit/features`, `tests/unit/labels`)

`DecisionView` and `ClientOrderState` are in `qexec.core.views`. One client `ReferenceBook` is
maintained per latency scenario (all tasks in a scenario see the same delayed stream), so
market-feature state is per scenario and queue state is per task world.

```python
# features/market.py
class MarketFeatureState:
    """Fed every DELIVERED batch in delivery order; computes causal market features."""
    def __init__(self, instrument: InstrumentDefinition, *, flow_window_ns: int = 1_000_000_000,
                 vol_window_ns: int = 5_000_000_000) -> None
    def on_delivered(self, batch: EventBatch, client_book_after: ReferenceBook, observation_time_ns: int) -> None
    def features(self, now_ns: int, client_book: ReferenceBook) -> dict[str, float]
    def copy(self) -> MarketFeatureState
# names (exact): spread_ticks, bid_qty_1, ask_qty_1, imbalance_1 = (b-a)/(b+a), depth_bid_3,
# depth_ask_3, imbalance_3, quote_age_ms, trade_flow_signed_1s (aggressor-signed TRADE qty in the
# window, +buy), trade_count_1s, cancel_qty_bid_1s, cancel_qty_ask_1s, add_qty_bid_1s, add_qty_ask_1s,
# mid_vol_5s (std of mid changes in ticks over window), mid_change_1s_ticks.
# Snapshot/INITIALIZATION batches never count as order flow.

# features/queue_proxy.py  (architecture 7.1 observable cohort proxy)
class QueueCohortProxy:
    def __init__(self, side: TaskSide, limit_price_fixed: int, client_book_at_decision: ReferenceBook,
                 decision_time_ns: int, expected_arrival_ns: int, last_delivered_proxy_ns: int) -> None
        # Freezes the same-price same-side order keys + quantities from the CLIENT book.
    def on_delivered(self, batch: EventBatch, client_book_after: ReferenceBook) -> None
        # Cohort qty = current client-book qty of cohort orders still at the limit with retained
        # priority (book reductions counted once by the book). Priority-losing MODIFY or price
        # move removes a member. ADDs at the limit whose batch exchange_proxy_time lies in
        # (last_delivered_proxy_ns, expected_arrival_ns] are UNCERTAIN (may be ahead); later adds
        # are behind. Uncertain adds are tracked by key and their current qty likewise.
    def features(self, client_book: ReferenceBook) -> dict[str, float]
    def copy(self) -> QueueCohortProxy
# names (exact): q_ahead_est (cohort qty), q_ahead_upper (cohort + uncertain qty),
# q_insertion_uncertain (uncertain qty), q_depleted (initial cohort qty - current), q_depleted_frac.

# features/groups.py
MARKET_FEATURES: tuple[str, ...]; MECHANICS_FEATURES = ("side", "limit_offset_ticks", "order_age_ms",
    "time_remaining_ms"); PRICE_SIGNAL_FEATURES = ("p_down", "p_unch", "p_up", "u_signal");
QUEUE_FEATURES = ("q_ahead_est","q_ahead_upper","q_insertion_uncertain","q_depleted","q_depleted_frac")
PROHIBITED_FEATURES = ("true_queue_ahead", "unreported_executed", "future_*", "m0")
def allowlist(policy_id: str) -> tuple[str, ...]   # B3: market+mechanics+price+queue;
    # B3_NO_QUEUE: same minus QUEUE_FEATURES; price model: MARKET_FEATURES only.
def assert_allowed(columns: Sequence[str], policy_id: str) -> None  # raises on prohibited/oracle (T25, T45)
def mechanics_features(view: DecisionView, tick_size_fixed: int) -> dict[str, float]

# labels/price.py
class MidSeries:  # committed HISTORICAL direct-book mids (evaluator side, not client)
    def __init__(self, times_ns: Sequence[int], mid2: Sequence[int | None]) -> None
    @classmethod def from_session(cls, session_dir: Path) -> MidSeries
    def mid_at(self, t_ns: int) -> int | None  # last committed valid mid at or before t
def price_direction_label(series: MidSeries, t_star_ns: int, tau_ns: int, tick_size_fixed: int) -> int | None
    # sign of m(t*+tau) - m(t*) in {-1,0,1}; None if either mid missing. Unchanged is class 0 (T43).
```
Required tests: T22 (future-only perturbation leaves earlier features unchanged), T25, T43, T45,
T54 (proxy uses only delivered information; differs from overlay truth when adds are uncertain),
hand-computed cohort example (5 ahead, 3 cancelled of a cohort member -> q_ahead_est 2; uncertain add).

### WP2-ENGINE — `sim/scheduler.py`, `sim/gateway.py`, `sim/oms.py`, `sim/controller.py`, `sim/engine.py`, `sim/tasks.py`, `policies/base.py`, `policies/baselines.py` (tests: `tests/unit/sim`, `tests/integration`)

Use `qexec.core.views.DecisionView`/`ClientOrderState` and `qexec.core.messages`.

Event-driven single-session replay with one historical stream fanned out to many independent
task worlds (policy x task x side). Key types:

```python
@dataclass(frozen=True) class DecisionView: ...   # client plane only (architecture 7.1)
    now_ns, task (without m0), side, client_book: BookSnapshot, client_quote_age_ns,
    own_limit_price_fixed, order_state, pending_command, reported_executed, time_remaining_ns,
    queue_features: Mapping[str, float], market_features: Mapping[str, float]
class Policy(Protocol):
    policy_id: str
    def on_arrival(self, view: DecisionView) -> Literal["JOIN_BEST","SWITCH_TO_TAKER","WAIT"]
    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]
def checkpoint_eligibility(view: DecisionView) -> DecisionReason  # shared; MODEL_CHOICE means eligible
class ClientOMS: exposure reservation invariant reported_executed + reserved <= 1
class TerminalController: absorbing, idempotent, one aggressive attempt (architecture 7.3)
class World: one (task, policy) counterfactual: overlay + client book + OMS + controller + timers
    def clone(self) -> World   # full-state checkpoint/restore for HOLD/SWITCH branches (T41)
class SessionEngine:
    def __init__(self, session_dir: Path, config: ExperimentConfig, policies: Sequence[Policy],
                 feature_builder: FeatureBuilder | None = None) -> None
    def run(self) -> SessionOutputs   # task manifest, decisions, executions, reports, task results
def build_task_manifest(session_dir, config) -> list[Task]  # seeded phase, arrival_spacing_ns, eligibility
```
Client book = a `ReferenceBook` fed **delivered** immutable batches at
`capture_complete_time + added_delivery` (T56 by construction). The client queue-cohort proxy
(architecture 7.1) is computed from the client book and delivered records only. Reports are
delivered at `exchange_time + response`. Commands: `send = decision + computation`,
`arrival = send + entry`. Exact timers fire with no market event (T38). Deadline outcome frozen
at T; drain afterwards for reconciliation only (T18). B0 and B1 implemented here.
Required tests: T11, T13-T16, T18, T38-T41, T48 (partially), T53, T56, plus G2 trace tests.

### WP-MODELS — `models/` (tests: `tests/unit/models`)

```python
def risk_allowance_choice(p_hat: Mapping[CheckpointChoice, float], v_hat: Mapping[CheckpointChoice, float],
    epsilon: float, supported: bool = True) -> tuple[CheckpointChoice, DecisionReason]
    # product 6.1: A_eps = {a: p_a <= min p + eps}; argmin v over A_eps; exact cost tie -> SWITCH;
    # nonfinite -> (SWITCH, FALLBACK_NONFINITE); not supported -> (SWITCH, FALLBACK_UNSUPPORTED)
def select_theta(validation: pl.DataFrame, epsilon: float, menu=THETA_MENU) -> ThetaSelection
    # rows: theta, session_id, miss, c_t; session-mean miss within eps of min, then min session-mean C_T,
    # ties -> smaller theta; uses risk_allowance logic generalized to a menu; logs all trials
class PriceModel  # regularized multinomial logistic (sklearn), classes down/unchanged/up
class ActionModels  # per action: binary miss model (constant fallback if one class), regularized mean cost
class FeatureSchema  # ordered names/types; hard error on mismatch (T52)
class ModelArtifact  # serialize/deserialize with metadata (architecture 10.3)
def forward_chained_oof_scores(...)  # T44
class SupportRule  # frozen numeric support thresholds
```
Required tests: T26, T44, T47, T52, T60.

### WP-RESEARCH — `features/`, `labels/`, `policies/model_policies.py`, `experiments/`, `analysis/support.py` (tests accordingly)

Feature builder with explicit allowlists per policy variant (T25, T45, T54), market/queue feature
groups exactly as product 6.2, leakage perturbation test (T22), price labels primary tau and
secondary 100 ms (T43), paired HOLD/SWITCH branch label generation via `World.clone()` (T41,
T42, T57), chronological session splits with purge (T23), B2/B3/B3_NO_QUEUE policies, the G2-S
support diagnostic and frozen selection rule (T59), and the experiment pipeline that trains on
train sessions, selects on validation, freezes a manifest, and evaluates the test sessions.

### WP7-RELEASE — `cli/`, `analysis/report.py`, `docs/`, `README.md` (tests: `tests/integration/test_cli*.py`)

`qexec demo --out DIR --sessions N [--quick]` (used by the CI smoke stage), `qexec synth`,
`qexec replay validate`, `qexec tasks build`, `qexec experiment run`, `qexec trace task`,
`qexec analysis report`. Run directory layout per architecture section 12. T33.

## 4. Synthetic-mode research contract (G0 decisions for software validation only)

These numbers stand in for the real G0 decisions so the pipeline is executable. They are not
market facts and must be re-decided on real data.

| Item | Synthetic value |
|---|---|
| Dataset id | `SYNTH.MBO`, publisher 1, instrument 1, symbol `SYNZ6` |
| Tick / multiplier | 0.25 (250_000_000 fixed) / 50 |
| Session window | whole synthetic session after `warmup_ns` |
| G2-S support minima | >= 30 decision-eligible checkpoints per session and >= 3 `QUEUE_DEPLETION` HOLD fills per session |
| Primary epsilon | 0.001 (absolute), sensitivity set from `core.config.EPSILON_SENSITIVITY` |
| Fee scenario | 0 (gross) primary; net = gross + fee scenario `fee_per_contract_fixed` |
| Splits | chronological by session index: 50% train / 20% validation / 30% test; at least 2 train for OOF, 1 validation, 1 test; pilots entirely within train |

## 5. Completion contract after independent review

Each normalized INITIALIZATION event is a complete replacement snapshot. All historical,
client, manifest, label and CLI replay paths call `ReferenceBook.begin_batch` before its records.
Fragmented vendor snapshots must be assembled by a future adapter before admission.
Execution allocations cannot bypass a better historical price; malformed source evidence and
snapshot crossings share technical exclusions across policies. Support counts use only
technically evaluable task windows.

`ExperimentConfig.planned_scenario_ids` declares the common study task population. Every study
stage and frozen trace uses the same declared scenarios. Standalone engine calls can use the
active scenario; direct manifest calls honor declared scenarios, or an explicit override.

Miss models retain missing-cost rows. Cost fits and their diagnostics use separate denominators.
Support ranges use the exact union training matrix; admission also requires the smallest
action-specific miss/cost fit population to meet the frozen row minimum. Model refits are atomic.
Price labels must be exactly DOWN/UNCHANGED/UP before conversion. Final price models are refit
on all eligible training rows after chronological tuning; artifact metadata names actual fits.

A run always requires a fresh output directory. Completion seals config, stages, frozen inputs,
results and the original unblinding-log prefix; later accesses append to that prefix. Reports and
traces reject absent or changed seals. Old runs are preserved and reproduced into fresh directories.
Run `scripts/ci.ps1 -Offline` to enforce cached dependency use after the first installation.
