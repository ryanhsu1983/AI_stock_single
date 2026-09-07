"""Read-only valuation of user-confirmed holdings; never creates actual fills."""
from __future__ import annotations

import math
import re
from datetime import date


def valid_source_hash(value: object) -> bool:
    """Only an actual SHA256 digest is evidence, not stringified NaN."""
    return isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{64}', value) is not None


def value_account(holdings: list[dict], market_rows: list[dict], as_of: str,
                  cash: float, cash_confirmed: bool, event_coverage: dict) -> dict:
    """Require exact official marks and per-position event coverage for NAV.

    Coverage must start no later than entry and finish at this session. An
    inventory, keyword search, or a previous-session certificate is insufficient.
    Cash estimates are always provisional, even with complete marks/events.
    """
    date.fromisoformat(as_of)
    if isinstance(cash, bool) or not isinstance(cash, (int, float)) or not math.isfinite(cash):
        raise ValueError('Invalid actual cash balance')
    seen = set()
    values = []
    blockers = []
    for holding in holdings:
        ticker = str(holding['ticker'])
        if ticker in seen:
            raise ValueError('Duplicate actual position')
        seen.add(ticker)
        units = holding['units']
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValueError('Actual units must be positive whole shares')
        entry = holding['entry_date']
        if date.fromisoformat(entry) > date.fromisoformat(as_of):
            raise ValueError('Actual position entry is in the future')
        rows = [row for row in market_rows if str(row.get('ticker')) == ticker
                and row.get('date') == as_of]
        price = rows[0].get('close') if len(rows) == 1 else None
        accepted = (not isinstance(price, bool) and isinstance(price, (int, float))
                    and math.isfinite(price) and price > 0
                    and valid_source_hash(rows[0].get('source_hash')) if len(rows) == 1 else False)
        if not accepted:
            blockers.append(f'{ticker}:exact_official_mark_missing_or_ambiguous')
        coverage = event_coverage.get(ticker, {})
        covered = (coverage.get('accepted') is True
                   and coverage.get('start') == entry
                   and coverage.get('end') == as_of
                   and bool(coverage.get('evidence_hash'))
                   and coverage.get('units_reconciled') == units)
        if not covered:
            blockers.append(f'{ticker}:holder_event_coverage_missing')
        values.append({'ticker': ticker, 'units': units,
                       'official_close': price if accepted else None,
                       'market_value': units * price if accepted and covered else None})
    nav = cash + sum(row['market_value'] for row in values) if not blockers else None
    return {'as_of': as_of, 'positions': values, 'nav': nav,
            'cash_confirmed': cash_confirmed, 'nav_is_provisional': not cash_confirmed,
            'status': 'blocked' if blockers else ('verified' if cash_confirmed else 'provisional_cash'),
            'blockers': blockers, 'actual_trades_changed': False}
