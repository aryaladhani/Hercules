"""fno_config.py — date-aware resolver over FuturesAndOptionsConfig.json.

The JSON holds Indian F&O configuration histories (lot sizes, expiry days,
STT, ELM, margin rates) as [from, to] dated bands. This module resolves the
value in force on any given date, so backtests can charge historically
correct lot sizes and costs.

Usage:
    from fno_config import FnoConfig
    cfg = FnoConfig()                      # loads JSON sitting next to this file
    cfg.lot_size("2024-06-01")             # -> 25
    cfg.stt_rate_sell("2023-11-01")        # -> 0.000625
    cfg.expiry_isoweekday("2025-10-01")    # -> 2 (Tuesday)
    cfg.elm_rate_per_leg("2022-01-01")     # -> 0.02
    cfg.margin_rate("2022-06-01")          # -> 0.12
    cfg.expiry_day_additional_elm("2025-01-01")  # -> 0.02
All methods accept anything pd.Timestamp accepts; default index is 'nifty'.
"""

from __future__ import annotations
import json
import os

import pandas as pd

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "FuturesAndOptionsConfig.json")


class FnoConfig:
    def __init__(self, path: str = _DEFAULT_PATH, index: str = "nifty"):
        with open(path) as f:
            self._raw = json.load(f)
        self.index = index

    # ------------------------------------------------------------------ core
    def _band(self, records: list, date, value_key: str,
              from_key: str = "from", to_key: str = "to"):
        """Return value_key of the record whose [from, to] covers date."""
        d = pd.Timestamp(date)
        for rec in records:
            lo = pd.Timestamp(rec[from_key])
            hi = pd.Timestamp(rec[to_key]) if to_key in rec else pd.Timestamp("9999-12-31")
            if lo <= d <= hi:
                return rec[value_key]
        raise KeyError(f"No config band covers {d.date()} for {value_key}")

    def _idx(self) -> dict:
        return self._raw[self.index]

    def _margin(self) -> dict:
        return self._idx()["margin_model"]

    # ------------------------------------------------------------ public API
    def lot_size(self, date) -> int:
        return int(self._band(self._idx()["lot_size_history"], date, "lot_size"))

    def expiry_isoweekday(self, date) -> int:
        return int(self._band(self._idx()["expiry_day_history"], date, "isoweekday"))

    def expiry_day_name(self, date) -> str:
        return self._band(self._idx()["expiry_day_history"], date, "day")

    def stt_rate_sell(self, date) -> float:
        return float(self._band(self._margin()["stt_history"], date, "stt_rate_sell"))

    def elm_rate_per_leg(self, date) -> float:
        return float(self._band(self._margin()["elm_history"], date, "elm_rate_per_leg"))

    def expiry_day_additional_elm(self, date) -> float:
        return float(self._band(self._margin()["expiry_day_elm_history"],
                                date, "additional_rate"))

    def margin_rate(self, date) -> float:
        """Blended SPAN+ELM effective rate. Bands here only carry 'from', so
        resolve as: latest band whose 'from' <= date."""
        d = pd.Timestamp(date)
        best = None
        for rec in self._margin()["margin_rate_history"]:
            lo = pd.Timestamp(rec["from"])
            if lo <= d and (best is None or lo > pd.Timestamp(best["from"])):
                best = rec
        if best is None:
            # date precedes the first band (e.g. 2019 vs a 2020 start):
            # fall back to the earliest known rate as the best approximation.
            best = min(self._margin()["margin_rate_history"],
                       key=lambda r: pd.Timestamp(r["from"]))
        return float(best["rate"])

    # -------------------------------------------------- vectorised conveniences
    def lot_size_series(self, dates: pd.Series) -> pd.Series:
        """Vectorised lot size for a series of dates."""
        bands = [(pd.Timestamp(r["from"]), pd.Timestamp(r["to"]), int(r["lot_size"]))
                 for r in self._idx()["lot_size_history"]]
        d = pd.to_datetime(dates)
        out = pd.Series(pd.NA, index=d.index, dtype="Int64")
        for lo, hi, v in bands:
            out[(d >= lo) & (d <= hi)] = v
        return out

    def stt_rate_series(self, dates: pd.Series) -> pd.Series:
        bands = [(pd.Timestamp(r["from"]), pd.Timestamp(r["to"]),
                  float(r["stt_rate_sell"])) for r in self._margin()["stt_history"]]
        d = pd.to_datetime(dates)
        out = pd.Series(float("nan"), index=d.index, dtype=float)
        for lo, hi, v in bands:
            out[(d >= lo) & (d <= hi)] = v
        return out
