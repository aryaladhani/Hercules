"""niftyoptions - NIFTY weekly options data-engineering pipeline.

Modules:
  config         Config dataclass: all paths + parameters
  downloaders    OptionsBhavcopyDownloader, IndexEODDownloader (NSE, free)
  tagging        ContractTagger -> Options table; daily lookup helpers
  spreads        SpreadSeriesBuilder (universe) -> SettleSpreadBuilder (daily)
                 / MinuteSpreadBuilder (1-minute); all share the SpreadSeries table
  fno_config     FnoConfig: date-aware lot size / STT / margin resolver
  minute_store   build_parquet_store + ParquetMinuteStore (1-min warehouse; unused for now)
"""

from .config import Config, DEFAULT_CONFIG
from .downloaders import IndexEODDownloader, OptionsBhavcopyDownloader
from .spreads import (
    MinuteSpreadBuilder,
    SettleSpreadBuilder,
    SpreadSeriesBuilder,
)
from .tagging import ContractTagger, all_daily_cid_tables, daily_cid_table

__all__ = [
    "Config", "DEFAULT_CONFIG",
    "OptionsBhavcopyDownloader", "IndexEODDownloader",
    "ContractTagger", "daily_cid_table", "all_daily_cid_tables",
    "SpreadSeriesBuilder", "SettleSpreadBuilder", "MinuteSpreadBuilder",
]
