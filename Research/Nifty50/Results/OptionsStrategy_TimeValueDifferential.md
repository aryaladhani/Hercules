# Nifty 50 Calendar-Spread Strategy — Time Value Differential

**Instrument:** Nifty 50 index options (CAL) calendar spreads
**Target series:** `OptType = CAL`, `Series = Active2`, `StrikeSeries = +2`
**Test window:** 2019-01-01 → 2024-12-31 (standing rule)
**Excluded date:** 2021-11-22 (data-quality outlier — see §6)
**Source data:** `Hercules/data/processed/Spreads.parquet` (1,227,208 rows × 21 cols)

---

## 1. Synthetic contract construction (StrikeSeries)

The tradable universe is built from a two-leg calendar spread on the same strike:

- **Leg1** = near expiry, **Leg2** = far expiry (one weekly step apart; the 2nd-nearest expiry pair from `CurrentDate`).
- **CID format:** `{OptType}.NIF50.{Strike}.{ExpiryYYYYMMDD}` (e.g. `CAL.NIF50.23900.20260703`).

**StrikeSeries** categorises each strike relative to the at-the-money level defined by the *previous* trading day's close:

```
ATM  = floor(PrevClose / 100) * 100
StrikeSeries (y) = (ATM - Strike) / 100        # stored as a signed integer
```

- `PrevClose` = previous trading day's `Asset` close (if a day is missing, its own Asset is used as the fallback).
- Positive `y` → strike is below ATM → **ITM for calls**; negative → OTM; `0` → ATM.
- **Filter:** only 100-point strikes (`Strike % 100 == 0`) are kept, so each StrikeSeries maps to exactly one strike (50-point strikes would collide two strikes onto one label).

The finalised **synthetic asset** is `StrikeSeries = +2` — i.e. a strike two 100-point steps in-the-money (ATM − 200), which behaves like a high-delta ITM call calendar spread.

Filtering the full `Spreads.parquet` to `CAL / Active2 / StrikeSeries +2 / Strike%100==0` yields **1,848 daily rows** (2019-01-01 → 2026-07-03); capped at 2024-12-31 and excluding 2021-11-22 it is **1,477 rows**.

Stored copies:
- `CAL_Active2_2_Spreads.parquet` / `.xlsx` — the filtered dataset with all 21 original columns plus `StrikeSeries` and `DollarPnL`.

---

## 2. Core spread metrics

For each day:

```
SpreadOpen  (SO) = (Leg2_Open  - Leg1_Open)  / Leg1_Open      # ratio; NaN when an open is 0
SpreadClose (SC) = (Leg2_Close - Leg1_Close) / Leg1_Close     # ratio
```

Data note: the vendor records a price of **0** (and `SpreadOpen = -1`) to mean "no trade" on illiquid days. Any price ≤ 0 is treated as **missing**, not real.

---

## 3. Daily P&L — self-financing (dollar-neutral) form

The original construction is a **self-financing** calendar spread: sell one lot of Leg1 at the open and buy the dollar-equivalent amount of Leg2, unwind both at the close.

```
$P&L = Leg1_Open × (Leg2_Close / Leg2_Open) − Leg1_Close        # per unit (mult = 1)
```

This is stored as the `DollarPnL` column. In pure spread terms it simplifies to:

```
$P&L = Leg1_Close × [ (SC − SO) / (1 + SO) ]
```

so the profit is the close price of the near leg times the normalised change in the spread ratio over the day.

---

## 4. Baseline long (sanity benchmark)

A no-signal benchmark: hold **TP = 1.0 every day** (always long the spread = sell Leg1, buy Leg2), with a **dynamic multiplier**:

```
mult = Leg1_Open / Leg2_Open        # struck at each position's inception, held for its life
```

The multiplier is a property of the position (fixed when opened, never re-struck mid-life) so a carried position never loses P&L to a later no-trade day. Days where a leg opens at 0 are flat (no trade).

**Result (2019 → 2024-12-31, excl 2021-11-22):** Total P&L 5,056.8 · Sharpe 1.36 · Max DD −734 · Hit 56%.
(With 2021-11-22 included it was 9,010.6 / Sharpe 0.80 — see §6.)

Stored: `CAL_Active2_2_BaselineLong.csv` (all original columns + LotMultiplier, TargetPosition, Leg1/Leg2 lots, per-leg P&L, DailyPnL, CumulativePnL, Drawdown).

---

## 5. Standing rules (baked into the `daily-backtesting` skill)

Applied automatically after sorting, before transforms; overridable per run:

| Rule | Default | CLI flag |
|---|---|---|
| Test window end (inclusive) | 2024-12-31 | `--end-date` |
| Excluded outlier dates | 2021-11-22 | `--exclude-dates` |

---

## 6. The 2021-11-22 outlier

On 2021-11-22 the Nifty fell ~348 points (~2%) after the Nov-19 holiday + weekend — a genuine broad sell-off (global risk-off, FII selling, weak Paytm IPO sentiment; Omicron fears deepened it days later). But the equity *blip* was mostly a **data artifact**: Leg2 opened at 80.05 vs a 219.45 close (a thin/stale far-leg print), which pushed `mult = Leg1_Open/Leg2_Open` to **4.62** and turned a normal ~855 gain into ~3,954. That single day was **~44% of the baseline's total P&L** and tripled daily volatility. Removing it dropped total P&L 9,010 → 5,057 but *raised* Sharpe 0.80 → 1.36. Hence the permanent exclusion.

---

## 7. The signal — Time Value Differential

**Hypothesis.** The "value" of an ITM option is `Asset · e^(rT) − Strike`. For the same strike, the difference between two expiries' ITM values should be `Asset · (e^(r·T2) − e^(r·T1))`. If the actual open-price gap `Leg2_Open − Leg1_Open` is *smaller* than this, Leg2 is underpriced (and Leg1 overpriced) → go **long** the spread.

```
Signal = PrevClose · (e^(r·T2) − e^(r·T1)) − (Leg2_Open − Leg1_Open)
```

- Computed **at the beginning of the day** using the previous close and today's leg opens.
- `r = 6.5%` continuously compounded (constant); `T = calendar days to expiry / 365` (Actual/365).
- `Signal > 0` → Leg2 underpriced → long (positive sign).
- Days with an invalid open (≤ 0) → `Signal = NaN` → no trade.

**Structural observation.** The model captures only intrinsic + carry, *no time value*. So `FairDiff` averages ~23.5 pts while the actual `Leg2_Open − Leg1_Open` averages ~55.6 pts (Leg2 carries more time value). The signal is therefore positive only ~14.6% of valid days — it flags the minority of days where the real gap is unusually small or inverted.

---

## 8. Signal → position (transform pipeline)

Standard `daily-backtesting` pipeline, **long bias**:

```
Winsorize (expanding P30/P70) → Rank (rolling 252, avg ties, today included) → Normalise
TP = (Rank / RankCount − 0.5) × 2          # TP ∈ [−1, 1]
```

All transforms are causal (expanding/rolling windows ending today) — no lookahead.

---

## 9. Backtest — dollar-neutral sizing

Signal → TP, long bias, dynamic inception-locked `mult = Leg1_Open/Leg2_Open`.

**Result:** Total 11,801.7 · Sharpe **6.12** · Max DD −68 · Hit 71.4% · 948 active days.

### Robustness checks (why a Sharpe of 6 is not trusted)

| Check | Total | Sharpe | Read |
|---|---|---|---|
| Base (signal at open) | 11,802 | 6.12 | spectacular |
| **Lag-1** (yesterday's signal) | −4,025 | **−1.22** | collapses & inverts |
| Leg2 volume ≥ 100 | 5,364 | 7.07 | edge persists on liquid days |
| Leg2 volume ≥ 1000 | 2,952 | 7.08 | still there when liquid |

- **Lag-1 collapse** is the loudest alarm: the signal has *zero* forward persistence and inverts — it is a pure same-session open→close reversion, not a positional edge.
- Fills are modelled at the exact open with **zero cost / zero bid-ask**; the edge is ~8 pts/day, easily inside a real spread.
- 55% of P&L comes from days with `Leg2_Volume < 100` (thin far leg), though even liquid-only days show a high Sharpe.
- Multiplier is **not** the culprit here (post-exclusion max mult 1.71; capping it changes nothing; top-20 days = only 20% of P&L).

Stored: `TimeValueDifferential.xlsx`.

---

## 10. Lot-neutral sizing (current structure)

Instead of equal dollar exposure, trade the **same integer number of lots on both legs**. This removes the `Leg1_Open/Leg2_Open` multiplier entirely (and its artifact risk).

```
N = round(TP × 10)                 # integer lots per leg; |TP| < 0.05 → N = 0 (flat)
Position: short N of Leg1, long N of Leg2   (for TP > 0)
Per-lot $P&L = (Leg2_Close − Leg2_Open) − (Leg1_Close − Leg1_Open)
```

**Opening dollar balance** (verified identity):

```
OpeningDollarBalance = Leg1_Lots·Leg1_Open + Leg2_Lots·Leg2_Open = N·(Leg2_Open − Leg1_Open)
DailyPnL             = ClosingDollarBalance − OpeningDollarBalance     (on fresh positions)
```

**Result:** Total 156,937 (points × lots) · Sharpe **6.07** · Max DD −658 · Hit 78% · 953 active days.

Key takeaway: switching sizing dollar-neutral → lot-neutral barely moved the Sharpe (6.12 → 6.07). The edge (and its fragility) lives in the signal + fill-at-open, **not** the sizing method. P&L units are option **points × lots** (multiply by lot size for rupees).

Stored: `TimeValueDifferential.xlsx` → sheet `Backtest_LotNeutral` (includes `OpeningDollarBalance`, `ClosingDollarBalance`).

---

## 11. Gating logic — volume hurdle

A causal liquidity hurdle: the trade decision for day *t* uses only data through *t−1*. Maintain a trailing **252-day** window of each leg's volume; trade only if **both legs** clear the bar yesterday.

### 11a. Bar = trailing average (rejected — too tight)

`vol[t−1] > mean(252-day window ending t−1)`, both legs.

- Volume is heavily right-skewed (Leg1 median 871 vs mean 6,089; Leg2 median 49 vs mean 3,439) → a leg exceeds its own mean only ~20–27% of days.
- The two legs' above-average days are slightly *negatively* correlated (−0.14) — near leg peaks near its expiry, far leg builds later.
- **Both clear only 2.9% of days** → 37 active trades, Sharpe 1.11. Too few to trust; rejected.

### 11b. Bar = 30th percentile (current)

`vol[t−1] > 30th percentile(252-day window ending t−1)`, both legs. A much lower hurdle (top ~70% of recent volume).

- Per-leg pass: Leg1 75%, Leg2 61%, **both ~49.9%** → 634 active days.
- **Result:** Total 92,556 · Sharpe **4.33** · Max DD **−862** · Hit 75.6%.

**Finding:** the volume hurdle *hurt* — Sharpe fell (6.07 → 4.33) and max drawdown deepened (−658 → −862) while keeping ~59% of P&L. The filtered lower-volume days were on net contributing and diversifying, so removing them concentrated risk. Interpreted as: the measured edge is **not** coming from higher-liquidity days.

(Scope note: applied with the *both-legs* scope; the percentile is a lever — lower bars trade more/closer to no-gate, higher bars prune harder.)

---

## 12. Open questions / next steps

1. **Execution realism (the decisive test).** Everything above fills at the exact open with zero cost. Add a per-leg bid-ask / slippage haircut (now simply points-per-leg × N lots on the lot-neutral structure) and watch how fast the Sharpe decays. The ~8-pt/day edge may not survive a realistic spread.
2. **Minute-level data.** Move from a single open print to a realistic fill window (first N minutes / VWAP), model quoted bid-ask, and add exchange costs (STT, brokerage). The lag-1 collapse means this is fundamentally an *intraday* strategy — execution is everything.
3. **Percentile sweep** on the volume gate to confirm no threshold materially improves risk-adjusted return.

---

## Appendix — file inventory (`Hercules/data/processed/`)

| File | Contents |
|---|---|
| `Spreads.parquet` | Source: full 1.2M-row spread universe |
| `CAL_Active2_2_Spreads.parquet` / `.xlsx` | Filtered synthetic asset (1,848 rows, +StrikeSeries, +DollarPnL) |
| `CAL_Active2_2_BaselineLong.csv` | Baseline-long run (§4), all inputs + compute columns |
| `TimeValueDifferential.xlsx` | Signal backtest (§9–10): Summary + lot-neutral ledger with OpeningDollarBalance |

**Parameters of record:** r = 6.5% (cont. comp.), Actual/365, winsorize P30/P70, rank window 252, normalise → TP∈[−1,1], long bias, lot-neutral `N = round(TP×10)`, test window ≤ 2024-12-31, exclude 2021-11-22.
