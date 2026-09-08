"""Materialize and advance the frozen C6 score0 daily research account.

The runner deliberately shares the V4-D official-market loader and liquidity
authority.  It never publishes a prior-day payload as if it were current.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .base_cycle_daily_report import ReportDataNotReady, load_official_prices_and_turnover
from .formal_sources.point_in_time_revenue import (
    FormalSymbol,
    MOPS_MARKET_BY_EXCHANGE,
    build_mops_revenue_url,
    fetch_mops_text,
    parse_mops_revenue_html,
)
from .v4d_dashboard_publish import SheetsClient
from .schedule_gate import fetch_twse_calendar, is_trading_day
from .v4d_top1_signal import (
    LAYER1_SNAPSHOT,
    LIQUIDITY_WARMUP,
    build_liquidity_authority,
    extend_adjusted_with_official_raw,
    next_trading_day,
)


MODEL_VERSION = "c6-research-score0-pit-v2-forward-daily"
C6_ADJUSTED_SEED = Path("data/c6_score0_adjusted_seed_20260903.csv.gz")
C6_TURNOVER_SEED = Path("data/c6_score0_turnover_seed_20260903.csv.gz")
COMMISSION = 0.000855
SLIPPAGE = 0.001
SELL_TAX = 0.003
WITHDRAWAL_AMOUNT = 75_000.0
WITHDRAWAL_START = date(2026, 9, 9)

POOL = {
    "2408": ("南亞科", "記憶體"), "2344": ("華邦電", "記憶體"), "2337": ("旺宏", "記憶體"),
    "6770": ("力積電", "記憶體"), "3260": ("威剛", "記憶體"), "4967": ("十銓", "記憶體"),
    "2451": ("創見", "記憶體"), "8271": ("宇瞻", "記憶體"), "8299": ("群聯", "記憶體"),
    "5351": ("鈺創", "記憶體"), "3006": ("晶豪科", "記憶體"),
    "2327": ("國巨", "被動元件"), "2492": ("華新科", "被動元件"), "3026": ("禾伸堂", "被動元件"),
    "6173": ("信昌電", "被動元件"), "2478": ("大毅", "被動元件"), "2375": ("凱美", "被動元件"),
    "2472": ("立隆電", "被動元件"), "6175": ("立敦", "被動元件"), "6127": ("九豪", "被動元件"),
    "6284": ("佳邦", "被動元件"), "3357": ("臺慶科", "被動元件"), "8043": ("蜜望實", "被動元件"),
    "3532": ("台勝科", "矽晶圓"), "5483": ("中美晶", "矽晶圓"), "6182": ("合晶", "矽晶圓"),
    "6488": ("環球晶", "矽晶圓"),
    "2383": ("台光電", "PCB／CCL／載板"), "6274": ("台燿", "PCB／CCL／載板"),
    "2368": ("金像電", "PCB／CCL／載板"), "3037": ("欣興", "PCB／CCL／載板"),
    "8046": ("南電", "PCB／CCL／載板"), "3044": ("健鼎", "PCB／CCL／載板"),
    "5439": ("高技", "PCB／CCL／載板"), "4958": ("臻鼎-KY", "PCB／CCL／載板"),
    "6669": ("緯穎", "AI伺服器"), "3231": ("緯創", "AI伺服器"), "2382": ("廣達", "AI伺服器"),
    "2317": ("鴻海", "AI伺服器"), "2376": ("技嘉", "AI伺服器"), "2356": ("英業達", "AI伺服器"),
    "3706": ("神達", "AI伺服器"),
    "2308": ("台達電", "AI機櫃電力與散熱"), "2301": ("光寶科", "AI機櫃電力與散熱"),
    "3017": ("奇鋐", "AI機櫃電力與散熱"), "3324": ("雙鴻", "AI機櫃電力與散熱"),
    "3653": ("健策", "AI機櫃電力與散熱"), "2421": ("建準", "AI機櫃電力與散熱"),
}


def _rolling_low_offset(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window, min_periods=window).apply(
        lambda item: float(window - 1 - int(np.argmin(item))), raw=True
    )


def build_daily_features(adjusted: pd.DataFrame, turnover: pd.DataFrame) -> pd.DataFrame:
    price = adjusted.loc[adjusted.ticker.isin(POOL), ["ticker", "date", "adjusted_analysis_close"]].copy()
    price = price.dropna().drop_duplicates(["ticker", "date"], keep="last").sort_values(["ticker", "date"])
    volume = turnover.loc[turnover.ticker.isin(POOL), ["ticker", "date", "turnover_value"]].copy()
    volume = volume.dropna().drop_duplicates(["ticker", "date"], keep="last")
    frame = price.merge(volume, on=["ticker", "date"], how="left", validate="one_to_one")
    parts = []
    for _, group in frame.groupby("ticker", sort=False):
        group = group.sort_values("date").copy()
        close = group.adjusted_analysis_close.astype(float)
        ret = close.pct_change()
        tv = pd.to_numeric(group.turnover_value, errors="coerce")
        group["ret20"] = close / close.shift(20) - 1
        group["return_60d"] = close / close.shift(59) - 1
        group["ma20"] = close.rolling(20, min_periods=20).mean()
        group["ma60"] = close.rolling(60, min_periods=60).mean()
        group["ma20_slope5"] = group.ma20 / group.ma20.shift(5) - 1
        group["ma60_slope5"] = group.ma60 / group.ma60.shift(5) - 1
        group["low20_offset"] = _rolling_low_offset(close, 20)
        group["higher_low"] = close.rolling(5, min_periods=5).min() > close.shift(5).rolling(5, min_periods=5).min()
        group["vol_compression"] = ret.rolling(10, min_periods=10).std(ddof=0) / ret.rolling(60, min_periods=60).std(ddof=0)
        group["breakout40"] = close > close.shift(1).rolling(40, min_periods=40).max()
        up_tv, down_tv = tv.where(ret > 0), tv.where(ret < 0)
        group["up_down_turnover20"] = up_tv.rolling(20, min_periods=8).mean() / down_tv.rolling(20, min_periods=8).mean()
        group["turnover20"] = tv.rolling(20, min_periods=20).mean()
        group["turnover_ratio20"] = group.turnover20 / group.turnover20.shift(20)
        group["down_turnover_contract"] = down_tv.rolling(10, min_periods=3).mean() <= down_tv.shift(10).rolling(20, min_periods=6).mean()
        parts.append(group)
    features = pd.concat(parts, ignore_index=True)
    market = adjusted.loc[adjusted.ticker.eq("0050"), ["date", "adjusted_analysis_close"]].drop_duplicates("date", keep="last").sort_values("date")
    market["market_ret20"] = market.adjusted_analysis_close / market.adjusted_analysis_close.shift(20) - 1
    features = features.merge(market[["date", "market_ret20"]], on="date", how="left")
    features["chain"] = features.ticker.map({key: value[1] for key, value in POOL.items()})
    features["stock_rs20"] = features.ret20 - features.market_ret20
    sector = features.groupby(["date", "chain"], as_index=False).agg(sector_ret20=("ret20", "median"))
    sector = sector.merge(market[["date", "market_ret20"]], on="date", how="left")
    sector["sector_rs20"] = sector.sector_ret20 - sector.market_ret20
    above = features.assign(above_ma20=features.adjusted_analysis_close > features.ma20).groupby(
        ["date", "chain"], as_index=False
    ).above_ma20.mean().rename(columns={"above_ma20": "sector_above_ma20_ratio"})
    features = features.merge(sector[["date", "chain", "sector_rs20"]], on=["date", "chain"], how="left")
    features = features.merge(above, on=["date", "chain"], how="left")
    features["bottom_score"] = (
        features.higher_low.fillna(False).astype(int) * 25 + features.low20_offset.ge(3).astype(int) * 20
        + features.ma20_slope5.ge(0).astype(int) * 20 + features.ma60_slope5.ge(-0.01).astype(int) * 15
        + features.vol_compression.le(0.75).astype(int) * 10
        + features.down_turnover_contract.fillna(False).astype(int) * 10
    ).astype(float)
    features["launch_score"] = (
        features.adjusted_analysis_close.gt(features.ma20).astype(int) * 20
        + features.ma20.gt(features.ma60).astype(int) * 15 + features.ma20_slope5.gt(0).astype(int) * 10
        + features.breakout40.fillna(False).astype(int) * 15 + features.up_down_turnover20.ge(1.2).astype(int) * 15
        + features.turnover_ratio20.ge(1).astype(int) * 10 + features.stock_rs20.gt(0).astype(int) * 10
        + (features.sector_rs20.gt(0) & features.sector_above_ma20_ratio.ge(0.5)).astype(int) * 5
    ).astype(float)
    return features


def _period_before(target: pd.Timestamp, months: int) -> str:
    return (target.replace(day=1) - pd.DateOffset(months=months)).strftime("%Y-%m")


def load_official_0050(start: pd.Timestamp, target: pd.Timestamp, cache_dir: Path, offline: bool = False) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    open_dates, closed_dates = (set(), None) if offline else fetch_twse_calendar()
    rows = []
    for month in pd.period_range(start=start, end=target, freq="M"):
        path = cache_dir / f"0050-{month.strftime('%Y-%m')}.json"
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?date={month.strftime('%Y%m')}01&stockNo=0050&response=json"
        )
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        cached_dates = set()
        for item in payload.get('data', []):
            year, month_number, day = (int(value) for value in item[0].split('/'))
            cached_dates.add(date(year + 1911, month_number, day))
        required_dates = set() if closed_dates is None else {
            day.date() for day in pd.date_range(max(start, month.start_time), min(target, month.end_time))
            if is_trading_day(day.date(), open_dates, closed_dates)}
        cache_complete = bool(required_dates) and required_dates.issubset(cached_dates)
        # A previously open month may have been cached before its last session.
        # Reuse only complete caches; never assume a past month is complete.
        if offline and not path.exists():
            continue
        if not offline and not cache_complete:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for item in payload.get("data", []):
            year, month_number, day = (int(value) for value in item[0].split("/"))
            actual = pd.Timestamp(year + 1911, month_number, day)
            if not (start <= actual <= target):
                continue
            rows.append({
                "date": actual, "ticker": "0050", "name": "元大台灣50", "market": "TWSE",
                "close": float(str(item[6]).replace(",", "")),
                "turnover_value": float(str(item[2]).replace(",", "")),
                "source_url": url, "source_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    frame = pd.DataFrame(rows)
    if frame.empty or not frame.date.eq(target).any():
        raise ReportDataNotReady(f"Official 0050 close is unavailable for {target.date()}")
    return frame


def load_revenue_yoy(target: pd.Timestamp, official: pd.DataFrame, cache_dir: Path) -> tuple[pd.DataFrame, list[str]]:
    # MOPS monthly revenue is conservatively available on day 10 of the next month.
    latest_lag = 1 if target.day >= 10 else 2
    periods = [_period_before(target, latest_lag), _period_before(target, latest_lag + 12)]
    latest, prior = periods
    symbols = {}
    current = official.sort_values("date").drop_duplicates("ticker", keep="last")
    for row in current.loc[current.ticker.isin(POOL)].itertuples(index=False):
        symbols[str(row.ticker)] = FormalSymbol(str(row.ticker), POOL[str(row.ticker)][0], str(row.market))
    rows, hashes = [], []
    cache_dir.mkdir(parents=True, exist_ok=True)
    for period in periods:
        for exchange, market in MOPS_MARKET_BY_EXCHANGE.items():
            for company_type in (0, 1):
                url = build_mops_revenue_url(market=market, period=period, company_type=company_type)
                path = cache_dir / f"{period}-{market}-{company_type}.html"
                text = path.read_text(encoding="utf-8") if path.exists() else fetch_mops_text(url)
                if not path.exists():
                    path.write_text(text, encoding="utf-8")
                hashes.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
                rows.extend(parse_mops_revenue_html(
                    html=text, period=period, exchange=exchange, source_url=url,
                    ingested_at=target.isoformat(), symbol_lookup=symbols,
                ))
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ReportDataNotReady("C6 PIT monthly revenue is unavailable")
    pivot = frame.pivot_table(index="symbol", columns="period", values="metric_value", aggfunc="last").apply(pd.to_numeric)
    pivot["monthly_revenue_yoy"] = pivot[latest] / pivot[prior] - 1
    return pivot[["monthly_revenue_yoy"]].reset_index().rename(columns={"symbol": "ticker"}), hashes


def rank_score0(features: pd.DataFrame, liquidity: pd.DataFrame, allowed: set[str], revenue: pd.DataFrame, target: pd.Timestamp) -> pd.DataFrame:
    candidates = liquidity.loc[
        liquidity.signal_date.eq(target) & liquidity.liquidity_pass & liquidity.ticker.isin(POOL) & liquidity.ticker.isin(allowed)
    ].merge(features.loc[features.date.eq(target)], on=["ticker"], how="left", suffixes=("", "_feature"))
    candidates = candidates.merge(revenue, on="ticker", how="left")
    required = ["return_60d", "bottom_score", "launch_score", "stock_rs20", "sector_rs20", "monthly_revenue_yoy"]
    candidates = candidates.dropna(subset=required).copy()
    if candidates.empty:
        return candidates
    group = candidates.groupby("signal_date", sort=False)
    candidates["launch_pct"] = group.launch_score.rank(pct=True) * 100
    candidates["bottom_pct"] = group.bottom_score.rank(pct=True) * 100
    candidates["stock_rs_pct"] = group.stock_rs20.rank(pct=True) * 100
    candidates["sector_rs_pct"] = group.sector_rs20.rank(pct=True) * 100
    candidates["selection_score"] = (
        candidates.launch_pct * 0.40 + candidates.bottom_pct * 0.25
        + candidates.stock_rs_pct * 0.20 + candidates.sector_rs_pct * 0.15
    )
    candidates = candidates.loc[
        candidates.return_60d.gt(0) & candidates.monthly_revenue_yoy.gt(-0.20)
        & candidates.bottom_score.ge(60) & candidates.launch_score.ge(65)
    ].copy()
    candidates = candidates.sort_values(
        ["selection_score", "launch_score", "sector_rs20", "ticker"], ascending=[False, False, False, True], kind="stable"
    )
    candidates["rank"] = range(1, len(candidates) + 1)
    return candidates


def _second_wednesday(day: date) -> date:
    first = day.replace(day=1)
    return date(day.year, day.month, 1 + ((2 - first.weekday()) % 7) + 7)


def _entry_date(payload: dict, slot_id: int) -> str:
    buys = [row for row in payload.get("ledger_rows", []) if str(row.get("event_type")) == "buy" and int(row.get("slot_id")) == slot_id]
    return str(sorted(buys, key=lambda row: str(row.get("account_date")))[-1].get("account_date")) if buys else ""


def _slot_cash(payload: dict, slot_id: int) -> float:
    rows = [
        row for row in payload.get("ledger_rows", [])
        if int(row.get("slot_id") or 0) == slot_id and row.get("cash_after") not in {"", None}
    ]
    return float(rows[-1]["cash_after"]) if rows else 0.0


def advance_account(
    payload: dict,
    target: pd.Timestamp,
    ranked: pd.DataFrame,
    official: pd.DataFrame,
    adjusted: pd.DataFrame,
) -> tuple[list[dict], list[dict], float, list[str], list[dict]]:
    target_text = target.date().isoformat()
    raw_today = official.loc[pd.to_datetime(official.date).eq(target)].copy()
    raw_map = {str(row.ticker): row for row in raw_today.itertuples(index=False)}
    adj_map = dict(zip(
        adjusted.loc[pd.to_datetime(adjusted.date).eq(target), "ticker"].astype(str),
        adjusted.loc[pd.to_datetime(adjusted.date).eq(target), "adjusted_analysis_close"].astype(float),
    ))
    slots = [dict(slot) for slot in payload.get("slots", [])]
    for slot in slots:
        slot["cash"] = float(slot.get("slot_cash", slot.get("cash", _slot_cash(payload, int(slot["slot_id"])))))
    ledger = [row for row in payload.get("ledger_rows", []) if str(row.get("account_date")) != target_text]
    blockers = []
    pending = [dict(order) for order in payload.get("pending_orders", [])]
    todays_orders = sorted(
        [order for order in pending if str(order.get("execution_date")) == target_text],
        key=lambda order: (0 if order.get("action") == "sell" else 1, int(order.get("slot_id") or 0)),
    )
    pending = [order for order in pending if str(order.get("execution_date")) != target_text]
    for order in todays_orders:
        slot = next(item for item in slots if int(item["slot_id"]) == int(order["slot_id"]))
        ticker = str(order["ticker"]).zfill(4)
        if ticker not in raw_map:
            blockers.append(f"missing_exact_execution_mark:{ticker}:{target_text}")
            continue
        close = float(raw_map[ticker].close)
        if order["action"] == "sell":
            shares = int(slot["shares"])
            price = close * (1 - SLIPPAGE)
            gross = shares * price
            cost = gross * (COMMISSION + SELL_TAX)
            net = gross - cost
            slot["cash"] += net
            ledger.append({
                "account_date": target_text, "event_sequence": 1, "slot_id": int(slot["slot_id"]),
                "event_type": "sell", "ticker": ticker, "shares": shares, "raw_close": close,
                "gross_amount": gross, "transaction_cost": cost, "net_amount": net, "cash_after": slot["cash"],
                "relative_return_pct": net / float(slot["position_cost"]) - 1, "reason": order["reason"],
                "signal_date": order.get("signal_date", ""),
            })
            slot.update({"ticker": "", "shares": 0, "position_cost": 0.0, "raw_close": 0.0})
        else:
            if ticker not in adj_map:
                blockers.append(f"missing_adjusted_execution_mark:{ticker}:{target_text}")
                continue
            price = close * (1 + SLIPPAGE)
            shares = math.floor(slot["cash"] / (price * (1 + COMMISSION)))
            if shares <= 0:
                blockers.append(f"insufficient_slot_cash:{slot['slot_id']}:{ticker}")
                continue
            gross = shares * price
            cost = gross * COMMISSION
            total = gross + cost
            slot["cash"] -= total
            slot.update({"ticker": ticker, "shares": shares, "position_cost": total, "raw_close": close})
            ledger.append({
                "account_date": target_text, "event_sequence": 1, "slot_id": int(slot["slot_id"]),
                "event_type": "buy", "ticker": ticker, "shares": shares, "raw_close": close,
                "gross_amount": gross, "transaction_cost": cost, "net_amount": -total, "cash_after": slot["cash"],
                "relative_return_pct": "", "reason": "ai_bottom_launch_rank1",
                "signal_date": order.get("signal_date", ""),
            })

    scheduled_sells = []
    for slot in slots:
        ticker = str(slot.get("ticker") or "").zfill(4)
        if ticker not in raw_map or ticker not in adj_map:
            blockers.append(f"missing_exact_holding_mark:{ticker}:{target_text}")
            continue
        entry = _entry_date(payload, int(slot["slot_id"]))
        if not entry:
            blockers.append(f"missing_entry_date:{ticker}")
            continue
        entry_adjusted_rows = adjusted.loc[(adjusted.ticker.eq(ticker)) & (pd.to_datetime(adjusted.date).eq(pd.Timestamp(entry)))]
        if entry_adjusted_rows.empty:
            blockers.append(f"missing_adjusted_entry:{ticker}:{entry}")
            continue
        entry_adjusted = float(entry_adjusted_rows.iloc[-1].adjusted_analysis_close) * (1 + SLIPPAGE)
        current_return = float(adj_map[ticker]) / entry_adjusted - 1
        history = adjusted.loc[
            adjusted.ticker.eq(ticker) & pd.to_datetime(adjusted.date).between(pd.Timestamp(entry), target), "adjusted_analysis_close"
        ].astype(float)
        peak_return = history.max() / entry_adjusted - 1
        td = int(len(history))
        reason = ""
        if current_return <= -0.12:
            reason = "hard_loss_guard"
        elif peak_return >= 0.20 and (1 + current_return) / (1 + peak_return) - 1 <= -0.12:
            reason = "activated_peak_drawdown"
        elif td >= 35 and current_return < 0:
            reason = "first_growth_review_failed"
        elif td >= 50 and current_return < 0.08:
            reason = "second_growth_review_failed"
        elif td >= 60:
            reason = "maximum_holding_td"
        # Macro exit is irrelevant unless profit >=15% and the date is within
        # three trading sessions of settlement.  That bounded authority is
        # intentionally not guessed here.
        if current_return >= 0.15 and 0 <= (_third_wednesday(target) - target).days <= 5:
            blockers.append(f"macro_triple_authority_required:{target_text}:{ticker}")
        close = float(raw_map[ticker].close)
        slot["raw_close"] = close
        ledger.append({
            "account_date": target_text, "event_sequence": int(slot["slot_id"]), "slot_id": int(slot["slot_id"]),
            "event_type": "daily_mark", "ticker": ticker, "shares": int(slot["shares"]), "raw_close": close,
            "gross_amount": int(slot["shares"]) * close, "transaction_cost": 0.0, "net_amount": 0.0,
            "cash_after": "", "relative_return_pct": int(slot["shares"]) * close / float(slot["position_cost"]) - 1,
            "reason": reason or "official_raw_holding_mark", "signal_date": "",
        })
        if reason:
            scheduled_sells.append(int(slot["slot_id"]))
            pending.append({
                "action": "sell", "slot_id": int(slot["slot_id"]), "ticker": ticker, "reason": reason,
                "signal_date": target_text, "execution_date": next_trading_day(target.date()).isoformat(),
            })

    vacancies = sorted(
        {int(slot["slot_id"]) for slot in slots if not slot.get("ticker")} | set(scheduled_sells)
    )
    held = {str(slot.get("ticker")) for slot in slots if slot.get("ticker")}
    choices = ranked.loc[~ranked.ticker.astype(str).isin(held)].head(len(vacancies)) if not ranked.empty else ranked
    execution_date = next_trading_day(target.date()).isoformat()
    for slot_id, row in zip(vacancies, choices.itertuples(index=False)):
        pending.append({
            "action": "buy", "slot_id": slot_id, "ticker": str(row.ticker), "reason": "ai_bottom_launch_rank1",
            "signal_date": target_text, "execution_date": execution_date,
        })

    due = _second_wednesday(target.date())
    already_withdrawn = any(
        str(row.get("event_type")) == "withdrawal" and str(row.get("account_date", ""))[:7] == target_text[:7]
        for row in ledger
    )
    if target.date() >= max(due, WITHDRAWAL_START) and not already_withdrawn:
        total_cash = sum(float(slot["cash"]) for slot in slots)
        if total_cash < WITHDRAWAL_AMOUNT:
            occupied = [slot for slot in slots if slot.get("ticker")]
            if not occupied:
                blockers.append(f"withdrawal_cash_shortfall:{target_text}")
            else:
                worst = min(
                    occupied,
                    key=lambda item: (
                        int(item["shares"]) * float(raw_map[str(item["ticker"])].close) / float(item["position_cost"]) - 1,
                        int(item["slot_id"]),
                    ),
                )
                close = float(raw_map[str(worst["ticker"])].close)
                needed = WITHDRAWAL_AMOUNT - total_cash
                candidates = {
                    max(1, min(int(worst["shares"]), int(round(needed / close)) + delta))
                    for delta in (-1, 0, 1)
                }
                shares = min(candidates, key=lambda qty: abs(qty * close - needed))
                price = close * (1 - SLIPPAGE)
                gross = shares * price
                cost = gross * (COMMISSION + SELL_TAX)
                worst["shares"] = int(worst["shares"]) - shares
                worst["cash"] += gross - cost
                ledger.append({
                    "account_date": target_text, "event_sequence": 90, "slot_id": int(worst["slot_id"]),
                    "event_type": "withdrawal_sale", "ticker": str(worst["ticker"]), "shares": shares,
                    "raw_close": close, "gross_amount": gross, "transaction_cost": cost, "net_amount": gross - cost,
                    "cash_after": worst["cash"], "relative_return_pct": "", "reason": "scheduled_withdrawal_funding",
                    "signal_date": target_text,
                })
        remaining = WITHDRAWAL_AMOUNT
        for slot in sorted(slots, key=lambda item: int(item["slot_id"])):
            take = min(float(slot["cash"]), remaining)
            slot["cash"] -= take
            remaining -= take
            if remaining <= 1e-6:
                break
        if remaining > 1e-6:
            blockers.append(f"withdrawal_net_cash_shortfall:{target_text}:{remaining:.2f}")
        else:
            ledger.append({
                "account_date": target_text, "event_sequence": 99, "slot_id": 0, "event_type": "withdrawal",
                "ticker": "", "shares": 0, "raw_close": 0.0, "gross_amount": WITHDRAWAL_AMOUNT,
                "transaction_cost": 0.0, "net_amount": -WITHDRAWAL_AMOUNT,
                "cash_after": sum(float(slot["cash"]) for slot in slots), "relative_return_pct": "",
                "reason": "monthly_second_wednesday_withdrawal", "signal_date": target_text,
            })
    cash = sum(float(slot["cash"]) for slot in slots)
    for slot in slots:
        slot["slot_cash"] = float(slot.pop("cash"))
    return slots, ledger, cash, blockers, pending


def _third_wednesday(target: pd.Timestamp) -> pd.Timestamp:
    first = target.replace(day=1)
    return first + pd.offsets.WeekOfMonth(week=2, weekday=2)


def actual_history_payload(official: pd.DataFrame, target: pd.Timestamp) -> dict:
    """Reuse downloaded raw history; do not infer actual fills or event coverage."""
    start = pd.Timestamp('2026-07-01')
    open_dates, closed_dates = fetch_twse_calendar()
    sessions = [] if closed_dates is None else [
        day.date().isoformat() for day in pd.date_range(start, target)
        if is_trading_day(day.date(), open_dates, closed_dates)]
    benchmark_dates = set(official.loc[official.ticker.eq('0050'), 'date'].dt.strftime('%Y-%m-%d'))
    complete = bool(sessions) and set(sessions).issubset(benchmark_dates)
    rows = [
        {'ticker': str(row.ticker), 'date': row.date.date().isoformat(),
         'close': float(row.close), 'source_hash': str(getattr(row, 'source_hash', ''))}
        for row in official.loc[official.date.between(start, target) & official.ticker.isin(POOL)]
        .itertuples(index=False) if pd.notna(row.close)]
    return {'start': start.date().isoformat(), 'end': target.date().isoformat(),
            'calendar_complete': complete, 'trading_dates': sessions, 'official_rows': rows,
            'calendar_loaded': closed_dates is not None,
            'missing_session_dates': sorted(set(sessions) - benchmark_dates),
            'event_basis_ready': False}


def build_daily_payload(*, target: pd.Timestamp, source_repo: Path, source_cache: Path, prior_payload: Path, output: Path, offline: bool = False) -> dict:
    prior = json.loads(prior_payload.read_text(encoding="utf-8"))
    official, turnover = load_official_prices_and_turnover(
        source_repo=source_repo, target=target, current=pd.DataFrame(columns=["ticker", "name", "market"]),
        source_cache=source_cache, offline=offline,
    )
    official_0050 = load_official_0050(pd.Timestamp("2026-07-01"), target, source_cache / "c6_0050", offline)
    official = pd.concat([official, official_0050], ignore_index=True, sort=False).drop_duplicates(
        ["date", "ticker"], keep="last"
    )
    turnover = pd.concat([
        turnover,
        official_0050[["date", "ticker", "name", "market", "turnover_value"]],
    ], ignore_index=True, sort=False).drop_duplicates(["date", "ticker"], keep="last")
    warm_raw = pd.read_csv(LIQUIDITY_WARMUP, dtype={"ticker": str})
    warm_raw["date"] = pd.to_datetime(warm_raw.date)
    turnover["date"] = pd.to_datetime(turnover.date)
    turnover["ticker"] = turnover.ticker.astype(str).str.zfill(4)
    liquidity = build_liquidity_authority(pd.concat([
        warm_raw[["date", "ticker", "name", "market", "turnover_value"]], turnover
    ], ignore_index=True).drop_duplicates(["date", "ticker"], keep="last"))
    warm_adjusted = pd.read_csv(C6_ADJUSTED_SEED, dtype={"ticker": str}).rename(columns={"adjusted_close": "adjusted_analysis_close"})
    warm_adjusted["date"] = pd.to_datetime(warm_adjusted.date)
    official["date"] = pd.to_datetime(official.date)
    official["ticker"] = official.ticker.astype(str).str.zfill(4)
    historical_raw = pd.concat([
        warm_raw[["date", "ticker", "raw_close"]].dropna(),
        official.rename(columns={"close": "raw_close"})[["date", "ticker", "raw_close"]],
        official_0050.rename(columns={"close": "raw_close"})[["date", "ticker", "raw_close"]],
    ], ignore_index=True).drop_duplicates(["date", "ticker"], keep="last")
    extension = extend_adjusted_with_official_raw(warm_adjusted, historical_raw, official)
    # The seed is the frozen score0 adjusted-price authority.  Preserve every
    # seeded observation and append only genuinely newer official sessions.
    # Re-solving historical factors here changes the frozen model's features.
    seed_max = warm_adjusted.groupby("ticker").date.max()
    extension = extension.loc[
        [row.date > seed_max.get(str(row.ticker), pd.Timestamp.min) for row in extension.itertuples(index=False)]
    ]
    adjusted = pd.concat([warm_adjusted, extension], ignore_index=True, sort=False).drop_duplicates(
        ["date", "ticker"], keep="last"
    )
    feature_turnover = pd.read_csv(C6_TURNOVER_SEED, dtype={"ticker": str})
    feature_turnover["date"] = pd.to_datetime(feature_turnover.date)
    feature_turnover["ticker"] = feature_turnover.ticker.astype(str).str.zfill(4)
    turnover_max = feature_turnover.groupby("ticker").date.max()
    turnover_extension = turnover.loc[
        [row.date > turnover_max.get(str(row.ticker), pd.Timestamp.min) for row in turnover.itertuples(index=False)]
    ]
    feature_turnover = pd.concat([feature_turnover, turnover_extension], ignore_index=True, sort=False).drop_duplicates(
        ["date", "ticker"], keep="last"
    )
    features = build_daily_features(adjusted, feature_turnover)
    layer1 = pd.read_csv(LAYER1_SNAPSHOT, dtype={"ticker": str})
    allowed = set(layer1.loc[layer1.layer1_pass.astype(bool), "ticker"].astype(str).str.zfill(4))
    revenue, revenue_hashes = load_revenue_yoy(target, official, source_cache / "c6_mops_revenue")
    ranked = rank_score0(features, liquidity, allowed, revenue, target)
    top3 = ranked.head(3)
    snapshot_rows = [row for row in prior.get("snapshot_rows", []) if str(row.get("signal_date")) != target.date().isoformat()]
    if top3.empty:
        snapshot_rows.append({
            "signal_date": target.date().isoformat(), "rank": 0, "ticker": "", "name": "", "market": "",
            "candidate_status": "frozen_engine_explicit_no_eligible_candidate", "eligibility_reason": "no_score0_candidate",
            "score": "", "display_close": "", "planned_execution_date": "",
            "source_readiness": "accepted_explicit_no_eligible_candidate", "source_label": "daily_c6_score0_pipeline",
        })
    else:
        current_market = official.loc[official.date.eq(target)].drop_duplicates("ticker", keep="last").set_index("ticker")
        for row in top3.itertuples(index=False):
            raw = current_market.loc[str(row.ticker)]
            snapshot_rows.append({
                "signal_date": target.date().isoformat(), "rank": int(row.rank), "ticker": str(row.ticker),
                "name": POOL[str(row.ticker)][0], "market": str(raw.market),
                "candidate_status": "frozen_C6_eligibility_passed_and_ranked",
                "eligibility_reason": "frozen_C6_eligibility_passed_and_ranked", "score": float(row.selection_score),
                "bottom_score": float(row.bottom_score), "launch_score": float(row.launch_score),
                "stock_rs20": float(row.stock_rs20), "sector_rs20": float(row.sector_rs20),
                "return_60d": float(row.return_60d), "monthly_revenue_yoy": float(row.monthly_revenue_yoy),
                "display_close": float(raw.close), "planned_execution_date": next_trading_day(target.date()).isoformat(),
                "source_readiness": "accepted_research_ranking_with_official_raw_display_close",
                "source_label": "daily_C6_SCORE_0_ranking",
            })
    slots, ledger, cash, blockers, pending = advance_account(prior, target, top3, official, adjusted)
    if blockers:
        raise ReportDataNotReady(";".join(blockers))
    official_hashes = sorted(set(str(value) for value in official.get("source_hash", pd.Series(dtype=str)).dropna()))
    source_material = [
        target.date().isoformat(), hashlib.sha256(C6_ADJUSTED_SEED.read_bytes()).hexdigest(),
        hashlib.sha256(C6_TURNOVER_SEED.read_bytes()).hexdigest(), *official_hashes, *sorted(revenue_hashes),
    ]
    source_hash = hashlib.sha256("|".join(source_material).encode()).hexdigest()
    payload = {
        "model_version": MODEL_VERSION, "snapshot_as_of": target.date().isoformat(),
        "data_status": "partial_whole_share_replay_event_coverage_pending", "source_manifest_hash": source_hash,
        "snapshot_rows": snapshot_rows, "ledger_rows": ledger, "slots": slots, "cash": cash,
        "pending_orders": pending,
        "actual_holding_history": actual_history_payload(official, target),
        "market_rows": [
            {"date": target.date().isoformat(), "ticker": str(row.ticker),
             "close": float(row.close), "source_hash": str(getattr(row, "source_hash", ""))}
            for row in official.loc[official.date.eq(target) & official.ticker.isin(POOL)]
            .drop_duplicates("ticker", keep="last").itertuples(index=False)
            if pd.notna(row.close)
        ],
        "notes": f"C6固定score0排名與三槽官方收盤估值更新至{target.date().isoformat()}；公司行動完整權威清冊仍待確認。",
        "coverage": {"ranking_snapshot_as_of": target.date().isoformat(), "accounting_snapshot_as_of": target.date().isoformat(),
                     "ranking_rows": len(snapshot_rows), "ledger_rows": len(ledger), "future_data_violation_count": 0},
        "formal_model_changed": False, "trade_decision_changed": True, "active_in_trade_decision": True,
        "report_changed": True, "not_live_rule": True,
        "ranking_snapshot_as_of": target.date().isoformat(), "accounting_snapshot_as_of": target.date().isoformat(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def verify_dashboard(spreadsheet_id: str, expected_date: str) -> None:
    client = SheetsClient(spreadsheet_id)
    values = client.get("'C6 Dashboard'!A1:D32")
    flat = "\n".join(str(cell) for row in values for cell in row)
    if expected_date not in flat or "C6研究版" not in flat:
        raise RuntimeError(f"C6 Dashboard read-back failed for {expected_date}")
    if len(values) < 32 or not values[31] or len(str(values[31][0])) < 500:
        raise RuntimeError("C6 Dashboard model logic at A32:B32 is missing or truncated")
    ledger = client.get("'C6模擬交易紀錄'!A1:R200")
    ledger_dates = [str(row[0]) for row in ledger[1:] if row]
    if expected_date not in ledger_dates:
        raise RuntimeError(f"C6 ledger read-back has no {expected_date} row")
    top = [row[:2] for row in values[5:8] if len(row) >= 2]
    slots = [row[:2] for row in values[10:14] if len(row) >= 2]
    latest_ledger = [row for row in ledger[1:] if row and str(row[0]) == expected_date]
    print(json.dumps({
        "dashboard_date": expected_date, "top_rows": top, "slot_rows": slots,
        "model_logic_chars": len(str(values[31][0])), "latest_ledger_rows": len(latest_ledger),
        "latest_ledger_reasons": [str(row[17]) for row in latest_ledger if len(row) > 17],
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--source-repo", default=".")
    parser.add_argument("--source-cache", default="data/current_base_cycle_source_cache")
    parser.add_argument("--prior-payload", default="data/c6_current_snapshot.json")
    parser.add_argument("--output", default="data/c6_current_snapshot.json")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        payload = build_daily_payload(
            target=pd.Timestamp(args.date), source_repo=Path(args.source_repo), source_cache=Path(args.source_cache),
            prior_payload=Path(args.prior_payload), output=Path(args.output), offline=args.offline,
        )
    except ReportDataNotReady as exc:
        print(f"C6_DATA_NOT_READY: {exc}")
        raise SystemExit(75) from exc
    print(json.dumps(payload["coverage"], ensure_ascii=False))


if __name__ == "__main__":
    main()
