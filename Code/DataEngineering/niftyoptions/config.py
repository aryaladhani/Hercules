"""Central configuration: every path and tunable parameter in one place.

Change values here (or pass a custom Config) instead of editing constants
scattered across modules. Paths target the repo's ``Data/`` tree:

    Data/
      Raw/bar_daily/     options_bhavcopy.parquet, nifty50_index_eod.csv
      Raw/bar_min/    Options_2025/{expiry}/NIFTY_*.csv
      Processed/Options/        Options.parquet, options_tagged.parquet
      Processed/Spread/         SpreadSeries.parquet
                    .../Daily Settle Spreads/
                    .../Minute Level OHLC Spreads/
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # ---- directories -----------------------------------------------------
    # config.py lives at <root>/Code/DataEngineering/niftyoptions/config.py
    project_root: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[3]
    )

    @property
    def data_dir(self) -> Path:
        return self.project_root / "Data"

    @property
    def raw_daily_dir(self) -> Path:
        return self.data_dir / "Raw" / "bar_daily"

    @property
    def raw_minute_dir(self) -> Path:
        """Root of the minute store (contains ``Options_{YYYY}/`` folders)."""
        return self.data_dir / "Raw" / "bar_min"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "Processed"

    @property
    def options_dir(self) -> Path:
        return self.processed_dir / "Options"

    @property
    def spread_dir(self) -> Path:
        return self.processed_dir / "Spread"

    @property
    def settle_spread_dir(self) -> Path:
        return self.spread_dir / "SettleSpreads"

    @property
    def minute_spread_dir(self) -> Path:
        return self.spread_dir / "Spreads"

    # ---- raw files (downloader outputs) ----------------------------------
    @property
    def raw_options_path(self) -> Path:
        return self.raw_daily_dir / "options_bhavcopy.parquet"

    @property
    def index_eod_path(self) -> Path:
        return self.raw_daily_dir / "nifty50_index_eod.csv"

    # ---- processed files --------------------------------------------------
    @property
    def tagged_path(self) -> Path:
        return self.options_dir / "options_tagged.parquet"

    @property
    def options_table_path(self) -> Path:
        return self.options_dir / "Options.parquet"

    @property
    def spread_series_path(self) -> Path:
        """The universe/identity table (no leg prices)."""
        return self.spread_dir / "SpreadSeries.parquet"

    # ---- parameters --------------------------------------------------------
    symbol: str = "NIFTY"            # NSE symbol to filter
    tag_root: str = "NIF50"          # root used inside SE tags / CIDs
    index_name: str = "Nifty 50"     # row name in NSE index bhavcopy

    request_delay: float = 0.4       # per-thread politeness delay between NSE requests
    download_workers: int = 8        # threads for parallel NSE downloads
    min_dte: int = 3                 # spreads: Leg1 expiry >= date + min_dte
    max_series: int = 4              # spreads: Active1..ActiveN
    strike_band_series: int = 10     # SpreadSeries: keep |StrikeSeries| <= this

    def ensure_dirs(self) -> "Config":
        for d in (self.raw_daily_dir, self.raw_minute_dir, self.options_dir,
                  self.spread_dir, self.settle_spread_dir, self.minute_spread_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


DEFAULT_CONFIG = Config()
