"""Append-only Google Sheets publishing primitives for the C6 research account.

This module intentionally does not calculate C6 signals or fabricate a replay.
It accepts source-materialized daily snapshots and account events, persists each
version immutably, and lets the dashboard point at one selected current version.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .v4d_dashboard_publish import SheetsClient
from .v4d_simulation_account import SELL_RATE, second_wednesday


DASHBOARD_SHEET = "C6 Dashboard"
SNAPSHOT_SHEET = "C6每日訊號資料庫"
LEDGER_SHEET = "C6模擬交易紀錄"
from .c6_account_basis import INITIAL_CAPITAL as C6_INITIAL_CAPITAL
C6_SLOT_COUNT = 3
C6_WITHDRAWAL_AMOUNT = 75_000.0
C6_FORWARD_START_DATE = "2026-08-05"
C6_WITHDRAWAL_START_DATE = "2026-09-09"
MODEL_LOGIC = """C6研究版｜AI硬體六大瓶頸與載體清冊 Bottom／Launch 三槽策略

【候選與排序】
選股池限定AI硬體六大瓶頸與載體清冊共48檔，涵蓋記憶體、被動元件、矽晶圓、PCB／CCL／載板、AI伺服器、AI機櫃電力與散熱六鏈。先通過V4-D同級的成交金額與PIT基本面重大風險篩選，並要求60日自身報酬大於0、月營收資料已公布且年增率不低於-20%。Bottom Score至少60分，確認跌勢收斂；Launch Score至少65分，確認價格、量能與相對強度開始發動。歷史題材資格若尚未具備當時可得的完整PIT證據，必須保留survivorship warning。

合格股票以綜合分數排序：Launch當日百分位40%＋Bottom當日百分位25%＋個股相對大盤20日強度百分位20%＋所屬供應鏈相對大盤20日強度百分位15%。同分時依Launch Score、族群相對強度及股票代號排序。每日列出Top1至Top3；分數是當日合格股票之間的相對排名，不代表預測報酬率。

【買進】
初始資金7,676,961.04元，平均分成三個獨立槽位。空槽存在時，收盤後從當日排名中選擇尚未持有的最高順位股票，下一交易日依正式成交口徑買進；同一股票不可重複占用兩槽。三槽全滿時不因每日Top1至Top3改變而換股；持續Top1不代表每天加碼。

【賣出：收盤確認，下一交易日執行】
1. 當下報酬跌至-12%以下，退出。
2. 最高報酬曾達+20%後，從持有高點回落12%，退出。
3. 持有滿35TD仍未轉為正報酬，退出。
4. 持有滿50TD仍未達+8%，退出。
5. 最長持有60TD，退出。
6. 持股已有至少+15%獲利，且美元位於近20TD高檔、0050 BIAS60位於近20TD高檔、距台指期結算日0至3TD三項同時成立，退出。

【成本、資金與提領】
買進使用官方未調整成交價並加入0.10%滑價及0.0855%手續費；賣出加入0.10%滑價、0.0855%手續費及0.30%證交稅。只能買整股，未使用資金保留為現金。自2026-09-09起，每月第二個星期三提領75,000元；排定日休市則順延至下一交易日。現金不足時，從三槽中相對買進成本報酬最低者賣出最接近75,000元現值的整股。

【模型定位】
C6是研究版，不取代正式V4-D。Dashboard會隨目前C6版本同步更新；每日訊號、模擬持股與損益以畫面標示的排名日期及正式帳本日期為準。"""
DEFAULT_HISTORICAL_BENCHMARK_PATH = Path("data/c6_historical_64_benchmark.json")

SNAPSHOT_HEADERS = [
    "model_version", "snapshot_as_of", "data_status", "signal_date", "rank", "ticker", "name",
    "market", "candidate_status", "source_manifest_hash", "immutable_snapshot_key",
]
PUBLIC_SNAPSHOT_HEADERS = [
    "訊號日期", "順位", "股票代號", "股票名稱", "官方收盤價", "C6分數", "合格/不合格原因", "預定執行日", "資料狀態",
]
LEDGER_HEADERS = [
    "模型版本", "帳本資料截至", "帳務日期", "當日事件順序", "槽位", "事件",
    "股票代號", "股數", "官方收盤價", "成交或市值金額", "交易成本", "現金增減", "事件後現金",
    "相對買進成本報酬", "原因", "事件識別碼",
]
VERSION_HEADERS = [
    "版本名稱", "資料日期", "目前可用程度", "資料驗證碼", "是否顯示為目前版本",
    "建立日期", "備註",
]
CURRENT_POINTER_HEADERS = ["目前版本", "正式帳本日期", "目前狀態", "最後更新"]
LEDGER_FIELDS = [
    "model_version", "snapshot_as_of", "account_date", "event_sequence", "slot_id", "event_type",
    "ticker", "shares", "raw_close", "gross_amount", "transaction_cost", "net_amount", "cash_after",
    "relative_return_pct", "reason", "immutable_event_key",
]
TRADE_HEADERS = [
    "日期", "槽位", "事件類型", "股票代號", "股票名稱", "動作", "成交／收盤價", "股數",
    "交易／市值金額", "交易成本", "現金餘額", "已實現損益", "已實現報酬", "訊號日期",
    "持有TD", "當日漲跌", "累積報酬", "原因／狀態",
]
VERSION_FIELDS = [
    "model_version", "snapshot_as_of", "data_status", "source_manifest_hash", "published_as_current",
    "created_at", "notes",
]


def _key(row: dict, fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(field, "")) for field in fields)


def _append_only(existing: list[list[object]], headers: list[str], rows: list[dict], fields: tuple[str, ...]) -> list[list[object]]:
    if existing and existing[0] != headers:
        raise ValueError("Existing C6 sheet schema does not match the append-only publisher contract.")
    field_indexes = [headers.index(field) for field in fields]
    known = {
        tuple(str(row[index]) if index < len(row) else "" for index in field_indexes)
        for row in existing[1:]
    }
    additions: list[list[object]] = []
    for row in rows:
        key = _key(row, fields)
        values = [row.get(header, "") for header in headers]
        if key in known:
            continue
        additions.append(values)
        known.add(key)
    return additions


def _human_data_status(data_status: str) -> str:
    if "event_coverage_pending" in data_status:
        return "三槽帳本已更新至最新交易日；公司行動覆蓋待確認"
    if "whole_share_replay_pit_blocked" in data_status:
        return "整股帳本已建立至目前權威日期；後續交易判斷仍待補齊"
    if "no_whole_share_replay" in data_status or "replay_not_materialized" in data_status:
        return "候選排名已完成；三槽模擬帳戶仍在完整重算"
    if data_status in {"complete", "ready", "formal_ready"}:
        return "資料完整"
    return "研究資料更新中"


def _human_rank_reason(rank: int) -> str:
    if rank == 0:
        return "當日沒有股票通過C6買進條件"
    return f"通過C6條件，列入當日Top{rank}"


def _human_source_status(rank: int) -> str:
    return "排名資料與官方收盤價完整" if rank else "當日候選結果已確認"


def _human_event(event_type: str) -> str:
    return {"buy": "買進", "sell": "賣出", "daily_mark": "每日收盤估值", "withdrawal": "每月提領"}.get(event_type, event_type)


def _human_model_version(model_version: str) -> str:
    if "forward-known-segment" in model_version:
        return "C6三槽整股模擬"
    if model_version.endswith("-v2"):
        return "新版排名"
    return "初版排名"


def _human_version_status(data_status: str) -> str:
    if "event_coverage_pending" in data_status:
        return "整股帳本與每日估值已更新；公司行動覆蓋待確認"
    if "whole_share_replay_pit_blocked" in data_status:
        return "整股帳本已核對至權威日期；後續交易尚未完成"
    if "no_whole_share_replay" in data_status or "replay_not_materialized" in data_status:
        return "只有候選排名，尚未建立整股交易帳本"
    return "資料完整"


def _money(value: float) -> str:
    return f"NT${value:,.2f}"


def _human_candidate_status(row: dict) -> str:
    status = str(row.get("candidate_status") or "")
    if status:
        return "已列入當日排名"
    return "排名資料已完成"


def _execution_date(signal_date: str) -> str:
    if not signal_date:
        return ""
    return (pd.Timestamp(signal_date) + pd.offsets.BDay(1)).date().isoformat()


def build_public_snapshot_values(snapshot_rows: list[dict]) -> list[list[object]]:
    """Return one current, de-duplicated, human-readable Top1~3 table."""
    unique: dict[tuple[str, int], dict] = {}
    for row in snapshot_rows:
        signal_date = str(row.get("signal_date") or "")
        rank = int(row.get("rank") or 0)
        if signal_date and rank in {0, 1, 2, 3}:
            unique[(signal_date, rank)] = row
    values = [PUBLIC_SNAPSHOT_HEADERS]
    for (signal_date, rank), row in sorted(unique.items()):
        if rank == 0:
            values.append([
                signal_date,
                "無候選" if "no_eligible" in str(row.get("candidate_status")) else "PIT待補",
                "", "", "", "", _human_rank_reason(0),
                str(row.get("planned_execution_date") or ""), _human_source_status(0),
            ])
            continue
        score = row.get("score", row.get("c6_score", row.get("selection_score", "")))
        values.append([
            signal_date,
            rank,
            str(row.get("ticker") or ""),
            str(row.get("name") or ""),
            row.get("display_close", row.get("raw_close", row.get("close", ""))),
            score,
            _human_rank_reason(rank),
            str(row.get("planned_execution_date") or _execution_date(signal_date)),
            _human_source_status(rank),
        ])
    return values


def build_trade_record_values(ledger_rows: list[dict], snapshot_rows: list[dict]) -> list[list[object]]:
    """Convert the latest account ledger into one readable, de-duplicated trade history."""
    names = {str(row.get("ticker") or ""): str(row.get("name") or "") for row in snapshot_rows}
    signal_by_execution = {
        (str(row.get("planned_execution_date") or _execution_date(str(row.get("signal_date") or ""))), str(row.get("ticker") or "")):
        str(row.get("signal_date") or "")
        for row in snapshot_rows if int(row.get("rank") or 0) > 0
    }
    latest: dict[tuple[str, int], dict] = {}
    for row in ledger_rows:
        date = str(row.get("account_date") or "")
        sequence = int(row.get("event_sequence") or 0)
        if date:
            latest[(date, sequence)] = row
    position_cost: dict[int, float] = {}
    entry_date: dict[int, str] = {}
    previous_mark: dict[int, float] = {}
    td_by_slot: dict[int, int] = {}
    output = [TRADE_HEADERS]
    for (_, _), row in sorted(latest.items()):
        date = str(row.get("account_date") or "")
        slot = int(row.get("slot_id") or 0)
        ticker = str(row.get("ticker") or "")
        event = str(row.get("event_type") or "")
        shares = int(float(row.get("shares") or 0))
        close = float(row.get("raw_close") or 0)
        gross = float(row.get("gross_amount") or 0)
        cost = float(row.get("transaction_cost") or 0)
        relative = row.get("relative_return_pct")
        signal_date = signal_by_execution.get((date, ticker), "") if event == "buy" else ""
        realized_pnl: object = ""
        realized_return: object = ""
        daily_return: object = ""
        if event == "buy":
            position_cost[slot] = gross + cost
            entry_date[slot] = date
            td_by_slot[slot] = 0
            previous_mark[slot] = close
            action = "買進"
        elif event in {"sell", "withdrawal_sale"}:
            net = float(row.get("net_amount") or 0)
            basis = float(row.get("allocated_cost", position_cost.get(slot, 0)))
            realized_pnl = net - basis if basis else ""
            realized_return = realized_pnl / basis if basis else ""
            position_cost[slot] = max(0, position_cost.get(slot, 0) - basis)
            action = "提領賣股" if event == "withdrawal_sale" else "賣出"
        elif event == "withdrawal":
            action = "每月提領"
        else:
            td_by_slot[slot] = td_by_slot.get(slot, 0) + 1
            prior = previous_mark.get(slot)
            daily_return = close / prior - 1 if prior else ""
            previous_mark[slot] = close
            action = "續抱"
        raw_reason = str(row.get("reason") or "")
        if event == "buy":
            reason = f"依{signal_date}收盤C6 Top1訊號，下一交易日建立第{slot}槽部位"
        elif event == "daily_mark":
            reason = (
                f"{entry_date.get(slot, '')}買入，已持有{td_by_slot.get(slot, 0)} TD；"
                "續抱中，未觸發賣出條件"
            )
        elif event == "sell":
            reason = {
                "macro_high_zone_exit": "高檔環境退出條件成立，下一交易日賣出",
                "max_holding_exit": "達最長持有期限，下一交易日賣出",
            }.get(raw_reason, f"賣出條件成立：{raw_reason}" if raw_reason else "賣出條件成立")
        elif event == "withdrawal_sale":
            reason = "提領資金賣股；已按賣出股數比例扣除成本，提領不當作虧損"
        elif event == "withdrawal":
            reason = "每月第二個星期三依規則提領約75,000元"
        else:
            reason = raw_reason
        output.append([
            date, slot, "成交" if event in {"buy", "sell", "withdrawal_sale"} else "每日持有", ticker, names.get(ticker, ""),
            action, close, shares, gross, cost, row.get("cash_after", ""), realized_pnl, realized_return,
            signal_date, td_by_slot.get(slot, "") if event == "daily_mark" else "", daily_return,
            float(relative) if relative not in {None, ""} else "", reason,
        ])
    return output


def select_withdrawal_slot(
    slots: list[dict], *, cash: float = 0.0, target_amount: float = C6_WITHDRAWAL_AMOUNT,
) -> dict:
    """Choose the lowest mark-vs-cost slot and estimate an exact whole-share sale."""
    if cash >= target_amount:
        return {
            "status": "cash_withdrawal", "slot_id": None, "planned_shares": 0,
            "gross_amount": target_amount, "transaction_cost": 0.0, "net_amount": target_amount,
            "relative_return_pct": None,
        }
    eligible = [
        slot for slot in slots
        if int(slot.get("shares") or 0) > 0 and float(slot.get("raw_close") or 0) > 0
        and float(slot.get("position_cost") or 0) > 0
    ]
    if not eligible:
        return {
            "status": "cash_or_flat", "slot_id": None, "planned_shares": 0,
            "gross_amount": 0.0, "transaction_cost": 0.0, "net_amount": 0.0,
            "relative_return_pct": None,
        }
    def relative_return(slot: dict) -> float:
        marked = float(slot["raw_close"]) * int(slot["shares"])
        return (marked - float(slot["position_cost"])) / float(slot["position_cost"])
    selected = min(eligible, key=lambda slot: (relative_return(slot), str(slot.get("slot_id", ""))))
    stock_target = target_amount
    shares = min(int(selected["shares"]), max(1, round(stock_target / float(selected["raw_close"]))))
    gross = shares * float(selected["raw_close"])
    cost = gross * (1 - (1 - .001) * (1 - .000855 - .003))
    return {
        "status": "planned_stock_sale",
        "slot_id": selected.get("slot_id"),
        "ticker": selected.get("ticker"),
        "planned_shares": shares,
        "gross_amount": gross,
        "transaction_cost": cost,
        "net_amount": gross - cost,
        "cash_withdrawal_amount": min(cash, target_amount),
        "relative_return_pct": relative_return(selected),
    }


def _next_withdrawal_dates(as_of: str, *, count: int = 2) -> list[str]:
    """Return future second-Wednesday dates without claiming market execution."""
    cursor = pd.Timestamp(as_of).date()
    start = pd.Timestamp(C6_WITHDRAWAL_START_DATE).date()
    dates: list[str] = []
    year, month = max((cursor.year, cursor.month), (start.year, start.month))
    while len(dates) < count:
        due = second_wednesday(year, month)
        if due >= start and due > cursor:
            dates.append(due.isoformat())
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return dates


def _legacy_dashboard_values(
    *, model_version: str, snapshot_as_of: str, data_status: str, slots: list[dict], cash: float = 0.0,
    notes: str = "", historical_benchmark: dict | None = None, snapshot_rows: list[dict] | None = None,
    ranking_snapshot_as_of: str | None = None, accounting_snapshot_as_of: str | None = None,
) -> list[list[object]]:
    replay_incomplete = any(token in data_status for token in (
        "no_whole_share_replay", "replay_not_materialized", "whole_share_replay_pit_blocked",
    ))
    next_dates = _next_withdrawal_dates(ranking_snapshot_as_of or snapshot_as_of)
    benchmark = historical_benchmark or {}
    current_rows = build_public_snapshot_values(snapshot_rows or [])[1:]
    ticker_names = {
        str(row.get("ticker") or ""): str(row.get("name") or "")
        for row in (snapshot_rows or []) if row.get("ticker") and row.get("name")
    }
    current_rows = [row for row in current_rows if row[1] in {1, 2, 3}]
    latest_date = max((str(row[0]) for row in current_rows), default=snapshot_as_of)
    latest_rows = [row for row in current_rows if str(row[0]) == latest_date]
    latest_by_rank = {int(row[1]): row for row in latest_rows}
    top_rows = [["順位", "股票", "C6分數", "代表意義"]]
    for rank in (1, 2, 3):
        row = latest_by_rank.get(rank)
        if row:
            top_rows.append([
                f"Top{rank}", f"{row[2]} {row[3]}", row[5],
                f"已通過C6條件，當日排名第{rank}",
            ])
        else:
            label = "無其他合格股票" if latest_rows else "排名尚未產出"
            top_rows.append([f"Top{rank}", label, "", ""])

    slot_rows = [["槽位", "持股與股數", "收盤市值", "相對買進成本報酬"]]
    total_mark = float(cash or 0.0)
    for slot in sorted(slots, key=lambda item: int(item.get("slot_id") or 0)):
        shares = int(slot.get("shares") or 0)
        close = float(slot.get("raw_close") or 0.0)
        mark = shares * close
        cost = float(slot.get("position_cost") or 0.0)
        relative = (mark - cost) / cost if cost else ""
        total_mark += mark
        slot_rows.append([
            f"第{slot.get('slot_id')}槽",
            f"{slot.get('ticker', '')} {slot.get('name') or ticker_names.get(str(slot.get('ticker') or ''), '')}｜{shares:,}股",
            mark,
            relative,
        ])
    while len(slot_rows) < 4:
        slot_rows.append([f"第{len(slot_rows)}槽", "尚未建立持股", "", ""])

    if replay_incomplete:
        withdrawal_text = "帳本尚未更新至最新交易日，暫不提供可能錯誤的賣股股數"
    else:
        withdrawal = select_withdrawal_slot(slots, cash=cash)
        withdrawal_text = (
            f"預計由第{withdrawal.get('slot_id')}槽賣出"
            f"{withdrawal.get('ticker', '')} {withdrawal.get('planned_shares', 0):,}股"
            if withdrawal.get("slot_id") else "以帳戶現金提領"
        )

    accounting_date = accounting_snapshot_as_of or snapshot_as_of
    if replay_incomplete:
        status_text = f"排名已更新；持股與損益目前只核對到 {accounting_date}"
    elif "event_coverage_pending" in data_status:
        status_text = f"排名與三槽帳戶已更新至 {accounting_date}；公司行動覆蓋待確認"
    else:
        status_text = f"排名與三槽帳戶均已更新至 {accounting_date}"
    return [
        ["C6 每日選股與三槽模擬帳戶", "", "", ""],
        ["最新排名日期", ranking_snapshot_as_of or latest_date, "正式帳本日期", accounting_date],
        ["資料狀態", status_text, "", ""],
        ["今日 Top1～Top3", "", "", ""],
        *top_rows,
        ["", "", "", ""],
        [f"三槽模擬帳戶（截至 {accounting_date[5:] if len(accounting_date) >= 10 else accounting_date}）", "", "", ""],
        *slot_rows,
        ["帳戶現金", _money(float(cash or 0.0)), "帳戶總資產", _money(total_mark)],
        ["相對700萬元損益", _money(total_mark - C6_INITIAL_CAPITAL), "報酬率", total_mark / C6_INITIAL_CAPITAL - 1],
        ["", "", "", ""],
        ["每月提領安排", "", "", ""],
        ["下次預定提領日", next_dates[0], "目標金額", _money(C6_WITHDRAWAL_AMOUNT)],
        ["賣股原則", "從三槽中報酬最低的一槽，賣出最接近7.5萬元的整股", "", ""],
        ["目前預估", withdrawal_text, "", ""],
        ["", "", "", ""],
        ["64條歷史路徑比較（不計入上述模擬帳戶）", "", "", ""],
        ["統計期間", benchmark.get("coverage", "2023年64個不同起點至2026-08-12"), "每月提領", C6_WITHDRAWAL_AMOUNT],
        ["期末資產中位數", benchmark.get("statistical_median_final_nav", ""), "帳戶最大回撤中位數", benchmark.get("statistical_median_account_nav_mdd", "")],
        ["TWR年化報酬中位數", benchmark.get("statistical_median_twr_cagr", ""), "TWR最大回撤中位數", benchmark.get("statistical_median_twr_mdd", "")],
        ["下中位代表路徑", benchmark.get("lower_median_actual_route_id", ""), "期末資產", benchmark.get("lower_median_actual_final_nav", "")],
        ["", "", "", ""],
        ["目前限制", "", "", ""],
        ["說明", f"{accounting_date}之後的交易判斷仍待完整資料；這不是空手，也不是確認續抱。", "", ""],
        ["使用方式", "每日先看Top1～3；持股、損益與提領只以「正式帳本日期」為準。", "", ""],
        [MODEL_LOGIC, "", "", ""],
    ]


def build_dashboard_values(*, ledger_rows=None, **kwargs):
    from .c6_dashboard_layout import layout
    legacy = _legacy_dashboard_values(**kwargs)
    names = {str(r.get('ticker')): r.get('name', '') for r in kwargs.get('snapshot_rows', [])}
    holdings = [dict(s, name=s.get('name') or names.get(str(s.get('ticker')), '')) for s in kwargs['slots']]
    realized = sum(float(r.get('realized_pnl') or 0) for r in (ledger_rows or []))
    withdrawals = sum(float(r.get('gross_amount') or 0) for r in (ledger_rows or []) if r.get('event_type') == 'withdrawal')
    return layout(title='C6研究版｜每日選股與模擬帳戶', date=kwargs.get('ranking_snapshot_as_of') or kwargs['snapshot_as_of'],
        status=legacy[2][1], top_rows=legacy[5:8], holdings=holdings, cash=kwargs.get('cash', 0),
        realized=realized, withdrawals=withdrawals, model_logic=MODEL_LOGIC,
        history=legacy[23:27], details=[['正式帳本日期', kwargs.get('accounting_snapshot_as_of') or kwargs['snapshot_as_of']],
            ['下次預定提領日', legacy[18][1]], ['目前預估', legacy[20][1]], ['目前限制', legacy[29][1]]])


def publish_snapshot(
    spreadsheet_id: str,
    *,
    model_version: str,
    snapshot_as_of: str,
    data_status: str,
    source_manifest_hash: str,
    snapshot_rows: list[dict],
    ledger_rows: list[dict],
    slots: list[dict],
    cash: float = 0.0,
    notes: str = "",
    historical_benchmark: dict | None = None,
    ranking_snapshot_as_of: str | None = None,
    accounting_snapshot_as_of: str | None = None,
) -> dict:
    """Append immutable C6 data, then move the mutable dashboard pointer."""
    for row in snapshot_rows:
        row.setdefault("model_version", model_version)
        row.setdefault("snapshot_as_of", snapshot_as_of)
        row.setdefault("data_status", data_status)
        row.setdefault("source_manifest_hash", source_manifest_hash)
        row.setdefault(
            "immutable_snapshot_key",
            "|".join(_key(row, ("model_version", "snapshot_as_of", "signal_date", "rank"))),
        )
    for row in ledger_rows:
        row.setdefault("model_version", model_version)
        row.setdefault("snapshot_as_of", snapshot_as_of)
        row.setdefault(
            "immutable_event_key",
            "|".join(_key(row, ("model_version", "snapshot_as_of", "account_date", "event_sequence"))),
        )
    client = SheetsClient(spreadsheet_id)
    public_snapshot_values = build_public_snapshot_values(snapshot_rows)
    trade_values = build_trade_record_values(ledger_rows, snapshot_rows)
    client.clear(f"'{SNAPSHOT_SHEET}'!A1:Z50000")
    client.update(f"'{SNAPSHOT_SHEET}'!A1", public_snapshot_values)
    client.clear(f"'{LEDGER_SHEET}'!A1:R50000")
    client.update(f"'{LEDGER_SHEET}'!A1", trade_values)
    dashboard = build_dashboard_values(
        model_version=model_version, snapshot_as_of=snapshot_as_of, data_status=data_status, slots=slots, cash=cash,
        notes=notes, historical_benchmark=historical_benchmark, snapshot_rows=snapshot_rows,
        ranking_snapshot_as_of=ranking_snapshot_as_of, accounting_snapshot_as_of=accounting_snapshot_as_of, ledger_rows=ledger_rows,
    )
    from .c6_dashboard_layout import format_dashboard
    format_dashboard(client)
    client.clear(f"'{DASHBOARD_SHEET}'!A1:H60")
    client.update(f"'{DASHBOARD_SHEET}'!A1", dashboard)
    from .c6_actual_dashboard_publish import signal_formulas
    for address, values in signal_formulas().items():
        client.update(address, values)
    expected_date = ranking_snapshot_as_of or snapshot_as_of
    if client.get(f"'{DASHBOARD_SHEET}'!B2") != [[expected_date]]:
        raise RuntimeError('C6 Dashboard ranking date read-back mismatch')
    for rank in range(1, 4):
        row = next((r for r in public_snapshot_values[1:]
                    if str(r[0]) == expected_date and str(r[1]) == str(rank)), None)
        expected = f'{row[2]} {row[3]}' if row else '無其他合格股票'
        if client.get(f"'{DASHBOARD_SHEET}'!B{rank+5}") != [[expected]]:
            raise RuntimeError('C6 Dashboard ranking read-back mismatch')
    return {
        "model_version": model_version,
        "snapshot_as_of": snapshot_as_of,
        "data_status": data_status,
        "snapshot_rows_published": len(public_snapshot_values) - 1,
        "trade_rows_published": len(trade_values) - 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Append an immutable C6 research snapshot to its Google Sheet.")
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--payload", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    # Coverage remains immutable payload metadata; it is not a Sheets write argument.
    payload.pop("coverage", None)
    if "historical_benchmark" not in payload and DEFAULT_HISTORICAL_BENCHMARK_PATH.exists():
        payload["historical_benchmark"] = json.loads(DEFAULT_HISTORICAL_BENCHMARK_PATH.read_text(encoding="utf-8"))
    contract_fields = {
        "model_version", "snapshot_as_of", "data_status", "source_manifest_hash", "snapshot_rows",
        "ledger_rows", "slots", "cash", "notes", "historical_benchmark", "ranking_snapshot_as_of",
        "accounting_snapshot_as_of",
    }
    result = publish_snapshot(args.spreadsheet_id, **{key: value for key, value in payload.items() if key in contract_fields})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
