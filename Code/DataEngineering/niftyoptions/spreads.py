"""Calendar-spread construction: one universe table, two enrichments.

Three builders share a single identity ("SpreadSeries") table so the daily and
minute engines trade exactly the same synthetic contracts:

  SpreadSeriesBuilder : asset prices -> identity table (NO leg prices)
      PreviousClose, CurrentDate, ExpirySeries, StrikeSeries, OptionType,
      Leg1_CID, Leg2_CID
  SettleSpreadBuilder : SpreadSeries + daily per-leg Open/High/Low/Close/Settle/
      Volume/OI (the old CalendarSpreadBuilder behaviour)
  MinuteSpreadBuilder : SpreadSeries + 1-minute per-leg OHLC/Volume/OI, with a
      CurrentDateTime column matching the raw Options_{YYYY} minute files

Definitions
-----------
ExpirySeries ActiveK : Leg1 = expiry rung K, Leg2 = rung K+1. Rungs = sorted
    unique expiries >= CurrentDate + ``min_dte`` days (weekly ladder; holiday
    shifts implicit). Both legs share the strike (calendar spread).
StrikeSeries y       : y = (ATM - Strike) / 100, ATM = floor(PreviousClose/100)
    * 100. Only 100-point strikes; kept where |y| <= ``strike_band_series``.
    y = +2 => strike = ATM - 200 (high-delta ITM call), etc.
PreviousClose        : previous trading day's Asset (NIFTY 50) close; own-day
    Asset as fallback when the prior day is missing.

A price <= 0 is the vendor's "no trade" marker (missing); left as-is here.
"""

from __future__ import annotations

import calendar
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config, DEFAULT_CONFIG

# Shared identity columns produced by SpreadSeriesBuilder (carried everywhere).
SERIES_COLS = ["PreviousClose", "CurrentDate", "ExpirySeries", "StrikeSeries",
               "OptionType", "Leg1_CID", "Leg2_CID"]


# ===========================================================================
# 1. SpreadSeries — the universe / identity table
# ===========================================================================

class SpreadSeriesBuilder:
    """Build the (date x ExpirySeries x StrikeSeries x OptionType) universe.

    Output rows carry only identities and the day's PreviousClose — no leg
    prices, volumes or OI. This is the master list every spread engine joins on.
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    # ------------------------------------------------------------------
    @staticmethod
    def parse_cids(options: pd.DataFrame) -> pd.DataFrame:
        """Add OptType (CAL/PUT) and Expiry parsed from the CID string."""
        df = options.copy()
        parts = df["CID"].str.split(".")
        df["OptType"] = parts.str[0]
        df["Expiry"] = pd.to_datetime(parts.str[3], format="%Y%m%d")
        return df

    def previous_close(self, options: pd.DataFrame) -> pd.Series:
        """CurrentDate -> previous trading day's Asset close.

        Asset is constant within a date; own-day Asset is the fallback for the
        first date (and any date whose prior session is absent).
        """
        asset = options.groupby("CurrentDate")["Asset"].first().sort_index()
        prev = asset.shift(1)
        return prev.fillna(asset)

    def expiry_ladder(self, day_df: pd.DataFrame, day: pd.Timestamp) -> list:
        cutoff = day + pd.Timedelta(days=self.cfg.min_dte)
        exps = day_df.loc[day_df["Expiry"] >= cutoff, "Expiry"].unique()
        return sorted(exps)[: self.cfg.max_series + 1]

    # ------------------------------------------------------------------
    def build(self, options: pd.DataFrame,
              strike_band: int | None = None) -> pd.DataFrame:
        band = self.cfg.strike_band_series if strike_band is None else strike_band
        df = self.parse_cids(options)
        prev_close = self.previous_close(options)

        out = []
        for day, g in df.groupby("CurrentDate"):
            pc = prev_close.loc[day]
            if not np.isfinite(pc) or pc <= 0:
                continue                      # cannot place the strike grid
            atm = np.floor(pc / 100.0) * 100.0
            ladder = self.expiry_ladder(g, day)

            for k in range(min(self.cfg.max_series, len(ladder) - 1)):
                e1, e2 = ladder[k], ladder[k + 1]
                leg1 = (g.loc[g["Expiry"] == e1, ["OptType", "Strike", "CID"]]
                        .rename(columns={"CID": "Leg1_CID"}))
                leg2 = (g.loc[g["Expiry"] == e2, ["OptType", "Strike", "CID"]]
                        .rename(columns={"CID": "Leg2_CID"}))
                m = leg1.merge(leg2, on=["OptType", "Strike"], how="inner")
                if m.empty:
                    continue

                m = m[m["Strike"] % 100 == 0].copy()
                if m.empty:
                    continue
                m["StrikeSeries"] = ((atm - m["Strike"]) / 100).round().astype(int)
                m = m[m["StrikeSeries"].abs() <= band]
                if m.empty:
                    continue

                m["CurrentDate"] = day
                m["PreviousClose"] = pc
                m["ExpirySeries"] = f"Active{k + 1}"
                m = m.rename(columns={"OptType": "OptionType"})
                out.append(m[SERIES_COLS])

        if not out:
            return pd.DataFrame(columns=SERIES_COLS)
        series = pd.concat(out, ignore_index=True)
        return series.sort_values(
            ["CurrentDate", "ExpirySeries", "OptionType", "StrikeSeries"]
        ).reset_index(drop=True)

    def run(self, options: pd.DataFrame | None = None) -> pd.DataFrame:
        self.cfg.ensure_dirs()
        if options is None:
            options = pd.read_parquet(self.cfg.options_table_path)
        series = self.build(options)
        series.to_parquet(self.cfg.spread_series_path, index=False)
        print(f"SpreadSeries: {len(series):,} rows / "
              f"{series['CurrentDate'].nunique()} days "
              f"-> {self.cfg.spread_series_path}")
        return series


# ===========================================================================
# 2. SettleSpread — daily per-leg prices
# ===========================================================================

class SettleSpreadBuilder:
    """Attach each leg's daily prices to the SpreadSeries universe.

    Per leg: Open, High, Low, Close, Settle, Volume, OI (from the Options
    table), alongside every SpreadSeries column. Optionally restrict to one
    synthetic contract (ExpirySeries / StrikeSeries / OptionType).
    """

    LEG_FIELDS = ["Open", "High", "Low", "Close", "Settle", "Volume", "OI"]

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    # ------------------------------------------------------------------
    def build(self, series: pd.DataFrame, options: pd.DataFrame,
              expiry_series: str | None = None,
              strike_series: int | None = None,
              option_type: str | None = None) -> pd.DataFrame:
        sub = series
        if expiry_series is not None:
            sub = sub[sub["ExpirySeries"] == expiry_series]
        if strike_series is not None:
            sub = sub[sub["StrikeSeries"] == strike_series]
        if option_type is not None:
            sub = sub[sub["OptionType"] == option_type]
        sub = sub.reset_index(drop=True)

        opts = options.copy()
        opts["CurrentDate"] = pd.to_datetime(opts["CurrentDate"])
        px = opts.set_index(["CurrentDate", "CID"])[self.LEG_FIELDS]
        sub["CurrentDate"] = pd.to_datetime(sub["CurrentDate"])

        for leg in ("Leg1", "Leg2"):
            j = px.rename(columns={f: f"{leg}_{f}" for f in self.LEG_FIELDS})
            sub = sub.merge(j, how="left",
                            left_on=["CurrentDate", f"{leg}_CID"],
                            right_index=True)

        leg_cols = [f"{leg}_{f}" for leg in ("Leg1", "Leg2")
                    for f in self.LEG_FIELDS]
        return sub[SERIES_COLS + leg_cols].reset_index(drop=True)

    def run(self, expiry_series: str = "Active2", strike_series: int = 2,
            option_type: str = "CAL", series: pd.DataFrame | None = None,
            options: pd.DataFrame | None = None) -> pd.DataFrame:
        self.cfg.ensure_dirs()
        if series is None:
            series = pd.read_parquet(self.cfg.spread_series_path)
        if options is None:
            options = pd.read_parquet(self.cfg.options_table_path)
        table = self.build(series, options, expiry_series, strike_series,
                            option_type)
        name = f"{option_type}_{expiry_series}_{strike_series}_SettleSpreads.parquet"
        path = self.cfg.settle_spread_dir / name
        table.to_parquet(path, index=False)
        print(f"SettleSpreads [{option_type}/{expiry_series}/{strike_series:+d}]: "
              f"{len(table):,} rows -> {path}")
        return table


# ===========================================================================
# 3. MinuteSpread — 1-minute per-leg OHLC/Volume/OI
# ===========================================================================
# Minute store layout (under cfg.raw_minute_dir):
#   Options_{YYYY}/{ExpiryYYYY-MM-DD}/NIFTY_{STRIKE}_{CE|PE}_{DD}_{MON}_{YY}.csv
# Each CSV holds one contract's whole life at 1-min bars
#   (timestamp, open, high, low, close, volume, oi; ISO-8601 +05:30).

MINUTE_LEG_FIELDS = ["Open", "High", "Low", "Close", "Volume", "OI"]
MINUTE_LEG_COLS = ["CurrentDate", "CurrentDateTime", "Leg1_CID", "Leg2_CID"] + [
    f"{leg}_{f}" for leg in ("Leg1", "Leg2") for f in MINUTE_LEG_FIELDS
]
# Final minute table column order: identity (+ CurrentDateTime) then leg bars.
MINUTE_COLS = (
    ["CurrentDate", "CurrentDateTime", "PreviousClose", "ExpirySeries",
     "StrikeSeries", "OptionType", "Leg1_CID", "Leg2_CID"]
    + [f"{leg}_{f}" for leg in ("Leg1", "Leg2") for f in MINUTE_LEG_FIELDS]
)

_CID_RE = re.compile(r"^(?P<ot>[A-Z]+)\.NIF50\.(?P<strike>\d+)\.(?P<expiry>\d{8})$")
_OPTTYPE_TO_SUFFIX = {"CAL": "CE", "PUT": "PE"}


def _parse_cid(cid: str) -> dict:
    m = _CID_RE.match(cid.strip())
    if not m:
        raise ValueError(f"Unrecognised CID: {cid!r}")
    return {"opt": _OPTTYPE_TO_SUFFIX[m.group("ot")],
            "strike": int(m.group("strike")),
            "expiry": pd.Timestamp(m.group("expiry"))}


def _cid_to_relpath(cid: str) -> str:
    """CAL.NIF50.22800.20250227 -> Options_2025/2025-02-27/NIFTY_22800_CE_27_FEB_25.csv"""
    p = _parse_cid(cid)
    e = p["expiry"]
    mon = calendar.month_abbr[e.month].upper()
    fname = f"NIFTY_{p['strike']}_{p['opt']}_{e.day:02d}_{mon}_{e.strftime('%y')}.csv"
    return os.path.join(f"Options_{e.year}", e.strftime("%Y-%m-%d"), fname)


class MinuteStore:
    """Lazy, per-process cache of one contract's minute bars from the CSV tree."""

    def __init__(self, root: str | Path):
        self.root = str(root)
        self._cache: dict[str, pd.DataFrame | None] = {}

    def contract_frame(self, cid: str) -> pd.DataFrame | None:
        if cid in self._cache:
            return self._cache[cid]
        path = os.path.join(self.root, _cid_to_relpath(cid))
        if not os.path.exists(path):
            self._cache[cid] = None
            return None
        df = pd.read_csv(path)
        if df.empty:
            self._cache[cid] = None
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
        df = df.sort_values("timestamp").reset_index(drop=True)
        self._cache[cid] = df
        return df

    def day_slice(self, cid: str, day: pd.Timestamp) -> pd.DataFrame | None:
        df = self.contract_frame(cid)
        if df is None:
            return None
        out = df.loc[df["timestamp"].dt.normalize() == day.normalize()]
        return out if len(out) else None


def _build_one(store: MinuteStore, current_date, leg1_cid: str, leg2_cid: str,
               how: str) -> pd.DataFrame:
    """Merge both legs' minute bars for one CurrentDate on the timestamp."""
    day = pd.Timestamp(current_date).normalize()
    l1 = store.day_slice(leg1_cid, day)
    l2 = store.day_slice(leg2_cid, day)
    if l1 is None or l2 is None:
        missing = [c for c, d in ((leg1_cid, l1), (leg2_cid, l2)) if d is None]
        raise FileNotFoundError(f"{day.date()}: no minute bars for {', '.join(missing)}")

    def _prep(d: pd.DataFrame, leg: str) -> pd.DataFrame:
        d = d.rename(columns={
            "timestamp": "CurrentDateTime",
            "open": f"{leg}_Open", "high": f"{leg}_High",
            "low": f"{leg}_Low", "close": f"{leg}_Close",
            "volume": f"{leg}_Volume", "oi": f"{leg}_OI"})
        keep = ["CurrentDateTime"] + [f"{leg}_{f}" for f in MINUTE_LEG_FIELDS]
        return d[keep]

    merged = _prep(l1, "Leg1").merge(_prep(l2, "Leg2"),
                                     on="CurrentDateTime", how=how)
    merged = merged.sort_values("CurrentDateTime").reset_index(drop=True)
    merged.insert(0, "CurrentDate", day)
    merged.insert(2, "Leg1_CID", leg1_cid)
    merged.insert(3, "Leg2_CID", leg2_cid)
    return merged[MINUTE_LEG_COLS]


def _process_pair(args):
    """Worker (child process): one contract pair -> all its requested days."""
    root, leg1_cid, leg2_cid, dates, how = args
    store = MinuteStore(root)
    frames, errors = [], []
    for d in dates:
        try:
            frames.append(_build_one(store, d, leg1_cid, leg2_cid, how))
        except FileNotFoundError as e:
            errors.append(str(e))
    out = pd.concat(frames, ignore_index=True) if frames else None
    return leg1_cid, leg2_cid, out, errors, len(dates)


class MinuteSpreadBuilder:
    """Attach 1-minute per-leg bars to one synthetic contract of SpreadSeries.

    Reads the SpreadSeries universe (filtered to a chosen ExpirySeries /
    StrikeSeries / OptionType), then for every (Leg1_CID, Leg2_CID) pair pulls
    both legs' minute bars from the CSV store and merges them on the minute.
    Parallelised across contract pairs (each pair persists ~a week, so a worker
    loads its two CSVs once).
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config
        self.minute_root = self.cfg.raw_minute_dir

    # ------------------------------------------------------------------
    def build(self, series: pd.DataFrame, expiry_series: str = "Active2",
              strike_series: int = 2, option_type: str = "CAL",
              start=None, end=None, workers: int | None = None,
              how: str = "inner", verbose: bool = True):
        """Return (minute_table, per-pair report) for one synthetic contract.

        how : 'inner' keeps minutes where BOTH legs printed; 'outer' keeps every
              minute either leg printed (missing leg NaN).
        """
        sub = series[(series["ExpirySeries"] == expiry_series)
                     & (series["StrikeSeries"] == strike_series)
                     & (series["OptionType"] == option_type)].copy()
        sub["CurrentDate"] = pd.to_datetime(sub["CurrentDate"])
        if start is not None:
            sub = sub[sub["CurrentDate"] >= pd.Timestamp(start)]
        if end is not None:
            sub = sub[sub["CurrentDate"] <= pd.Timestamp(end)]
        if not len(sub):
            return pd.DataFrame(columns=MINUTE_COLS), pd.DataFrame()

        tasks = [
            (str(self.minute_root), l1, l2,
             sorted(g["CurrentDate"].unique()), how)
            for (l1, l2), g in sub.groupby(["Leg1_CID", "Leg2_CID"], sort=False)
        ]
        workers = workers or min(len(tasks), os.cpu_count() or 4)
        if verbose:
            print(f"[minute] {len(sub)} rows "
                  f"({sub['CurrentDate'].min().date()} -> "
                  f"{sub['CurrentDate'].max().date()}) "
                  f"-> {len(tasks)} pairs on {workers} workers", file=sys.stderr)

        frames, rep = [], []
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_process_pair, t) for t in tasks]
            for done, fut in enumerate(as_completed(futures), 1):
                l1, l2, out, errors, n_days = fut.result()
                if out is not None:
                    frames.append(out)
                rep.append({"Leg1_CID": l1, "Leg2_CID": l2,
                            "days_requested": n_days,
                            "days_built": 0 if out is None else out["CurrentDate"].nunique(),
                            "minute_rows": 0 if out is None else len(out),
                            "errors": "; ".join(errors)})
                if verbose and (done % 25 == 0 or done == len(tasks)):
                    print(f"[minute] {done}/{len(tasks)} pairs "
                          f"({time.perf_counter() - t0:.1f}s)", file=sys.stderr)

        if not frames:
            return pd.DataFrame(columns=MINUTE_COLS), pd.DataFrame(rep)

        table = pd.concat(frames, ignore_index=True)
        # Carry the SpreadSeries identity columns onto every minute row.
        keys = sub[["CurrentDate", "Leg1_CID", "Leg2_CID", "PreviousClose",
                    "ExpirySeries", "StrikeSeries", "OptionType"]].drop_duplicates()
        table = table.merge(keys, on=["CurrentDate", "Leg1_CID", "Leg2_CID"],
                            how="left")
        table = (table[MINUTE_COLS]
                 .sort_values(["CurrentDate", "CurrentDateTime"])
                 .reset_index(drop=True))
        report = pd.DataFrame(rep)
        if verbose:
            print(f"[minute] done: {len(table):,} minute rows in "
                  f"{time.perf_counter() - t0:.1f}s", file=sys.stderr)
        return table, report

    def run(self, expiry_series: str = "Active2", strike_series: int = 2,
            option_type: str = "CAL", series: pd.DataFrame | None = None,
            start=None, end=None, workers: int | None = None,
            how: str = "inner") -> pd.DataFrame:
        self.cfg.ensure_dirs()
        if series is None:
            series = pd.read_parquet(self.cfg.spread_series_path)
        table, _ = self.build(series, expiry_series, strike_series, option_type,
                              start=start, end=end, workers=workers, how=how)
        name = f"{option_type}_{expiry_series}_{strike_series}_MinuteSpreads.parquet"
        path = self.cfg.minute_spread_dir / name
        table.to_parquet(path, index=False)
        print(f"MinuteSpreads [{option_type}/{expiry_series}/{strike_series:+d}]: "
              f"{len(table):,} minute rows -> {path}")
        return table
