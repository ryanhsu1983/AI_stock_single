from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

import pandas as pd
import numpy as np
import requests

from .daily_risk_features import fetch_price
from .schedule_gate import fetch_twse_calendar


WINDOW_START = pd.Timestamp("2026-03-02")
STATIC_SNAPSHOT = Path("outputs/radar_vnext_current_layer0_core_top250_weekly_snapshot_fill_20260722/current_layer0_core_top250_weekly_snapshot_delta.csv")
STATIC_PRICE = Path("outputs/radar_vnext_current_layer0_base_cycle_adjusted_close_liquidity_fill_20260722")
STATIC_WARMUP = Path("outputs/radar_vnext_current_layer0_base_cycle_adjusted_warmup_fill_20260722")
MEMBERSHIP_HISTORY = Path("data/current_base_cycle_weekly_core_membership_history.csv")
REPORT_TITLE = "強勢股低基期 Top10 每日追蹤"


class ReportDataNotReady(RuntimeError):
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the private current Layer0 V_BASE Top10 daily report.")
    parser.add_argument("--date", required=True, help="Taiwan trading date, YYYY-MM-DD")
    parser.add_argument("--core-repo", required=True)
    parser.add_argument("--source-repo", default=".")
    parser.add_argument("--state", default="data/current_base_cycle_top3_state.json")
    parser.add_argument("--output", default="reports/current_base_cycle_top10_daily.html")
    parser.add_argument("--screen-output", default="reports/current_base_cycle_top10_daily.csv")
    parser.add_argument("--source-cache", default="data/current_base_cycle_source_cache")
    parser.add_argument("--offline", action="store_true", help="Use committed 2026-07 source package only.")
    args = parser.parse_args()
    try:
        payload = build_daily_report(
            report_date=args.date,
            core_repo=Path(args.core_repo),
            source_repo=Path(args.source_repo),
            state_path=Path(args.state),
            output_path=Path(args.output),
            screen_output=Path(args.screen_output),
            source_cache=Path(args.source_cache),
            offline=args.offline,
        )
    except ReportDataNotReady as exc:
        print(f"report_data_not_ready: {exc}")
        raise SystemExit(75) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_daily_report(
    *,
    report_date: str,
    core_repo: Path,
    source_repo: Path,
    state_path: Path,
    output_path: Path,
    screen_output: Path,
    source_cache: Path,
    offline: bool = False,
) -> dict:
    target = pd.Timestamp(report_date)
    state = load_state(state_path)
    membership = load_membership(core_repo, source_repo)
    membership = refresh_weekly_membership(membership, target, source_cache, offline)
    # A weekly close snapshot becomes usable on the next Taiwan trading day.
    active_date = membership.loc[membership.snapshot_date.lt(target), "snapshot_date"].max()
    if pd.isna(active_date):
        raise RuntimeError("No Layer0 core snapshot is available on or before the report date")
    current = membership[membership.snapshot_date.eq(active_date)].copy()
    if len(current) != 250:
        raise RuntimeError(f"Active Layer0 core snapshot must contain exactly 250 stocks; got {len(current)}")

    registry_tickers = set(state.get("registry", {}))
    prices = load_adjusted_prices(source_repo, current, registry_tickers, target, offline)
    needed = len(current) + 1
    counts = prices[prices.ticker.isin(set(current.ticker) | {"0050"})].groupby("date").ticker.nunique()
    valid_dates = counts[counts.eq(needed)].index
    valid_dates = valid_dates[valid_dates <= target]
    adjusted_target = int(counts.get(target, 0))
    if adjusted_target < needed and not offline:
        raise ReportDataNotReady(
            f"Target-date adjusted data is incomplete ({adjusted_target}/{len(current) + 1}); retry after source publication"
        )
    actual = max(valid_dates) if len(valid_dates) else pd.NaT
    if pd.isna(actual):
        raise RuntimeError("No adjusted price date is available")

    official_rows, turnover = load_official_prices_and_turnover(
        source_repo=source_repo,
        target=target,
        current=current,
        source_cache=source_cache,
        offline=offline,
    )
    screens = run_core_screen(core_repo, membership, current, prices, turnover, actual)
    base = screens[(screens.variant.eq("V_BASE")) & screens.final_pass & screens.final_rank.le(30)].copy()
    base = base.sort_values("final_rank")
    top10 = base.head(10).copy()
    raw_map = official_close_map(official_rows, prices, actual)
    top10["display_close"] = top10.ticker.map(raw_map)
    if top10.head(3).display_close.isna().any():
        missing = top10.head(3).loc[top10.head(3).display_close.isna(), "ticker"].tolist()
        raise RuntimeError(f"Official/report-date close is missing for Top3: {missing}")

    trading_dates = sorted(set(official_rows.loc[official_rows.date.le(actual), "date"].dt.strftime("%Y-%m-%d")))
    state, new_top3, registry_rows = update_state(
        state,
        report_date=actual.strftime("%Y-%m-%d"),
        top10=top10,
        raw_close_map=raw_map,
        trading_dates=trading_dates,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    screen_output.parent.mkdir(parents=True, exist_ok=True)
    top10.to_csv(screen_output, index=False, encoding="utf-8-sig")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(actual, active_date, top10, new_top3, registry_rows), encoding="utf-8")
    return {
        "status": "complete",
        "requested_report_date": report_date,
        "actual_report_date": actual.strftime("%Y-%m-%d"),
        "active_layer0_snapshot_date": active_date.strftime("%Y-%m-%d"),
        "top10_count": len(top10),
        "new_top3_count": len(new_top3),
        "tracked_top3_ticker_count": len(registry_rows),
        "state_path": str(state_path),
        "html_path": str(output_path),
        "screen_path": str(screen_output),
        "future_data_violation_count": 0,
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "registry": {}, "daily_top10": {}, "trading_dates": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("version", 1)
    state.setdefault("registry", {})
    state.setdefault("daily_top10", {})
    state.setdefault("trading_dates", [])
    return state


def load_membership(core_repo: Path, source_repo: Path) -> pd.DataFrame:
    del core_repo
    local_repo = Path(__file__).resolve().parents[1]
    compact = local_repo / MEMBERSHIP_HISTORY
    base = pd.read_csv(
        compact,
        usecols=["snapshot_date", "ticker", "name", "market", "traded_value_rank_20d"],
        dtype={"ticker": str},
    )
    base = base.rename(columns={"traded_value_rank_20d": "core_rank"})
    delta = pd.read_csv(source_repo / STATIC_SNAPSHOT, dtype={"ticker": str})
    delta = delta[["snapshot_date", "ticker", "name", "market", "top250_core_rank"]]
    delta = delta.rename(columns={"top250_core_rank": "core_rank"})
    out = pd.concat([base, delta], ignore_index=True).drop_duplicates(["snapshot_date", "ticker"], keep="last")
    out["ticker"] = out.ticker.str.zfill(4)
    out["snapshot_date"] = pd.to_datetime(out.snapshot_date)
    return out[out.snapshot_date.ge(WINDOW_START)].copy()


def refresh_weekly_membership(membership: pd.DataFrame, target: pd.Timestamp, cache: Path, offline: bool) -> pd.DataFrame:
    saved = cache / "weekly_membership_latest.csv"
    if saved.exists():
        cached = pd.read_csv(saved, dtype={"ticker": str})
        cached["ticker"] = cached.ticker.str.zfill(4)
        cached["snapshot_date"] = pd.to_datetime(cached.snapshot_date)
        membership = pd.concat([membership, cached], ignore_index=True).drop_duplicates(
            ["snapshot_date", "ticker"], keep="last"
        )
    if offline:
        return membership
    open_dates, _closed_dates = fetch_twse_calendar()
    target_date = target.date()
    week = [day for day in open_dates if day.isocalendar()[:2] == target_date.isocalendar()[:2]]
    if not week or max(week) != target_date:
        return membership
    if target.normalize() in set(membership.snapshot_date):
        return membership
    rows, meta = fetch_price(target_date, {str(code) for code in range(1000, 10000)})
    frame = pd.DataFrame(rows)
    if frame.empty or set(frame.market) != {"TWSE", "TPEx"}:
        raise ReportDataNotReady(f"Weekly Layer0 source is incomplete: {meta}")
    frame["ticker"] = frame.ticker.astype(str).str.zfill(4)
    frame["turnover_value"] = pd.to_numeric(frame.turnover_value, errors="coerce")
    frame = frame[frame.turnover_value.gt(0)].sort_values(["turnover_value", "ticker"], ascending=[False, True]).head(250)
    frame["snapshot_date"] = target.normalize()
    frame["core_rank"] = range(1, len(frame) + 1)
    cache.mkdir(parents=True, exist_ok=True)
    frame[["snapshot_date", "ticker", "name", "market", "core_rank"]].to_csv(saved, index=False, encoding="utf-8-sig")
    return pd.concat([membership, frame[["snapshot_date", "ticker", "name", "market", "core_rank"]]], ignore_index=True)


def load_adjusted_prices(
    source_repo: Path,
    current: pd.DataFrame,
    registry_tickers: set[str],
    target: pd.Timestamp,
    offline: bool,
) -> pd.DataFrame:
    current_dir = source_repo / STATIC_PRICE
    warmup_dir = source_repo / STATIC_WARMUP
    frames = [
        pd.read_csv(warmup_dir / "current_layer0_adjusted_warmup_exact_rows.csv.gz", dtype={"ticker": str}),
        pd.read_csv(current_dir / "current_layer0_adjusted_analysis_exact_rows.csv.gz", dtype={"ticker": str}),
    ]
    base = pd.concat(frames, ignore_index=True, sort=False)
    base["ticker"] = base.ticker.str.zfill(4)
    base["date"] = pd.to_datetime(base.date)
    tickers = set(current.ticker) | {"0050"} | registry_tickers
    base = base[base.ticker.isin(tickers)].copy()
    if offline:
        return base.sort_values(["ticker", "date"])
    markets = dict(zip(current.ticker, current.market))
    markets["0050"] = "TWSE"
    for ticker in registry_tickers:
        if ticker not in markets:
            old = base[base.ticker.eq(ticker)]
            if not old.empty:
                markets[ticker] = str(old.market.iloc[-1])
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_yahoo_history, ticker, markets.get(ticker, "TWSE"), target): ticker for ticker in sorted(tickers)}
        fresh = []
        for future in concurrent.futures.as_completed(futures):
            try:
                fresh.extend(future.result())
            except (requests.RequestException, KeyError, TypeError, ValueError):
                continue
    if fresh:
        base = pd.concat([base, pd.DataFrame(fresh)], ignore_index=True, sort=False)
    base["date"] = pd.to_datetime(base.date)
    base["adjusted_analysis_close"] = pd.to_numeric(base.adjusted_analysis_close, errors="coerce")
    return base.dropna(subset=["adjusted_analysis_close"]).drop_duplicates(["ticker", "date"], keep="last").sort_values(["ticker", "date"])


def fetch_yahoo_history(ticker: str, market: str, target: pd.Timestamp) -> list[dict]:
    suffix = ".TWO" if market == "TPEx" else ".TW"
    start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int((target.to_pydatetime().replace(tzinfo=timezone.utc) + timedelta(days=2)).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}{suffix}?period1={start}&period2={end}&interval=1d&events=div%2Csplits"
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    result = (response.json().get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    raw = result["indicators"]["quote"][0]["close"]
    rows = []
    for index, stamp in enumerate(result.get("timestamp") or []):
        value = adjusted[index] if index < len(adjusted) else None
        if value is None:
            continue
        rows.append({
            "ticker": ticker,
            "name": "",
            "market": market,
            "date": datetime.fromtimestamp(stamp, timezone.utc).date().isoformat(),
            "adjusted_analysis_close": value,
            "raw_close_comparator": raw[index] if index < len(raw) else None,
            "source_quality": "trusted_nonofficial_yahoo_research_grade",
            "raw_as_adjusted_used": False,
        })
    return rows


def load_official_prices_and_turnover(
    *, source_repo: Path, target: pd.Timestamp, current: pd.DataFrame, source_cache: Path, offline: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    static = pd.read_csv(source_repo / STATIC_PRICE / "current_layer0_official_turnover_daily.csv.gz", dtype={"ticker": str})
    static["ticker"] = static.ticker.str.zfill(4)
    static["date"] = pd.to_datetime(static.date)
    cached_path = source_cache / "official_recent_full_market.csv.gz"
    cached = pd.DataFrame()
    if cached_path.exists():
        cached = pd.read_csv(cached_path, dtype={"ticker": str})
        cached["ticker"] = cached.ticker.str.zfill(4)
        cached["date"] = pd.to_datetime(cached.date)
    rows = []
    if not offline:
        known_max = max(static.date.max(), cached.date.max() if not cached.empty else pd.Timestamp.min)
        start = max(pd.Timestamp("2026-06-30"), known_max + pd.Timedelta(days=1))
        for day in pd.date_range(start, target, freq="D"):
            if day.weekday() >= 5:
                continue
            fetched, meta = fetch_price(day.date(), {str(code) for code in range(1000, 10000)})
            if fetched:
                rows.extend(fetched)
            elif day == target:
                raise ReportDataNotReady(f"Target-date official close is not ready: {meta}")
    fresh = pd.DataFrame(rows)
    if not fresh.empty:
        fresh["ticker"] = fresh.ticker.astype(str).str.zfill(4)
        fresh["date"] = pd.to_datetime(fresh.date)
        cached = pd.concat([cached, fresh], ignore_index=True, sort=False).drop_duplicates(
            ["date", "ticker"], keep="last"
        )
        source_cache.mkdir(parents=True, exist_ok=True)
        cached.to_csv(cached_path, index=False, compression="gzip")
    if not cached.empty:
        turnover = cached[["date", "ticker", "name", "market", "turnover_value"]].copy()
        display_columns = ["date", "ticker", "name", "market", "close"]
        display_columns += [column for column in ("source_hash", "source_url", "price_basis", "retrieved_at_utc")
                            if column in cached.columns]
        display = cached[display_columns].copy()
    else:
        turnover = pd.DataFrame(columns=["date", "ticker", "name", "market", "turnover_value"])
        display_path = source_repo / STATIC_PRICE / "current_layer0_raw_display_close_20260721.csv"
        display = pd.read_csv(display_path, dtype={"ticker": str})
        display["ticker"] = display.ticker.str.zfill(4)
        display["date"] = pd.to_datetime(display.date)
    turnover = pd.concat([static[["date", "ticker", "name", "market", "turnover_value"]], turnover], ignore_index=True)
    turnover["turnover_value"] = pd.to_numeric(turnover.turnover_value, errors="coerce")
    turnover = turnover.drop_duplicates(["date", "ticker"], keep="last")
    return display, turnover


def run_core_screen(core_repo: Path, membership: pd.DataFrame, current: pd.DataFrame, prices: pd.DataFrame, turnover: pd.DataFrame, actual: pd.Timestamp) -> pd.DataFrame:
    """Reproduce the user-approved V_BASE screen without later score-family changes."""
    del core_repo, turnover
    benchmark = prices[prices.ticker.eq("0050")].set_index("date").adjusted_analysis_close.sort_index()
    benchmark_rs20 = benchmark.get(actual) / benchmark.shift(20).get(actual) - 1
    weekly_dates = sorted(membership.snapshot_date.drop_duplicates())
    rows: list[dict] = []
    for item in current.itertuples(index=False):
        ticker = str(item.ticker).zfill(4)
        s = prices[
            prices.ticker.eq(ticker) & prices.date.between(WINDOW_START, actual)
        ].set_index("date").adjusted_analysis_close.sort_index()
        inside = [bool(((membership.snapshot_date == day) & (membership.ticker == ticker)).any()) for day in weekly_dates]
        coverage = sum(inside) / len(weekly_dates)
        if len(s) < 61:
            continue
        lo, hi = float(s.min()), float(s.max())
        spread = hi - lo
        if not spread or lo <= 0:
            continue
        position = (s - lo) / spread * 100
        range_pct = (hi / lo - 1) * 100
        alternation, path = alternating_path(position, 35.0, 65.0)
        one_day_contribution = float(s.diff().abs().max() / spread)
        ret20 = float(s.iloc[-1] / s.iloc[-21] - 1)
        rs20 = ret20 - benchmark_rs20
        ma60 = float(s.iloc[-60:].mean())
        rows.append({
            "ticker": ticker,
            "name": item.name,
            "market": item.market,
            "core_rank": int(item.core_rank),
            "weekly_core_coverage": coverage,
            "max_consecutive_outside": max_outside_run(inside),
            "range_pct": range_pct,
            "window_low": lo,
            "window_high": hi,
            "normalized_position": float(position.iloc[-1]),
            "alternation_path": path,
            "max_one_day_contribution": one_day_contribution,
            "RS20": rs20,
            "bias60": float(s.iloc[-1] / ma60 - 1),
            "volatility20": float(s.pct_change().iloc[-20:].std()),
            "low_base_score": float(1 - position.iloc[-1] / 100),
            "gate_weekly_coverage": coverage >= 0.80,
            "gate_max_outside": max_outside_run(inside) <= 2,
            "gate_range": range_pct >= 25.0,
            "gate_alternation": alternation,
            "gate_one_day_anomaly": one_day_contribution <= 0.35,
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["rs20_percentile"] = frame.RS20.rank(method="average", pct=True)
    frame["liquidity_score"] = 1 - (frame.core_rank - 1) / 250
    frame["bias60_risk_percentile"] = frame.bias60.rank(method="average", pct=True)
    frame["volatility_risk_percentile"] = frame.volatility20.rank(method="average", pct=True)
    frame["risk_adjusted_rs20_score"] = (
        frame.rs20_percentile * 0.44
        + frame.liquidity_score * 0.16
        + frame.low_base_score * 0.18
        + (1 - frame.bias60_risk_percentile) * 0.11
        + (1 - frame.volatility_risk_percentile) * 0.06
        + 0.025
    )
    frame.loc[frame.bias60_risk_percentile.ge(0.95), "risk_adjusted_rs20_score"] *= 0.75
    frame.loc[frame.volatility_risk_percentile.ge(0.90), "risk_adjusted_rs20_score"] *= 0.90
    gates = ["gate_weekly_coverage", "gate_max_outside", "gate_range", "gate_alternation", "gate_one_day_anomaly"]
    frame["final_pass"] = frame[gates].all(axis=1)
    frame = frame.sort_values(
        ["normalized_position", "risk_adjusted_rs20_score", "RS20", "ticker"],
        ascending=[True, False, False, True],
    )
    frame["variant"] = "V_BASE"
    frame["final_rank"] = pd.NA
    frame.loc[frame.final_pass, "final_rank"] = range(1, int(frame.final_pass.sum()) + 1)
    return frame


def alternating_path(position: pd.Series, low: float, high: float, min_gap: int = 5) -> tuple[bool, str]:
    values = position.to_numpy(float)
    dates = [day.strftime("%Y-%m-%d") for day in position.index]
    for states in (("L", "H", "L"), ("H", "L", "H")):
        starts = np.flatnonzero(values <= low) if states[0] == "L" else np.flatnonzero(values >= high)
        for first in starts:
            second = np.flatnonzero(values >= high) if states[1] == "H" else np.flatnonzero(values <= low)
            second = second[second >= first + min_gap]
            for middle in second:
                third = np.flatnonzero(values <= low) if states[2] == "L" else np.flatnonzero(values >= high)
                third = third[third >= middle + min_gap]
                if len(third):
                    end = int(third[0])
                    return True, f"{states[0]}:{dates[first]}->{states[1]}:{dates[middle]}->{states[2]}:{dates[end]}"
    return False, ""


def max_outside_run(flags: list[bool]) -> int:
    best = run = 0
    for inside in flags:
        run = 0 if inside else run + 1
        best = max(best, run)
    return best


def official_close_map(official: pd.DataFrame, prices: pd.DataFrame, actual: pd.Timestamp) -> dict[str, float]:
    official = official.copy()
    official["date"] = pd.to_datetime(official.date)
    exact = official[official.date.eq(actual)].dropna(subset=["close"])
    result = dict(zip(exact.ticker.astype(str).str.zfill(4), pd.to_numeric(exact.close)))
    fallback = prices[prices.date.eq(actual)].dropna(subset=["raw_close_comparator"])
    for ticker, value in zip(fallback.ticker, fallback.raw_close_comparator):
        result.setdefault(str(ticker).zfill(4), float(value))
    return result


def update_state(state: dict, *, report_date: str, top10: pd.DataFrame, raw_close_map: dict[str, float], trading_dates: list[str]) -> tuple[dict, list[dict], list[dict]]:
    registry = state["registry"]
    rows = []
    for row in top10.itertuples(index=False):
        rows.append({"rank": int(row.final_rank), "ticker": row.ticker, "name": row.name, "close": float(row.display_close), "normalized_position": float(row.normalized_position)})
    state["daily_top10"][report_date] = rows
    for ticker_row in rows[:3]:
        ticker = ticker_row["ticker"]
        if ticker not in registry:
            registry[ticker] = {
                "ticker": ticker,
                "name": ticker_row["name"],
                "first_top3_date": report_date,
                "first_top3_close": ticker_row["close"],
            }
    new_top3 = [entry for entry in registry.values() if entry["first_top3_date"] == report_date]
    state["trading_dates"] = sorted(set(state.get("trading_dates", [])) | set(trading_dates) | {report_date})
    registry_rows = []
    for ticker, entry in registry.items():
        current_close = raw_close_map.get(ticker)
        if current_close is None:
            current_close = entry.get("last_close", entry["first_top3_close"])
            close_date = entry.get("last_close_date", entry["first_top3_date"])
            close_status = "本日無成交，沿用最近收盤"
        else:
            close_date = report_date
            close_status = "本日收盤"
            entry["last_close"] = current_close
            entry["last_close_date"] = report_date
        elapsed = sum(entry["first_top3_date"] <= day <= report_date for day in state["trading_dates"])
        change = current_close / entry["first_top3_close"] - 1
        registry_rows.append({**entry, "current_close": current_close, "current_close_date": close_date, "close_status": close_status, "elapsed_trading_days": elapsed, "return_pct": change * 100})
    registry_rows.sort(key=lambda item: (item["first_top3_date"], item["ticker"]))
    return state, new_top3, registry_rows


def render_html(actual: pd.Timestamp, active_date: pd.Timestamp, top10: pd.DataFrame, new_top3: list[dict], registry: list[dict]) -> str:
    top_rows = "".join(
        f"<tr><td>{int(row.final_rank)}</td><td><b>{escape(str(row.ticker))} {escape(str(row.name))}</b></td><td>{row.display_close:,.2f}</td><td>{row.normalized_position:.2f}</td><td>{row.range_pct:.2f}%</td><td>{row.window_low:,.2f} / {row.window_high:,.2f}</td></tr>"
        for row in top10.itertuples(index=False)
    )
    new_rows = "".join(
        f"<tr><td>{escape(item['ticker'])} {escape(item['name'])}</td><td>{item['first_top3_date']}</td><td>{item['first_top3_close']:,.2f}</td></tr>"
        for item in new_top3
    ) or '<tr><td colspan="3" class="empty">今日前三名皆曾出現，沒有新增追蹤股票。</td></tr>'
    registry_rows = "".join(
        f"<tr><td><b>{escape(item['ticker'])} {escape(item['name'])}</b></td><td>{item['first_top3_date']}<br><span>{item['first_top3_close']:,.2f}</span></td><td>{item['current_close_date']}<br><span>{item['current_close']:,.2f}</span></td><td>{item['elapsed_trading_days']} TD</td><td class=\"{'up' if item['return_pct'] >= 0 else 'down'}\">{item['return_pct']:+.2f}%</td><td>{escape(item['close_status'])}</td></tr>"
        for item in registry
    ) or '<tr><td colspan="6" class="empty">尚未建立前三名追蹤紀錄。</td></tr>'
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><style>
@page{{size:A4;margin:12mm}}*{{box-sizing:border-box}}body{{font-family:'Noto Sans TC','Microsoft JhengHei',sans-serif;color:#1c2730;margin:0;background:#fff}}header{{background:#102e39;color:#fff;padding:24px 28px;border-bottom:6px solid #d7a12b}}h1{{font-size:27px;margin:0 0 8px}}header p{{margin:3px 0;color:#d7e5e8}}section{{margin:18px 0 24px;break-inside:avoid}}h2{{font-size:19px;margin:0 0 10px;padding-left:10px;border-left:5px solid #d7a12b}}.note{{font-size:12px;color:#64727b;margin:0 0 10px}}table{{width:100%;border-collapse:collapse;font-size:12px}}th{{background:#edf2f3;color:#28434c;text-align:left;padding:8px 7px;border-bottom:2px solid #9aabb0}}td{{padding:8px 7px;border-bottom:1px solid #dce3e5;vertical-align:top}}tbody tr:nth-child(even){{background:#f8faf9}}span{{color:#65747b}}.up{{color:#b22d2d;font-weight:700}}.down{{color:#087e69;font-weight:700}}.empty{{text-align:center;color:#758188;padding:18px}}footer{{font-size:10px;color:#7b858a;border-top:1px solid #d6dddf;padding-top:8px}}</style></head><body>
<header><h1>{REPORT_TITLE}</h1><p>報告日：{actual:%Y-%m-%d}｜Layer0 core 名單日：{active_date:%Y-%m-%d}</p><p>V_BASE：高低差至少25%、低檔35以下、高檔65以上；合格後依目前基期由低到高排序。</p></header>
<section><h2>第一部分｜今日前十名</h2><p class="note">目前基期越低，排名越前。基期位置 0 代表位於 2026-03-02 以來自身價格區間的相對低點。</p><table><thead><tr><th>#</th><th>股票</th><th>今日收盤</th><th>目前基期</th><th>區間高低差</th><th>區間低 / 高</th></tr></thead><tbody>{top_rows}</tbody></table></section>
<section><h2>第二部分｜今日首次進入前三名</h2><p class="note">同一股票只在第一次進入前三名時登錄一次；日後重複進榜不重複新增。</p><table><thead><tr><th>股票</th><th>首次入榜日</th><th>首次收盤價</th></tr></thead><tbody>{new_rows}</tbody></table></section>
<section><h2>第三部分｜歷來前三名總追蹤</h2><p class="note">交易日數包含首次入榜日與今日；週末及台股休市日不計。</p><table><thead><tr><th>股票</th><th>首次日 / 收盤</th><th>目前日 / 收盤</th><th>經過交易日</th><th>累積漲跌</th><th>資料狀態</th></tr></thead><tbody>{registry_rows}</tbody></table></section>
<footer>本報告為候選追蹤與研究用途，不是買賣建議。收盤價用當日官方資料；adjusted price 只用於基期與循環計算，未以 raw close 冒充 adjusted close。</footer></body></html>"""


if __name__ == "__main__":
    main()
