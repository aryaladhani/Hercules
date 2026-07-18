"""Run the data-engineering pipeline end-to-end: raw NSE data -> Options table
-> SpreadSeries universe -> one synthetic contract's daily SettleSpreads.

Steps:
  1. Download F&O options bhavcopy      -> Raw/bar_daily/options_bhavcopy.parquet
  2. Download NIFTY 50 index EOD closes -> Raw/bar_daily/nifty50_index_eod.csv
  3. Tag contracts + build Options table-> Processed/Options/Options.parquet
  4. Build the SpreadSeries universe    -> Processed/Spread/SpreadSeries.parquet
  5. Build daily SettleSpreads for one  -> Processed/Spread/Daily Settle Spreads/
     synthetic contract (default CAL / Active2 / StrikeSeries +2)

Minute-level spreads are a separate, heavier step — build them on demand with
niftyoptions.MinuteSpreadBuilder once the SpreadSeries table exists.

Usage:
  python run_pipeline.py --start 2019-01-01 --end 2024-12-31
  python run_pipeline.py --start ... --end ... --skip-download        # reuse Raw
  python run_pipeline.py --start ... --end ... --expiry Active2 --strike 2 --opt CAL

Tip: try a short range (a few weeks) first - the full 5y download takes
~15-20 min against NSE at the polite default request delay.
"""

import argparse
from datetime import date

from niftyoptions import (
    DEFAULT_CONFIG,
    ContractTagger,
    IndexEODDownloader,
    OptionsBhavcopyDownloader,
    SettleSpreadBuilder,
    SpreadSeriesBuilder,
)


def main():
    p = argparse.ArgumentParser(description="NIFTY options data-engineering runner")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--skip-download", action="store_true",
                   help="Reuse existing Raw (skip steps 1-2).")
    p.add_argument("--index-source", choices=["nse", "yfinance"], default="nse")
    p.add_argument("--expiry", default="Active2", help="ExpirySeries for SettleSpreads")
    p.add_argument("--strike", type=int, default=2, help="StrikeSeries for SettleSpreads")
    p.add_argument("--opt", choices=["CAL", "PUT"], default="CAL",
                   help="OptionType for SettleSpreads")
    args = p.parse_args()

    cfg = DEFAULT_CONFIG.ensure_dirs()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    if not args.skip_download:
        print("== Step 1/5: options bhavcopy ==")
        OptionsBhavcopyDownloader(cfg).run(start, end)
        print("== Step 2/5: index EOD closes ==")
        IndexEODDownloader(cfg).run(start, end, source=args.index_source)
    else:
        print("== Steps 1-2 skipped (using existing Raw) ==")

    print("== Step 3/5: tagging + Options table ==")
    ContractTagger(cfg).run(start=args.start, end=args.end)

    print("== Step 4/5: SpreadSeries universe ==")
    SpreadSeriesBuilder(cfg).run()

    print(f"== Step 5/5: SettleSpreads ({args.opt}/{args.expiry}/{args.strike:+d}) ==")
    SettleSpreadBuilder(cfg).run(expiry_series=args.expiry,
                                 strike_series=args.strike, option_type=args.opt)

    print("\nPipeline complete. Key outputs: "
          f"{cfg.options_table_path}, {cfg.spread_series_path}")


if __name__ == "__main__":
    main()
