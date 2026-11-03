#!/usr/bin/env python3
"""
Write the live data files for the Swarm Trader dashboard (read-only against Alpaca).

Outputs account.json, positions.json, performance.json and market.json into the
given directory. Cash is written as one positions row (symbol CASH, kind cash)
so the holdings table always has a row.

Usage:
  poetry run python tools/dashboard_data.py OUT_DIR [--start 2026-11-03]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()
import requests

from src.accounts import get_account_for_mode
import swing_vs_spy_report as rep
from performance_tracker_v2 import get_alpaca_daily_closes

NY = ZoneInfo("America/New_York")
SYMBOLS = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN"]
NAMES = {"SPY": "S&P 500 ETF", "QQQ": "Nasdaq-100 ETF", "NVDA": "NVIDIA", "AAPL": "Apple",
         "MSFT": "Microsoft", "TSLA": "Tesla", "AMZN": "Amazon"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--start", default="2026-11-03")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    acct = get_account_for_mode("swing")
    H, B = acct.headers, acct.base_url
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def w(name, rows):
        (out / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    ac = requests.get(f"{B}/account", headers=H, timeout=10)
    ac.raise_for_status()
    ac = ac.json()
    eq, last, cash = float(ac["equity"]), float(ac["last_equity"]), float(ac["cash"])
    pos = requests.get(f"{B}/positions", headers=H, timeout=10)
    pos.raise_for_status()
    pos = pos.json()
    w("account", [{"as_of": now, "equity": eq, "cash": cash, "buying_power": float(ac["buying_power"]),
                   "day_pnl": eq - last, "day_pnl_pct": (eq - last) / last if last else 0,
                   "positions": len(pos), "cash_pct": cash / eq if eq else 0}])
    rows = [{"symbol": p["symbol"], "kind": "stock", "qty": float(p["qty"]), "avg_entry": float(p["avg_entry_price"]),
             "price": float(p["current_price"]), "market_value": float(p["market_value"]),
             "unrealized_pl": float(p["unrealized_pl"]), "unrealized_plpc": float(p["unrealized_plpc"]),
             "weight": float(p["market_value"]) / eq if eq else 0} for p in pos]
    rows.append({"symbol": "CASH", "kind": "cash", "qty": None, "avg_entry": None, "price": None,
                 "market_value": cash, "unrealized_pl": 0.0, "unrealized_plpc": 0.0, "weight": cash / eq if eq else 0})
    w("positions", rows)

    hist = rep.portfolio_history(a.start, datetime.now(NY).strftime("%Y-%m-%d"))
    spy = dict(get_alpaca_daily_closes("SPY", 60))
    qqq = dict(get_alpaca_daily_closes("QQQ", 60))
    w("performance", [{"date": d, "equity": e, "spy_close": spy.get(d), "qqq_close": qqq.get(d)} for d, e in hist])

    snap = requests.get("https://data.alpaca.markets/v2/stocks/snapshots",
                        params={"symbols": ",".join(SYMBOLS), "feed": "iex"}, headers=H, timeout=10)
    snap.raise_for_status()
    snap = snap.json()
    mk = []
    for s in SYMBOLS:
        x = snap[s]
        p, prev = x["latestTrade"]["p"], x["prevDailyBar"]["c"]
        mk.append({"symbol": s, "name": NAMES[s], "kind": "index" if s in ("SPY", "QQQ") else "stock",
                   "price": p, "prev_close": prev, "change_pct": (p - prev) / prev,
                   "day_high": x["dailyBar"]["h"], "day_low": x["dailyBar"]["l"], "volume_iex": x["dailyBar"]["v"]})
    w("market", mk)
    print(json.dumps({"as_of": now, "files": sorted(f.name for f in out.glob("*.json"))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
