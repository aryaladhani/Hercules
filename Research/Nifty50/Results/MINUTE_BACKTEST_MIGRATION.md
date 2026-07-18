# Hercules — Minute-Level Backtesting Migration Brief

**Purpose.** This document is a complete, self-contained handoff for building the minute-level
backtesting system in the Hercules repo. It captures everything established during the daily-data
research phase — data structures, strategy definition, engine rules, cost model, code assets, and
validated findings — so a fresh Claude Code session can pick up the work without any prior context.

**Repo:** `~/Documents/Hercules` &nbsp;·&nbsp; **Package:** `niftyoptions/` &nbsp;·&nbsp; **Data:** `data/processed/`

---

## 1. Project context (one paragraph)

We trade Nifty 50 call calendar spreads: Leg1 = near weekly expiry, Leg2 = the following weekly
expiry, same strike. The finalised synthetic asset is **OptType CAL, Series Active2, StrikeSeries +2**
(strike = ATM − 200, where ATM = floor(PrevClose/100)×100; only 100-point strikes). A daily
mispricing signal ("Time Value Differential") ranks days and sets a target position; the strategy is
lot-neutral (equal integer lots both legs, short Leg1 / long Leg2 when long the spread). Daily-data
backtests show a spectacular but **suspect** edge (Sharpe ~5–6) that collapses under a 1-day lag —
it is a same-session open→close reversion harvested at the opening print with zero spread cost.
**The purpose of the minute build is to test whether the edge survives realistic intraday execution.**

## 2. Data inventory

| Path (repo-relative) | Contents |
|---|---|
| `data/processed/Spreads.parquet` | Full daily spread universe, 1,227,208 rows × 21 cols, 2019-01-01→2026-07-03 |
| `data/processed/CAL_Active2_2_Spreads.xlsx` / `.parquet` | Filtered synthetic asset: 1,848 rows, 21 original cols + `StrikeSeries` + `DollarPnL` |
| `data/processed/CAL_Active2_2_BaselineLong.csv` | Baseline-long daily run (audit columns included) |
| `data/processed/TimeValueDifferential.xlsx` | Daily signal backtest: Summary + lot-neutral ledger |
| `data/processed/OHLC_1m/Options_2025/` | **Minute store** (see §3) |
| `data/raw/nifty50_index_eod.csv` | Nifty 50 EOD index closes |
| `niftyoptions/` | Python package (config, downloaders, tagging, spreads, minute_spreads) |
| `FuturesAndOptionsConfig.json` | Indian F&O config history (lot sizes, STT, ELM, margins) — §7 |

Key daily columns: `CurrentDate, Series, OptType, Strike, Asset, Leg{1,2}_CID, Leg{1,2}_Open/Close/
Settle/Volume/OI, Leg{1,2}_Expiry, SpreadOpen, SpreadClose`.
**Data convention: a price ≤ 0 is the vendor's "no trade" marker → treat as missing** (`SpreadOpen = -1` marks the same).

`CID` format: `CAL.NIF50.{Strike}.{ExpiryYYYYMMDD}` — constant throughout a day.

## 3. Minute store layout

```
data/processed/OHLC_1m/Options_{YYYY}/{ExpiryYYYY-MM-DD}/NIFTY_{STRIKE}_{CE|PE}_{DD}_{MON}_{YY}.csv
```
- Folders are **expiry dates** (weekly; Thursday through 2025-08-28, Tuesday from 2025-09-02).
- One CSV per contract holds its **entire life** of 1-minute bars.
- Columns: `timestamp` (ISO-8601, +05:30), `open, high, low, close, volume, oi`.
- CID → file mapping: `CAL.NIF50.22800.20250227` → `Options_2025/2025-02-27/NIFTY_22800_CE_27_FEB_25.csv`.
- Coverage currently 2025 only. Session hours ≈ 09:15–15:29 IST; the spread's joined window often
  starts minutes late (far leg prints late) — this is signal, not noise: it is the illiquidity we must model.

**Existing loader:** `niftyoptions/minute_spreads.py` — `MinuteSpreadBuilder`
(`build(start, end, workers, how)`): date-range filtered, multiprocessing (chunked by unique
(Leg1_CID, Leg2_CID) pair — a pair persists ~a week, so each worker loads its 2 CSVs once),
returns the unified 16-column table
`CurrentDate, CurrentDateTime, Leg{1,2}_CID, Leg{1,2}_{Open,High,Low,Close,Volume,OI}`
plus a per-pair audit report. `how='inner'` (both legs printed that minute) or `'outer'`.

## 4. The strategy (parameters of record)

**Signal (computed at day start, no lookahead):**
```
Signal = PrevClose × (e^(r·T2) − e^(r·T1)) − (Leg2_Open − Leg1_Open)
```
- `PrevClose` = previous trading day's Asset close (Nifty 50).
- `r = 6.5%` continuously compounded, constant. `T = calendar days to expiry / 365` (Actual/365).
- Signal > 0 ⇒ Leg2 underpriced ⇒ **long the spread** (sell Leg1, buy Leg2). Invalid open (≤0) ⇒ NaN ⇒ no trade.
- Structural note: the fair value is intrinsic+carry only (no time value), so Signal > 0 on only ~15% of days.

**Transforms (strict order, all causal):**
```
Winsorize (expanding P30/P70) → Rank (rolling 252, avg ties, today included) → Normalise
TP = (Rank/RankCount − 0.5) × 2      ∈ [−1, 1], long bias (no sign flip)
```
Rank-lookback sweep result: 21 > 42 > 63 > 126 > 252 on Sharpe (6.28 → 6.07), TPs 0.93–0.99
correlated across windows — the ensemble adds nothing. 252 remains the default.

**Position (lot-neutral, current structure):**
```
N = round(TP × 10)  integer lots per leg  (|TP| < 0.05 ⇒ flat)
Long spread: short N Leg1, long N Leg2.  Units per leg = N × lot_size(inception date)
OpeningDollarBalance = Leg1_Lots·Leg1_Open + Leg2_Lots·Leg2_Open = N·(Leg2_Open − Leg1_Open)
DailyPnL(gross) = ClosingDollarBalance − OpeningDollarBalance     [verified identity, fresh positions]
```

**Daily engine rules (must survive migration):**
- Sort by CurrentDate; never use future data for date *t* (transforms are expanding/rolling ending at *t*).
- Same CIDs as yesterday ⇒ may carry; rebalance only if |TP_today − HeldTP| > 0.10 (compare to *held* TP).
- Either CID changed (roll) ⇒ never carry; build from flat.
- Next day's CIDs differ (knowable: contract calendars are deterministic) ⇒ close everything at Close.
- Closing trades reverse the exact held lots — never create new exposure.
- Price fallbacks: open trades Open→Settle→Close; close trades Close→Settle; price ≤ 0 = missing;
  no usable price ⇒ skip the rebalance (carry) and log.
- P&L is mark-to-market: overnight (carried lots × (Open_t − Close_{t−1})) + intraday (post-open lots × (Close − Open)).
- A book flat all day = exactly 0 P&L (never NaN).
- Position-level constants are struck at **inception** and held for the position's life (lot size; the
  old dollar-neutral multiplier likewise). A position opened from a *flat carry* also strikes at that inception.

**Standing rules (baked into the daily-backtesting skill's `backtest.py` CLI):**
- Test window ends **2024-12-31** (`--end-date`, inclusive). Note: minute data is 2025 — see §9.
- Exclude **2021-11-22** (`--exclude-dates`): thin far-leg open (80.05 vs 219.45 close) blew the old
  dollar-neutral multiplier to 4.62 ≈ 44% of that run's total P&L. Lot-neutral kills this artifact class,
  but the exclusion stands.

## 5. Cost model (from `FuturesAndOptionsConfig.json`, resolver in `fno_config.py`)

Never hardcode a lot size or STT rate — the backtest window spans multiple regimes:

| Item | History |
|---|---|
| Nifty lot size | 75 (→2021-10-06) → 50 (→2024-04-25) → 25 (→2024-11-19) → 75 (→2025-12-31) → 65 (2026+) |
| STT (sell side, on premium) | 0.05% → 0.0625% (Oct-2023) → 0.10% (Oct-2024) → 0.15% (Apr-2026) |
| Expiry day | Thursday ≤ 2025-08-28; Tuesday ≥ 2025-09-02 |
| ELM | 2% of notional per short index option leg; +2% expiry-day add-on from 2024-11-20 |
| Margin (blended SPAN+ELM) | 6.5% (2020, also used for 2019 fallback) → 9% → 10% → 12% (2022) → 13% (Nov-2024) → 13.2% (2026) |
| Brokerage assumption | ₹20 flat per order (each leg's open + close = separate orders) |

Resolver API (`fno_config.FnoConfig`, sits next to the skill's `backtest.py`; copy into repo):
`lot_size(d)`, `stt_rate_sell(d)`, `expiry_isoweekday(d)`, `elm_rate_per_leg(d)`,
`expiry_day_additional_elm(d)`, `margin_rate(d)`, plus vectorised `lot_size_series` / `stt_rate_series`.
STT is charged on **every sell execution**: `|sold lots| × lot_size × price × rate`.

## 6. Validated findings from the daily phase (why minute-level matters)

| Run | Result | Verdict |
|---|---|---|
| Baseline long (TP=1 daily) | Sharpe 1.36 after outlier exclusion | sanity benchmark |
| Signal, dollar-neutral | Sharpe 6.12, maxDD −68 pts | too good — suspect |
| Signal, **lag-1** | Sharpe **−1.22** (inverts!) | edge has ZERO forward persistence |
| Signal, lot-neutral | Sharpe 6.07 (points×lots) | sizing method is not the source of edge |
| Volume gate (both legs > mean 252d) | 2.9% of days pass | rejected — volume too right-skewed |
| Volume gate (both legs > P30 252d) | Sharpe 4.33, deeper DD | hurt performance — not adopted |
| Rupee terms + STT + ₹20/order | Net ₹86.9L, Sharpe 5.33; costs only 2.1% of gross | statutory costs are NOT the threat |

**Interpretation:** the signal correlates (~0.2) with Leg2's own open→close move; 55% of daily P&L
came from days with Leg2 volume < 100 lots. The measured edge is an intraday reversion off (possibly
stale/unhittable) opening prints. Statutory costs don't kill it; **bid-ask spread and fill reality might.**
That is the question the minute engine must answer.

## 7. Minute-level backtest — build specification

### 7.1 Execution model (replaces "fill at the daily Open")
1. **Signal timing:** compute Signal at 09:15 from PrevClose and the legs' first *tradable* prints —
   or better, parameterise: signal from first joint minute vs. daily-file Open. Compare both.
2. **Entry window:** enter over the first N minutes after both legs have printed (default N=15).
   Fill price options (parameterise): minute close of entry bar, VWAP of window, worst-of-bar.
3. **Liquidity guard:** only execute in minutes where the leg's volume > 0; a leg with no prints in
   the window ⇒ no trade that day (log it — these were the profit days in the daily backtest; watch
   how much edge disappears).
4. **Exit window:** mirror logic near the close (default: last M minutes, M=15, VWAP or last-bar close).
5. **Spread/slippage penalty:** per-leg haircut in points, parameterised; default sweep 0 / 0.5 / 1 / 2
   points per leg per side. (No quoted bid-ask in the store; the haircut is the proxy. If quote data
   ever arrives, replace with half-spread.)
6. **Costs:** rupee engine from §5 — historical lot size (struck at inception), STT on every sell
   execution at the historical rate, ₹20/order brokerage.
7. **Intraday MTM (optional v2):** minute-level equity curve within the day; enables intraday
   stops/targets later. V1 needs only entry/exit fills + daily aggregation.

### 7.2 Deliverables
- `niftyoptions/minute_backtest.py` — engine class following package conventions (Config-driven
  paths, dataclass-style params, multiprocessing over contract pairs like `minute_spreads.py`).
- Per-day ledger: signal, TP, N, entry/exit fills per leg (with the minute used), slippage charged,
  STT, brokerage, gross/net rupee P&L, plus flags (no-print day, partial window, carried).
- Equity curve + summary stats (Sharpe, maxDD, hit rate) with the daily-engine result as reference.
- **The decisive table:** Sharpe as a function of the slippage haircut (0/0.5/1/2 pts) and entry
  window (1/5/15/30 min). If Sharpe survives ≥1pt haircut with a realistic window, the edge is real.

### 7.3 Invariants (carry over from daily engine — do not break)
1. No lookahead anywhere (signal at day start uses only ≤ open info; transforms causal).
2. Rolls (CID change) always build from flat; closes reverse exact held quantity.
3. Rebalance threshold 0.10 vs held TP.
4. Price ≤ 0 or zero-volume minute = not a fillable price.
5. Flat book ⇒ exactly 0 P&L.
6. Inception-locked position constants (lot size).
7. Standing exclusions: 2021-11-22; window rule per §4 (but see §9 for the 2025 clash).
8. Never silently assume — parameterise and surface every execution choice.

## 8. Code assets to migrate

| File (current location) | Role | Action |
|---|---|---|
| `niftyoptions/minute_spreads.py` | minute store loader + parallel merge | already in repo — reuse |
| `~/Documents/Hercules/FuturesAndOptionsConfig.json` | config JSON | move/copy into repo root or `niftyoptions/data` |
| skill `scripts/fno_config.py` | date-aware config resolver | copy into `niftyoptions/` (adjust default path) |
| skill `scripts/backtest.py` | daily engine + transforms (winsorize/rank/normalise) | import transforms or port them into `niftyoptions/` |
| skill `scripts/pqreader.py` | pure-Python parquet fallback reader | copy if pyarrow unavailable |
| `OptionsStrategy_TimeValueDifferential.md` (repo root) | strategy documentation | keep; this brief supersedes for minute work |

Daily reference results to validate against (regenerate any time from `CAL_Active2_2_Spreads.xlsx`):
lot-neutral gross 156,937 pts×lots / Sharpe 6.07; rupee net ₹8,689,339 / Sharpe 5.33
(window ≤ 2024-12-31, excl 2021-11-22, rank 252, threshold 0.10, ₹20/order).

## 9. Open questions (decide with the user before coding)

1. **Window clash:** standing rule caps testing at 2024-12-31, but the minute store covers 2025 only.
   Options: (a) lift the end-date for the minute phase and treat 2025 as the out-of-sample execution
   study (recommended — it is genuinely OOS for a signal tuned on ≤2024); (b) acquire pre-2025 minute data.
2. Entry/exit fill convention (bar close vs VWAP vs worst-of-bar) — default proposal: VWAP of window.
3. Slippage haircut grid — default proposal: 0 / 0.5 / 1 / 2 points per leg per side.
4. Expiry-day nuance: from Sep-2025 expiry is Tuesday; Active2 legs never expire same-day in this
   strategy, so expiry-day ELM shouldn't bind — confirm once real 2025 rolls are inspected.
5. 2026 lot change (75→65) only matters if the study extends into 2026 data.

---
*Generated 2026-07-13 from the Cowork research session. Daily-phase artifacts and the full narrative
live in the project doc `TimeValueDifferential_Strategy.md` and `data/processed/TimeValueDifferential.xlsx`.*
