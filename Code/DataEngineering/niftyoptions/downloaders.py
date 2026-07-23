"""Downloaders for NSE data (free, official archives).

Classes
-------
NSESession                : requests.Session with browser headers + cookie warm-up
OptionsBhavcopyDownloader : daily F&O bhavcopy -> data/raw/options_bhavcopy.parquet
IndexEODDownloader        : NIFTY 50 index closes -> data/raw/nifty50_index_eod.csv

Both downloaders handle NSE's bhavcopy format switch (2024-07-08) and treat
404s on weekdays as holidays. NSE blocks plain scripted requests, hence the
session warm-up and realistic headers.
"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd

from .config import Config, DEFAULT_CONFIG

FORMAT_SWITCH_DATE = date(2024, 7, 8)  # first day of the new UDiFF format

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}


class NSESession:
    """Lazily-created requests session with NSE cookie warm-up."""

    def __init__(self):
        self._session = None

    @property
    def session(self):
        if self._session is None:
            import requests

            s = requests.Session()
            s.headers.update(_HEADERS)
            s.get("https://www.nseindia.com", timeout=10)
            s.get("https://www.nseindia.com/all-reports", timeout=10)
            self._session = s
        return self._session

    def get(self, url: str, timeout: int = 15):
        return self.session.get(url, timeout=timeout)


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def threaded_fetch(fetch_one, items, workers, delay=0.0, desc="download"):
    """Run ``fetch_one(session, item)`` across a thread pool, one warmed-up
    NSE session per worker thread.

    Downloads are network-I/O bound (each call mostly waits on NSE), so threads
    — not processes — are the right tool: they share memory, skip interpreter
    startup, and release the GIL while blocked on the socket. Each thread keeps
    its own ``NSESession`` in thread-local storage so the cookie warm-up happens
    once per thread, never concurrently on a shared session (requests.Session is
    not thread-safe). Returns a list of ``(item, result)`` in completion order.
    """
    from tqdm import tqdm

    local = threading.local()

    def _work(item):
        http = getattr(local, "http", None)
        if http is None:
            http = local.http = NSESession()
        try:
            return item, fetch_one(item, http)
        finally:
            if delay:
                time.sleep(delay)          # politeness throttle, per thread

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, it) for it in items]
        for fut in tqdm(as_completed(futures), total=len(futures), desc=desc):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# F&O options bhavcopy
# ---------------------------------------------------------------------------

class OptionsBhavcopyDownloader:
    """Downloads + normalizes daily F&O bhavcopy into one parquet file."""

    def __init__(self, config: Config = DEFAULT_CONFIG, session: NSESession | None = None):
        self.cfg = config
        self.http = session or NSESession()

    # ---- URLs ----
    @staticmethod
    def _old_url(d: date) -> str:
        mon = d.strftime("%b").upper()
        # Legacy host archives.nseindia.com now 503s; nsearchives serves the
        # same historical old-format zips.
        return (
            "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
            f"{d.year}/{mon}/fo{d.strftime('%d')}{mon}{d.year}bhav.csv.zip"
        )

    @staticmethod
    def _new_url(d: date) -> str:
        return (
            "https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
        )

    # ---- one day ----
    def fetch_day(self, d: date, http: "NSESession" | None = None) -> pd.DataFrame | None:
        http = http or self.http
        url = self._old_url(d) if d < FORMAT_SWITCH_DATE else self._new_url(d)
        try:
            resp = http.get(url)
        except Exception:
            return None
        if resp.status_code != 200 or len(resp.content) < 200:
            return None  # holiday / not yet published
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    raw = pd.read_csv(f)
        except (zipfile.BadZipFile, IndexError):
            return None
        return self._normalize(raw, d)

    def _normalize(self, df: pd.DataFrame, d: date) -> pd.DataFrame:
        """Map old + new schema to one common schema, filter to our symbol."""
        sym = self.cfg.symbol
        if "SYMBOL" in {c.upper() for c in df.columns}:  # old format
            df = df.rename(columns=str.upper)
            df = df[(df["SYMBOL"] == sym) & (df["INSTRUMENT"] == "OPTIDX")]
            out = pd.DataFrame({
                "date": d,
                "symbol": df["SYMBOL"],
                "expiry": pd.to_datetime(df["EXPIRY_DT"], format="%d-%b-%Y",
                                         errors="coerce"),
                "strike": df["STRIKE_PR"],
                "option_type": df["OPTION_TYP"],
                "open": df["OPEN"], "high": df["HIGH"],
                "low": df["LOW"], "close": df["CLOSE"],
                "settle_price": df["SETTLE_PR"],
                "volume_contracts": df["CONTRACTS"],
                "open_interest": df["OPEN_INT"],
                "change_in_oi": df["CHG_IN_OI"],
                "underlying_close": pd.NA,  # not present in old format
            })
        else:  # new UDiFF format
            df.columns = [c.strip() for c in df.columns]
            if "FinInstrmTp" in df.columns:
                df = df[df["FinInstrmTp"] == "IDO"]
            df = df[df["TckrSymb"] == sym]
            out = pd.DataFrame({
                "date": d,
                "symbol": df["TckrSymb"],
                "expiry": pd.to_datetime(df["XpryDt"], errors="coerce"),
                "strike": df["StrkPric"],
                "option_type": df["OptnTp"],
                "open": df["OpnPric"], "high": df["HghPric"],
                "low": df["LwPric"], "close": df["ClsPric"],
                "settle_price": df["SttlmPric"],
                "volume_contracts": df["TtlTradgVol"],
                "open_interest": df["OpnIntrst"],
                "change_in_oi": df["ChngInOpnIntrst"],
                "underlying_close": df.get("UndrlygPric", pd.NA),
            })
        return out[out["option_type"].isin(["CE", "PE"])]

    # ---- full run ----
    def run(self, start: date, end: date, workers: int | None = None) -> pd.DataFrame:
        self.cfg.ensure_dirs()
        days = list(weekdays(start, end))
        workers = workers or self.cfg.download_workers
        pairs = threaded_fetch(self.fetch_day, days, workers,
                               delay=self.cfg.request_delay, desc="F&O bhavcopy")

        frames, missing = [], []
        for d, day in pairs:
            (frames.append(day) if day is not None and not day.empty
             else missing.append(d))

        if not frames:
            raise RuntimeError("No data downloaded - check network/NSE access.")

        full = pd.concat(frames, ignore_index=True)
        full.sort_values(["date", "expiry", "strike", "option_type"],
                         inplace=True)
        full.to_parquet(self.cfg.raw_options_path, index=False)
        print(f"{len(full):,} rows / {full['date'].nunique()} days "
              f"-> {self.cfg.raw_options_path}")
        if missing:
            print(f"{len(missing)} weekdays skipped (holidays), "
                  f"e.g. {missing[:3]}")
        return full


# ---------------------------------------------------------------------------
# NIFTY 50 index EOD closes
# ---------------------------------------------------------------------------

class IndexEODDownloader:
    """Official NSE index closes; needed to backfill Asset for pre-2024-07-08
    dates (old bhavcopy format has no underlying price)."""

    def __init__(self, config: Config = DEFAULT_CONFIG, session: NSESession | None = None):
        self.cfg = config
        self.http = session or NSESession()

    @staticmethod
    def _url(d: date) -> str:
        return ("https://nsearchives.nseindia.com/content/indices/"
                f"ind_close_all_{d.strftime('%d%m%Y')}.csv")

    def fetch_day(self, d: date, http: "NSESession" | None = None) -> float | None:
        http = http or self.http
        try:
            resp = http.get(self._url(d))
        except Exception:
            return None
        if resp.status_code != 200 or len(resp.content) < 100:
            return None
        try:
            df = pd.read_csv(io.BytesIO(resp.content))
        except Exception:
            return None
        df.columns = [c.strip() for c in df.columns]
        if not {"Index Name", "Closing Index Value"} <= set(df.columns):
            return None
        row = df[df["Index Name"].str.strip().str.casefold()
                 == self.cfg.index_name.casefold()]
        if row.empty:
            return None
        return float(str(row.iloc[0]["Closing Index Value"]).replace(",", ""))

    def run_nse(self, start: date, end: date, workers: int | None = None) -> pd.DataFrame:
        days = list(weekdays(start, end))
        workers = workers or self.cfg.download_workers
        pairs = threaded_fetch(self.fetch_day, days, workers,
                               delay=self.cfg.request_delay, desc="Index EOD")
        records = [{"date": d, "close": close}
                   for d, close in pairs if close is not None]
        return pd.DataFrame(records)

    def run_yfinance(self, start: date, end: date) -> pd.DataFrame:
        import yfinance as yf

        raw = yf.download("^NSEI", start=start.isoformat(),
                          end=(end + timedelta(days=1)).isoformat(),
                          progress=False, auto_adjust=False)
        if raw.empty:
            return pd.DataFrame(columns=["date", "close"])
        close = raw["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        out = close.reset_index()
        out.columns = ["date", "close"]
        out["date"] = pd.to_datetime(out["date"]).dt.date
        return out

    def run(self, start: date, end: date, source: str = "nse") -> pd.DataFrame:
        self.cfg.ensure_dirs()
        df = self.run_nse(start, end) if source == "nse" else self.run_yfinance(start, end)
        if df.empty:
            raise RuntimeError("No index data downloaded.")

        path = self.cfg.index_eod_path
        if path.exists():  # incremental merge: newest wins on overlaps
            old = pd.read_csv(path, parse_dates=["date"])
            old["date"] = old["date"].dt.date
            df = (pd.concat([old, df], ignore_index=True)
                  .drop_duplicates(subset="date", keep="last"))
        df = df.sort_values("date").reset_index(drop=True)
        df.to_csv(path, index=False)
        print(f"{len(df):,} index closes -> {path}")
        return df
