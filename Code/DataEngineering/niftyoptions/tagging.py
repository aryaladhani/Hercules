"""Contract tagging and the Options table.

ContractTagger turns the raw bhavcopy parquet into the canonical Options
table used by everything downstream.

Identifiers
-----------
SE tag : NIF50.<strike>.<yyyymmdd>              (strike-expiry)
CID    : CAL.<se_tag> / PUT.<se_tag>            (contract ID)

Options table schema (one row per CurrentDate x CID)
----------------------------------------------------
CurrentDate | CID | Strike | Open | High | Low | Close | Settle
| Volume | Asset | OI | OIDelta
"""

from __future__ import annotations

import pandas as pd

from .config import Config, DEFAULT_CONFIG

RENAME = {
    "cid": "CID", "strike": "Strike",
    "open": "Open", "high": "High", "low": "Low", "close": "Close",
    "settle_price": "Settle", "volume_contracts": "Volume",
    "underlying_close": "Asset",
    "open_interest": "OI", "change_in_oi": "OIDelta",
}

OPTIONS_COLS = ["Strike", "Open", "High", "Low", "Close", "Settle",
                "Volume", "Asset", "OI", "OIDelta"]


class ContractTagger:
    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    # ------------------------------------------------------------------
    def backfill_asset(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fill NaN underlying_close (old bhavcopy format, pre 2024-07-08)
        from the index EOD file if present. New-format rows are untouched."""
        if not df["underlying_close"].isna().any():
            return df
        path = self.cfg.index_eod_path
        if not path.exists():
            n = df["underlying_close"].isna().sum()
            print(f"WARNING: {n:,} rows have NaN underlying_close and "
                  f"{path} not found - Asset will stay NaN for those dates. "
                  f"Run the index downloader first.")
            return df

        spot = pd.read_csv(path, parse_dates=["date"])
        spot = spot.rename(columns={"close": "_spot"})[["date", "_spot"]]
        df = df.merge(spot, on="date", how="left")
        df["underlying_close"] = df["underlying_close"].fillna(df["_spot"])
        df = df.drop(columns="_spot")

        still = df["underlying_close"].isna().sum()
        if still:
            print(f"WARNING: {still:,} rows still NaN "
                  f"(dates missing from {path.name}).")
        return df

    # ------------------------------------------------------------------
    def add_tags(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df["expiry"] = pd.to_datetime(df["expiry"])

        # int cast so tags don't come out as NIF50.21350.0.20260707
        strike_str = df["strike"].astype(int).astype(str)
        expiry_str = df["expiry"].dt.strftime("%Y%m%d")
        df["se_tag"] = f"{self.cfg.tag_root}." + strike_str + "." + expiry_str

        prefix = df["option_type"].map({"CE": "CAL", "PE": "PUT"})
        df["cid"] = prefix + "." + df["se_tag"]

        # integrity checks: fail loudly, never silently
        dupes = df.duplicated(subset=["date", "cid"]).sum()
        if dupes:
            raise ValueError(f"{dupes} duplicate (date, cid) rows in Raw.")
        if df["cid"].isna().any():
            raise ValueError("Rows with option_type outside CE/PE found.")
        return df

    # ------------------------------------------------------------------
    @staticmethod
    def build_options_table(tagged: pd.DataFrame) -> pd.DataFrame:
        t = tagged.rename(columns=RENAME).rename(columns={"date": "CurrentDate"})
        t = t[["CurrentDate", "CID"] + OPTIONS_COLS]
        return t.sort_values(["CurrentDate", "CID"]).reset_index(drop=True)

    # ------------------------------------------------------------------
    def run(self, start=None, end=None) -> pd.DataFrame:
        """Full step: read raw parquet -> tagged parquet + Options.parquet."""
        self.cfg.ensure_dirs()
        df = pd.read_parquet(self.cfg.raw_options_path)
        df["date"] = pd.to_datetime(df["date"])
        if start:
            df = df[df["date"] >= pd.to_datetime(start)]
        if end:
            df = df[df["date"] <= pd.to_datetime(end)]
        if df.empty:
            raise SystemExit(f"No rows in range {start} .. {end}.")

        tagged = self.add_tags(self.backfill_asset(df))
        tagged.to_parquet(self.cfg.tagged_path, index=False)

        options = self.build_options_table(tagged)
        options.to_parquet(self.cfg.options_table_path, index=False)

        n_days = options["CurrentDate"].nunique()
        print(f"Options table: {len(options):,} rows / {n_days} days "
              f"-> {self.cfg.options_table_path}")

        sides = tagged.groupby(["date", "se_tag"])["option_type"].nunique()
        one_sided = (sides == 1).groupby("date").mean().mean()
        print(f"One-sided SE tags (no straddle possible): {one_sided:.1%}")
        return options


# ---------------------------------------------------------------------------
# Lookup helpers (work on the Options table)
# ---------------------------------------------------------------------------

def daily_cid_table(options: pd.DataFrame, day) -> pd.DataFrame:
    """One day's chain, indexed by CID.

        >>> daily_cid_table(options, "2026-07-01").loc["CAL.NIF50.24000.20260707"]
    """
    day = pd.to_datetime(day)
    t = options.loc[options["CurrentDate"] == day].set_index("CID")[OPTIONS_COLS]
    if t.empty:
        raise KeyError(f"No data for {day.date()} (holiday/weekend/out of range)")
    return t.sort_index()


def all_daily_cid_tables(options: pd.DataFrame, start=None, end=None) -> dict:
    """{date -> daily table} over an inclusive date range (bounds optional)."""
    sub = options
    if start is not None:
        sub = sub[sub["CurrentDate"] >= pd.to_datetime(start)]
    if end is not None:
        sub = sub[sub["CurrentDate"] <= pd.to_datetime(end)]
    return {day: g.set_index("CID")[OPTIONS_COLS].sort_index()
            for day, g in sub.groupby("CurrentDate")}
