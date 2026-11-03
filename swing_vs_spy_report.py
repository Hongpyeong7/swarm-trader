#!/usr/bin/env python3
"""
Swing account vs S&P 500 (SPY) performance report.

The equity curve comes from Alpaca's own account portfolio history, so a
missed daily snapshot never leaves a gap. SPY closes come from Alpaca market
data. Daily snapshots in snapshots/ (performance_tracker_v2.py --snapshot)
are listed for cross-checking.

Usage:
  poetry run python swing_vs_spy_report.py --start 2026-11-03            # start -> today
  poetry run python swing_vs_spy_report.py --start 2026-11-03 --days 30  # fixed window
  poetry run python swing_vs_spy_report.py --start 2026-11-03 --out reports/
"""

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import requests

from src.accounts import get_account_for_mode
from performance_tracker_v2 import get_alpaca_daily_closes, load_snapshots, RISK_FREE_DAILY

ROOT = Path(__file__).parent
NY = ZoneInfo("America/New_York")


def portfolio_history(start: str, end: str, mode: str = "swing") -> list[tuple[str, float]]:
    """Daily (date, equity) from Alpaca, oldest first, dropping pre-funding zero rows."""
    acct = get_account_for_mode(mode)
    r = requests.get(
        f"{acct.base_url}/account/portfolio/history",
        params={"timeframe": "1D", "start": start, "end": end, "intraday_reporting": "market_hours"},
        headers=acct.headers, timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    rows = {}
    for ts, eq in zip(d.get("timestamp") or [], d.get("equity") or []):
        if eq is None or eq <= 0:
            continue
        # Alpaca stamps daily bars at 20:00 New York time; read the date there, not in UTC.
        day = datetime.fromtimestamp(ts, tz=NY).strftime("%Y-%m-%d")
        rows[day] = float(eq)
    # Today's bar lands in history only later; use live equity for today if in range.
    today = datetime.now(NY).strftime("%Y-%m-%d")
    if start <= today <= end and today not in rows:
        a = requests.get(f"{acct.base_url}/account", headers=acct.headers, timeout=10)
        a.raise_for_status()
        rows[today] = float(a.json()["equity"])
    return sorted(rows.items())


def returns(series: list[float]) -> list[float]:
    return [(b - a) / a for a, b in zip(series, series[1:]) if a]


def max_drawdown(series: list[float]) -> float:
    peak, mdd = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return mdd


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(rets: list[float]) -> float | None:
    sd = stdev(rets)
    if not rets or sd == 0:
        return None
    return (sum(rets) / len(rets) - RISK_FREE_DAILY) / sd * math.sqrt(252)


def beta_corr(p: list[float], b: list[float]) -> tuple[float | None, float | None]:
    n = min(len(p), len(b))
    if n < 3:
        return None, None
    p, b = p[-n:], b[-n:]
    mp, mb = sum(p) / n, sum(b) / n
    cov = sum((x - mp) * (y - mb) for x, y in zip(p, b)) / (n - 1)
    vb, vp = stdev(b) ** 2, stdev(p) ** 2
    beta = cov / vb if vb else None
    corr = cov / math.sqrt(vp * vb) if vp and vb else None
    return beta, corr


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def build(start: str, end: str, mode: str) -> tuple[str, dict]:
    eq = portfolio_history(start, end, mode)
    spy_all = get_alpaca_daily_closes("SPY", days=(date.fromisoformat(end) - date.fromisoformat(start)).days + 5)
    spy = {d: c for d, c in spy_all if start <= d <= end}

    # Align on trading days present in both series
    days = [d for d, _ in eq if d in spy]
    eqm = dict(eq)
    p_series = [eqm[d] for d in days]
    s_series = [spy[d] for d in days]
    if len(days) < 2:
        raise SystemExit(f"Not enough overlapping trading days between {start} and {end} ({len(days)} found).")

    pr, sr = returns(p_series), returns(s_series)
    p_tot = p_series[-1] / p_series[0] - 1
    s_tot = s_series[-1] / s_series[0] - 1
    beta, corr = beta_corr(pr, sr)
    beat_days = sum(1 for a, b in zip(pr, sr) if a > b)
    best = max(zip(days[1:], pr), key=lambda x: x[1])
    worst = min(zip(days[1:], pr), key=lambda x: x[1])
    snaps = [s for s in load_snapshots() if start <= s.get("date", "") <= end]

    m = {
        "start": days[0], "end": days[-1], "trading_days": len(days),
        "equity_start": p_series[0], "equity_end": p_series[-1],
        "swing_return": p_tot, "spy_return": s_tot, "alpha": p_tot - s_tot,
        "swing_mdd": max_drawdown(p_series), "spy_mdd": max_drawdown(s_series),
        "swing_vol_ann": stdev(pr) * math.sqrt(252), "spy_vol_ann": stdev(sr) * math.sqrt(252),
        "swing_sharpe": sharpe(pr), "spy_sharpe": sharpe(sr),
        "beta": beta, "corr": corr, "days_beat_spy": beat_days, "days_compared": len(pr),
        "best_day": best, "worst_day": worst, "snapshots_found": len(snaps),
    }

    verdict = "beat" if m["alpha"] > 0 else "trailed"
    lines = [
        f"# Swing vs S&P 500 (SPY): {m['start']} to {m['end']}",
        "",
        f"The swing account **{verdict} SPY by {abs(m['alpha']) * 100:.2f} percentage points** "
        f"over {m['trading_days']} trading days ({pct(m['swing_return'])} vs {pct(m['spy_return'])}).",
        "",
        "| Metric | Swing account | SPY |",
        "|---|---|---|",
        f"| Total return | {pct(m['swing_return'])} | {pct(m['spy_return'])} |",
        f"| Max drawdown | {pct(m['swing_mdd'])} | {pct(m['spy_mdd'])} |",
        f"| Volatility (annualized) | {pct(m['swing_vol_ann'])} | {pct(m['spy_vol_ann'])} |",
        f"| Sharpe (annualized, rf 4.5%) | {num(m['swing_sharpe'])} | {num(m['spy_sharpe'])} |",
        "",
        f"- Equity: ${m['equity_start']:,.2f} → ${m['equity_end']:,.2f}",
        f"- Beta to SPY: {num(m['beta'])}, correlation: {num(m['corr'])}",
        f"- Days the account beat SPY: {m['days_beat_spy']} of {m['days_compared']}",
        f"- Best day: {m['best_day'][0]} ({pct(m['best_day'][1])}); worst day: {m['worst_day'][0]} ({pct(m['worst_day'][1])})",
        f"- Daily snapshots on disk for this window: {m['snapshots_found']} (equity curve itself comes from Alpaca portfolio history)",
        "",
        "## Daily series",
        "",
        "| Date | Swing equity | Swing day | SPY close | SPY day |",
        "|---|---|---|---|---|",
    ]
    for i, d in enumerate(days):
        pd_ = pct(pr[i - 1]) if i else ""
        sd_ = pct(sr[i - 1]) if i else ""
        lines.append(f"| {d} | ${p_series[i]:,.2f} | {pd_} | ${s_series[i]:,.2f} | {sd_} |")
    if m["swing_return"] == 0 and m["swing_vol_ann"] == 0:
        lines += ["", "> Note: the account's equity never moved, so no trades were held in this window."]
    return "\n".join(lines) + "\n", m


def main() -> int:
    ap = argparse.ArgumentParser(description="Swing account vs SPY report")
    ap.add_argument("--start", required=True, help="Window start date (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, help="Calendar days after --start (default: through today)")
    ap.add_argument("--mode", default="swing", choices=["swing", "day"])
    ap.add_argument("--out", help="Directory to write the Markdown report and JSON metrics")
    a = ap.parse_args()
    end = (date.fromisoformat(a.start) + timedelta(days=a.days)).isoformat() if a.days else datetime.now(NY).date().isoformat()
    report, metrics = build(a.start, end, a.mode)
    print(report)
    if a.out:
        out = ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{a.mode}_vs_spy_{metrics['start']}_{metrics['end']}"
        (out / f"{stem}.md").write_text(report, encoding="utf-8")
        (out / f"{stem}.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
        print(f"Saved {out / (stem + '.md')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
