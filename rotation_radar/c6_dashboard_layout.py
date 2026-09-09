"""Common, stable coordinates for simulation and manually confirmed C6 holdings."""
from .c6_account_basis import INITIAL_CAPITAL


def layout(*, title, date, status, top_rows, holdings, cash, realized, withdrawals,
           model_logic, details=None, history=None):
    rows = [[''] * 8 for _ in range(40)]
    def put(row, values):
        rows[row-1] = list(values) + [''] * (8-len(values))
    put(1, [title])
    put(2, ['最新排名日期', date, '帳戶追蹤起日', '2026-08-05', '期初總資產', INITIAL_CAPITAL])
    put(3, ['資料狀態', status])
    put(4, ['01｜今日候選排名'])
    put(5, ['順位', '股票', 'C6分數', '代表意義'])
    for i, row in enumerate(top_rows[:3], 6):
        put(i, row)
    put(9, ['排名不是加碼指令', '已持有股票不因持續Top1而每日加碼；實際成交只依Ryan確認登錄。'])
    put(10, ['02｜持股明細（預留五格；模型仍為三槽）'])
    put(11, ['槽位', '持股與股數', '每股含費成本', '剩餘持股成本', '官方收盤', '持股市值', '未實現損益', '成本報酬率'])
    total_cost = total_mark = 0.0
    if len(holdings) > 5:
        raise ValueError('More than five actual positions require a layout extension')
    by_slot = {int(float(h['slot_id'])): h for h in holdings}
    if len(by_slot) != len(holdings) or any(i not in range(1, 6) for i in by_slot):
        raise ValueError('Invalid or duplicate slot identifier')
    for i in range(5):
        if by_slot.get(i+1, {}).get('shares', 0):
            h = by_slot[i+1]
            units, basis, close = h['shares'], h['position_cost'], h['raw_close']
            mark = units * close
            total_cost += basis
            total_mark += mark
            put(12+i, [h['slot_id'], f"{h['ticker']} {h.get('name', '')}｜{units}股", basis/units,
                       basis, close, mark, mark-basis, mark/basis-1])
        else:
            put(12+i, [i+1, '未持有／預留位置'])
    nav = cash + total_mark
    put(17, ['03｜帳戶資產與損益'])
    put(18, ['現金餘額', cash, '帳戶總資產（參考估值）', nav, '持股成本合計', total_cost])
    put(19, ['期初總資產', INITIAL_CAPITAL, '相對期初本金損益（含提領）', nav+withdrawals-INITIAL_CAPITAL,
             '累計報酬（非TWR）', (nav+withdrawals)/INITIAL_CAPITAL-1])
    put(20, ['已實現交易損益', realized, '未實現持股損益', total_mark-total_cost, '累計已確認提領', withdrawals])
    put(21, ['提領安排', '每月第二個星期三；休市順延', '每月目標金額', 75000])
    put(22, ['提領與成本', '提領不是虧損。賣股按股數比例分攤成本；剩餘每股成本不因提領改變。'])
    put(23, ['成交與槽位', '研究版最多三槽；實際版可超過三檔，未回報成交不入帳。'])
    put(24, ['持有高點與TD', '請見交易紀錄「原因／狀態」。原始價格高點與公司行動調整後高點分開。'])
    put(25, ['退出追蹤', '-12%、+20%後回撤12%、TD35／50／60、獲利15%與宏觀三條件；缺資料不判成安全。'])
    put(26, ['價格與資金治理', '收盤市值採已回報股數×官方收盤；未扣尚未發生的賣出成本，公司行動覆蓋限制仍保留。'])
    put(27, ['更新狀態', status])
    put(28, ['帳戶資料確認', '兩版期初總資產7,676,961.04元；實際版包括8/5期初既有現金198,073.04元，非投資獲利。'])
    put(29, ['損益口徑', '已實現＋未實現不含未核對股利；帳戶損益加回已確認提領。尚未實際提領不扣現金。'])
    put(30, ['歷史研究', '下方舊歷史基準保留原700萬元與原期間，不代表本次新本金帳戶績效。'])
    put(31, ['04｜模型完整說明'])
    put(32, [model_logic])
    put(34, ['05｜補充資料與歷史參考'])
    for i, row in enumerate((history or []) + (details or []), 35):
        if i > len(rows):
            rows.append([''] * 8)
        put(i, row)
    return rows


def format_requests(sheet_id):
    def region(r0, r1, c0=0, c1=8):
        return dict(sheetId=sheet_id, startRowIndex=r0, endRowIndex=r1, startColumnIndex=c0, endColumnIndex=c1)
    requests = [
        {'unmergeCells': {'range': region(0, 60)}},
        {'repeatCell': {'range': region(0, 60), 'cell': {'userEnteredFormat': {
            'backgroundColor': {'red': 1, 'green': 1, 'blue': 1}, 'textFormat': {'fontFamily': 'Arial', 'fontSize': 11},
            'verticalAlignment': 'MIDDLE', 'wrapStrategy': 'WRAP', 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00;[Red]-#,##0.00'}}}, 'fields': 'userEnteredFormat'}},
        {'updateDimensionProperties': {'range': {'sheetId': sheet_id, 'dimension': 'ROWS', 'startIndex': 0, 'endIndex': 60}, 'properties': {'pixelSize': 38}, 'fields': 'pixelSize'}},
        {'updateSheetProperties': {'properties': {'sheetId': sheet_id, 'gridProperties': {'hideGridlines': True, 'frozenRowCount': 2}}, 'fields': 'gridProperties.hideGridlines,gridProperties.frozenRowCount'}},
    ]
    for col, width in enumerate([185, 275, 170, 205, 125, 155, 155, 135]):
        requests.append({'updateDimensionProperties': {'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': col, 'endIndex': col+1}, 'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
    for row in (1, 4, 10, 17, 31, 34):
        requests.extend([
            {'mergeCells': {'range': region(row-1, row), 'mergeType': 'MERGE_ALL'}},
            {'repeatCell': {'range': region(row-1, row), 'cell': {'userEnteredFormat': {'backgroundColor': {'red': .08, 'green': .20, 'blue': .29}, 'textFormat': {'bold': True, 'fontSize': 12, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}}}}, 'fields': 'userEnteredFormat(backgroundColor,textFormat)'}},
        ])
    for row in (3, 9, 22, 23, 24, 25, 26, 27, 28, 29, 30, 39, 40, 41, 42, 43):
        requests.append({'mergeCells': {'range': region(row-1, row, 1), 'mergeType': 'MERGE_ALL'}})
    for row in (5, 11, 18, 19, 20):
        requests.append({'repeatCell': {'range': region(row-1, row), 'cell': {'userEnteredFormat': {'backgroundColor': {'red': .91, 'green': .96, 'blue': .96}, 'textFormat': {'bold': True}}}, 'fields': 'userEnteredFormat(backgroundColor,textFormat.bold)'}})
    requests.extend([
        {'repeatCell': {'range': region(11, 16, 0, 1), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': region(35, 37, 3, 4), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': region(36, 37, 1, 2), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'updateDimensionProperties': {'range': {'sheetId': sheet_id, 'dimension': 'ROWS', 'startIndex': 34, 'endIndex': 35}, 'properties': {'pixelSize': 110}, 'fields': 'pixelSize'}},
        {'mergeCells': {'range': region(31, 32), 'mergeType': 'MERGE_ALL'}},
        {'repeatCell': {'range': region(31, 32), 'cell': {'userEnteredFormat': {'verticalAlignment': 'TOP', 'horizontalAlignment': 'LEFT'}}, 'fields': 'userEnteredFormat(verticalAlignment,horizontalAlignment)'}},
        {'updateDimensionProperties': {'range': {'sheetId': sheet_id, 'dimension': 'ROWS', 'startIndex': 31, 'endIndex': 32}, 'properties': {'pixelSize': 1000}, 'fields': 'pixelSize'}},
        {'repeatCell': {'range': region(11, 16, 7, 8), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%;[Red]-0.00%'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': region(18, 19, 5, 6), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%;[Red]-0.00%'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': region(1, 2, 1, 2), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
    ])
    return requests


def format_dashboard(client):
    import requests
    from .v4d_dashboard_publish import retry_request
    metadata = retry_request(requests.get, client.base, headers=client.headers,
        params={'fields': 'sheets(properties(sheetId,title))'}, timeout=30)
    client._raise_for_status(metadata)
    sheet_id = next(s['properties']['sheetId'] for s in metadata.json()['sheets'] if s['properties']['title'] == 'C6 Dashboard')
    response = retry_request(requests.post, client.base + ':batchUpdate', headers=client.headers,
        json={'requests': format_requests(sheet_id)}, timeout=30)
    client._raise_for_status(response)
