"""Share score0 signals with the actual account, without simulating actual fills.

This publisher owns only the signal database and the Dashboard signal/status cells.
Holdings, cash, actual transactions and exit history remain user-account authority.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from .c6_dashboard_publish import build_public_snapshot_values
from .v4d_dashboard_publish import SheetsClient
from .c6_actual_account import valid_source_hash, holding_history_observation, evaluate_exit_observation

SIGNALS = 'C6每日訊號資料庫'
DASHBOARD = 'C6 Dashboard'
ACTUAL_TRADES = 'C6實際交易紀錄'


def daily_observation_rows(account_rows: list, payload: dict, actual_ledger: list | None = None) -> list:
    """Value the currently reported units, without inferring any execution."""
    target = payload['ranking_snapshot_as_of']
    records = payload.get('market_rows', [])
    entries = {}
    for event in (actual_ledger or [])[1:]:
        if len(event) >= 18 and event[2] == '期初持倉登錄':
            match = re.search(r'實際買入日(\d{4}-\d{2}-\d{2})', str(event[17]))
            if match:
                key = str(event[3])
                if key in entries and entries[key] != match[1]:
                    raise ValueError('Conflicting actual entry dates')
                entries[key] = match[1]
    result = []
    for row in account_rows[2:6]:
        label = str(row[1])
        match = re.fullmatch(r'(\d{4})\s+(.+?)｜(\d+)股', label)
        if not match:
            raise ValueError('Actual holding layout changed; review required')
        ticker, name, units_text = match.groups()
        units, cost = int(units_text), float(row[2])
        if units <= 0 or not math.isfinite(cost) or cost <= 0:
            raise ValueError('Invalid actual units or cost')
        quotes = [q for q in records if str(q.get('ticker')) == ticker and q.get('date') == target]
        if len(quotes) != 1 or not valid_source_hash(quotes[0].get('source_hash')):
            raise ValueError(f'Actual official price lineage unavailable: {ticker}')
        close = quotes[0]['close']
        if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(close) or close <= 0:
            raise ValueError(f'Invalid actual mark: {ticker}')
        pnl = round(units * (close - cost), 2)
        history = payload.get('actual_holding_history', {})
        entry = entries.get(ticker)
        tracking = holding_history_observation(
            ticker=ticker, entry_date=entry, as_of=target,
            official_rows=history.get('official_rows', []), trading_dates=history.get('trading_dates', []),
            calendar_complete=history.get('calendar_complete') is True and history.get('end') == target,
        ) if entry else {}
        td = tracking.get('holding_td')
        high = tracking.get('raw_close_high')
        details = f'實際買入日{entry}；' if entry else '買入日待確認；'
        details += f'已持有{td} TD；' if td else '交易日資料待補；'
        details += f'持有期間最高原始收盤{high:g}元（未調整公司行動）；' if high else '持有高點資料待補；'
        if td:
            checks = evaluate_exit_observation(current_return=close / cost - 1, holding_td=td,
                                               peak_return=None, macro_triple=None)
            labels = {'hard_loss_guard': '成本虧損12%', 'activated_peak_drawdown': '+20%後回撤12%',
                      'first_growth_review_failed': 'TD35未轉正', 'second_growth_review_failed': 'TD50未達8%',
                      'maximum_holding_td': 'TD60退出', 'macro_triple': '獲利15%與宏觀三條件'}
            details += '成本參考條件：' + '；'.join(
                labels[key] + ('已達門檻' if state is True else '未達門檻' if state is False else '待資料')
                for key, state in checks['conditions'].items()) + '；'
        result.append([target, row[0], '每日估值（非成交）', ticker, name,
                       '持倉追蹤', close, units, round(units * close, 2), '', '', '', '', '',
                       td if td else '待完整交易日資料', '', close / cost - 1,
                       details + f'帳面未實現損益{pnl:,.2f}元（成本含買進費用，未扣未發生賣出成本，不含股利）；完整退出條件待接通，非續抱或賣出指令。'])
    return result


def holding_quote_text(account_rows: list, payload: dict) -> str:
    """Display only same-session source quotes, never carry or account valuation."""
    target = payload['ranking_snapshot_as_of']
    prices = {str(row['ticker']): row for row in payload.get('market_rows', [])
              if str(row.get('date')) == target}
    lines = []
    for row in account_rows[2:6]:  # existing account A12:D15
        label = str(row[1]) if len(row) > 1 else ''
        match = re.match(r'^(\d{4})\s', label)
        if not match:
            raise ValueError('Actual holding identifier needs review')
        ticker = match.group(1)
        record = prices.get(ticker)
        value = record.get('close') if record else None
        accepted = isinstance(value, (int, float)) and math.isfinite(value) and value > 0 and valid_source_hash(record.get('source_hash'))
        lines.append(f'{ticker}：{value:g}' if accepted else f'{ticker}：官方來源待補')
    return f'{target}｜' + '；'.join(lines) + '（非已驗證帳戶市值）'


def signal_formulas() -> dict[str, list[list[object]]]:
    # Use explicit rank lookups: fewer than three qualified names stay empty.
    source = f"'{SIGNALS}'"
    result = {f"'{DASHBOARD}'!B2": [[f'=IF(COUNT({source}!A2:A)=0,"",MAX({source}!A2:A))']]}
    for rank in range(1, 4):
        condition = f'{source}!A2:A=$B$2,{source}!B2:B={rank}'
        name = f'IFNA(INDEX(FILTER({source}!C2:C&" "&{source}!D2:D,{condition}),1),"無其他合格股票")'
        score = f'IFNA(INDEX(FILTER({source}!F2:F,{condition}),1),"")'
        result[f"'{DASHBOARD}'!B{rank+5}:D{rank+5}"] = [[f'={name}', f'={score}',
            f'=IF(C{rank+5}="","", "通過C6條件，當日排名第{rank}")']]
    return result


def publish(spreadsheet_id: str, payload: dict) -> dict:
    expected_date = str(payload['ranking_snapshot_as_of'])
    rows = build_public_snapshot_values(payload['snapshot_rows'])
    latest = [row for row in rows[1:] if str(row[0]) == expected_date]
    if not latest or max(str(row[0]) for row in rows[1:]) != expected_date:
        raise ValueError('Actual account signal source date mismatch')
    client = SheetsClient(spreadsheet_id)
    # Guard against accidentally targeting the simulation workbook.
    actual_ledger = client.get(f"'{ACTUAL_TRADES}'!A1:R1000")
    if not actual_ledger:
        raise ValueError('Actual account ledger is missing; refusing to publish')
    before = client.get(f"'{DASHBOARD}'!A10:D31")
    if not before:
        raise ValueError('Actual account holdings scaffold is missing')
    observations = daily_observation_rows(before, payload, actual_ledger)
    # Upsert only our own same-day observation rows; preserve every real event.
    observation_writes = []
    next_row = len(actual_ledger) + 1
    for observation in observations:
        matches = [i + 1 for i, row in enumerate(actual_ledger)
                   if len(row) > 3 and str(row[0]) == expected_date
                   and row[2] == '每日估值（非成交）' and str(row[3]) == observation[3]]
        if len(matches) > 1:
            raise ValueError('Duplicate actual observations need review')
        destination = matches[0] if matches else next_row
        if not matches:
            next_row += 1
        if destination > 1000:
            raise ValueError('Actual ledger capacity needs extension')
        observation_writes.append((destination, observation))
    client.clear(f"'{SIGNALS}'!A1:G1000")
    client.update(f"'{SIGNALS}'!A1", rows)
    for address, values in signal_formulas().items():
        client.update(address, values)
    quote_text = holding_quote_text(before, payload)
    client.update(f"'{DASHBOARD}'!A18:B18", [['當日官方收盤', quote_text]])
    client.update(f"'{DASHBOARD}'!B27", [['每日排名已接通共用C6來源；持倉退出與公司行動核對尚未完成']])
    db = client.get(f"'{SIGNALS}'!A1:G{len(rows)}")
    actual = [row for row in db[1:] if row and str(row[0]) == expected_date]
    if [(str(r[1]), str(r[2]) if len(r) > 2 else '') for r in actual] != [
        (str(r[1]), str(r[2]) if len(r) > 2 else '') for r in latest
    ]:
        raise RuntimeError('Actual signal database read-back mismatch')
    if client.get(f"'{DASHBOARD}'!B2") != [[expected_date]]:
        raise RuntimeError('Actual Dashboard date read-back mismatch')
    for rank in range(1, 4):
        expected = next((r for r in latest if str(r[1]) == str(rank)), None)
        expected_name = f'{expected[2]} {expected[3]}' if expected else '無其他合格股票'
        if client.get(f"'{DASHBOARD}'!B{rank+5}") != [[expected_name]]:
            raise RuntimeError(f'Actual Dashboard rank {rank} read-back mismatch')
    if client.get(f"'{ACTUAL_TRADES}'!A1:R1000") != actual_ledger:
        raise RuntimeError('Actual trades changed during signal publication')
    # The only allowed change in this rectangle is the integration status B27.
    after = client.get(f"'{DASHBOARD}'!A10:D31")
    before[17][1] = after[17][1]
    before[8] = ['當日官方收盤', quote_text]
    if before != after:
        raise RuntimeError('Actual account fields changed during signal publication')
    for destination, observation in observation_writes:
        address = f"'{ACTUAL_TRADES}'!A{destination}:R{destination}"
        client.update(address, [observation])
        verified = client.get(address)
        if not verified or len(verified[0]) != 18 or str(verified[0][3]) != observation[3] or verified[0][0] != expected_date:
            raise RuntimeError('Actual daily observation read-back mismatch')
    return {'signal_date': expected_date, 'signal_readback_verified': True,
            'actual_trades_changed': False, 'daily_observation_rows': len(observations),
            'account_exit_tracking_ready': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spreadsheet-id', required=True)
    parser.add_argument('--payload', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.spreadsheet_id, json.loads(args.payload.read_text(encoding='utf-8'))), ensure_ascii=False))


if __name__ == '__main__':
    main()
